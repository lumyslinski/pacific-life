"""Local frontend API: publish functions/settings, freeze core, edit variations."""
from threading import RLock
import os
from pathlib import Path

from litestar import Litestar, get, post, put, patch
from litestar.config.cors import CORSConfig
from litestar.exceptions import HTTPException
from litestar.params import FromPath, FromQuery
from litestar.response import Response
import snowflake.connector
from snowflake.connector.errors import Error as DatabaseError

from .bootstrap import connection_options
from .catalog import FunctionCatalog, ConfigCatalog
from .gateway import SnowflakeGateway
from .lifecycle import ModelLifecycle
from .repository import Repository, Conflict, NotFound
from .worker import ModelError


def create_app(connect_options=None, allowed_origins=None):
    options = connect_options or connection_options()
    lock = RLock()  # One local API writer; production needs durable run ownership.

    def execute(action, transaction=False):
        with lock, snowflake.connector.connect(**options) as connection:
            gateway = SnowflakeGateway(connection)
            try:
                if transaction:
                    gateway.rows("BEGIN")
                result = action(gateway)
                if transaction:
                    connection.commit()
                return result
            except Exception as error:
                if transaction:
                    connection.rollback()
                if isinstance(error, HTTPException):
                    raise
                code = 409 if isinstance(error, Conflict) else 404 if isinstance(error, NotFound) else 422
                if isinstance(error, (ModelError, KeyError, TypeError, ValueError)):
                    raise HTTPException(status_code=code, detail=str(error)) from error
                if isinstance(error, DatabaseError):
                    raise HTTPException(status_code=502, detail=f"Database operation failed: {error}") from error
                raise

    def command(operation, data, action):
        request_id = data.get("requestId")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100:
            raise HTTPException(status_code=422, detail="Supply a stable requestId containing 1..100 characters.")
        def run(db):
            repo = Repository(db)
            request = {"operation": operation, "data": data}
            cached = repo.replay(request_id, request)
            if cached is not None:
                return cached
            db.rows("ALTER SESSION SET QUERY_TAG=%s", (request_id,))
            result = action(ModelLifecycle(db))
            repo.remember(request_id, request, result)
            return result
        return execute(run, transaction=True)

    @get("/health", sync_to_thread=False)
    def health() -> dict:
        return {"status": "ok", "mode": "local-simulation", "lifecycleVersion": 2}

    @get("/openapi.yaml", include_in_schema=False, sync_to_thread=False)
    def openapi_yaml() -> Response:
        schema_path = Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
        if not schema_path.is_file():
            raise HTTPException(status_code=404, detail="Canonical OpenAPI document is not packaged.")
        return Response(content=schema_path.read_text(encoding="utf-8"), media_type="application/yaml")

    @get("/functions", sync_to_thread=True)
    def functions() -> dict:
        return execute(lambda db: {"functions": [{"functionId": r[0], "draftRevision": r[1]} for r in
            db.rows("SELECT FUNCTION_ID,REVISION FROM INSURANCE.CALC.FUNCTION_DRAFTS ORDER BY FUNCTION_ID")]})

    @get("/functions/{function_id:str}", sync_to_thread=True)
    def get_function(function_id: FromPath[str]) -> dict:
        return execute(lambda db: FunctionCatalog(db).draft(function_id))

    @put("/functions/{function_id:str}", sync_to_thread=True)
    def edit_function(function_id: FromPath[str], data: dict) -> dict:
        return execute(lambda db: FunctionCatalog(db).edit(function_id, data["expectedRevision"], data["definition"]), transaction=True)

    @post("/functions/{function_id:str}/preview", status_code=200, sync_to_thread=True)
    def preview_function(function_id: FromPath[str], data: dict) -> dict:
        return execute(lambda db: FunctionCatalog(db).preview(function_id, data["expectedRevision"], data["arguments"]))

    @post("/functions/{function_id:str}/publish", status_code=200, sync_to_thread=True)
    def publish_function(function_id: FromPath[str], data: dict) -> dict:
        return execute(lambda db: FunctionCatalog(db).publish(function_id, data["expectedRevision"]))

    @get("/functions/{function_id:str}/releases/{version:int}", sync_to_thread=True)
    def function_release(function_id: FromPath[str], version: FromPath[int]) -> dict:
        return execute(lambda db: FunctionCatalog(db).release(function_id, version))

    @get("/configs", sync_to_thread=True)
    def configs() -> dict:
        return execute(lambda db: {"configs": [{"configId": r[0], "revision": r[1]} for r in
            db.rows("SELECT CONFIG_ID,REVISION FROM INSURANCE.CALC.CONFIG_REVISIONS ORDER BY CONFIG_ID,REVISION")]})

    @post("/configs/{config_id:str}/revisions", status_code=201, sync_to_thread=True)
    def publish_config(config_id: FromPath[str], data: dict) -> dict:
        return execute(lambda db: ConfigCatalog(db).publish(config_id, data), transaction=True)

    @get("/configs/{config_id:str}/revisions/{version:int}", sync_to_thread=True)
    def get_config(config_id: FromPath[str], version: FromPath[int]) -> dict:
        return execute(lambda db: ConfigCatalog(db).get(config_id, version))

    @post("/core-models", status_code=201, sync_to_thread=True)
    def create_core(data: dict) -> dict:
        policies = data.get("policies")
        max_policies = int(os.getenv("CALC_MAX_POLICIES", "100"))
        max_parameters = int(os.getenv("CALC_MAX_PARAMETERS", "100"))
        if not isinstance(policies, list) or not policies or (max_policies > 0 and len(policies) > max_policies):
            raise HTTPException(status_code=422, detail="Invalid policy list or configured policy limit exceeded.")
        if any(not isinstance(p, dict) or not isinstance(p.get("parameters"), dict) or
               (max_parameters > 0 and len(p["parameters"]) > max_parameters) for p in policies):
            raise HTTPException(status_code=422, detail="Invalid parameters or configured parameter limit exceeded.")
        return command("create-core", data, lambda service: service.core(data))

    @get("/core-models/{model_id:str}", sync_to_thread=True)
    def read_core(model_id: FromPath[str]) -> dict:
        return execute(lambda db: Repository(db).core(model_id))

    @post("/variations", status_code=201, sync_to_thread=True)
    def create_variation(data: dict) -> dict:
        return command("create-variation", data, lambda service: service.fork(data))

    @get("/variations/{model_id:str}", sync_to_thread=True)
    def read_variation(model_id: FromPath[str]) -> dict:
        return execute(lambda db: Repository(db).variation(model_id))

    @get("/variations/{model_id:str}/revisions/{version:int}", sync_to_thread=True)
    def read_revision(model_id: FromPath[str], version: FromPath[int]) -> dict:
        return execute(lambda db: Repository(db).variation(model_id, version))

    @patch("/variations/{model_id:str}", sync_to_thread=True)
    def edit_variation(model_id: FromPath[str], data: dict) -> dict:
        return command(f"edit-variation:{model_id}", data, lambda service: service.change(model_id, data))

    @post("/variations/{model_id:str}/calculate", status_code=200, sync_to_thread=True)
    def calculate_variation(model_id: FromPath[str], data: dict) -> dict:
        return command(f"calculate-variation:{model_id}", data, lambda service: service.change(model_id, data, calculate=True))

    @get("/audit/{aggregate_id:str}", sync_to_thread=True)
    def audit(aggregate_id: FromPath[str], after_revision: FromQuery[int] = 0, limit: FromQuery[int] = 100) -> dict:
        if after_revision < 0 or not 1 <= limit <= 100:
            raise HTTPException(status_code=422, detail="Invalid audit page range.")
        def read(db):
            events = Repository(db).audit(aggregate_id, after_revision, limit)
            return {"aggregateId": aggregate_id, "events": events,
                    "nextAfterRevision": events[-1]["revision"] if len(events) == limit else None}
        return execute(read)

    @post("/simulations", status_code=200, sync_to_thread=False)
    def legacy_simulations(data: dict) -> dict:
        raise HTTPException(status_code=410, detail="Use POST /core-models, then POST /variations and /variations/{id}/calculate. Core output is immutable.")

    origins = allowed_origins or os.getenv("FRONTEND_ORIGINS", "http://localhost:4200,http://localhost:5173").split(",")
    return Litestar([health, openapi_yaml, functions, get_function, edit_function, preview_function, publish_function,
        function_release, configs, publish_config, get_config, create_core, read_core, create_variation,
        read_variation, read_revision, edit_variation, calculate_variation, audit, legacy_simulations],
        cors_config=CORSConfig(allow_origins=origins, allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
            allow_headers=["Content-Type"]), debug=False)
