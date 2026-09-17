"""A small Snowflake-compatible HTTP surface for the ordinary Python connector."""
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
import re
import zlib

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from sqlglot import Dialect, exp

from . import __version__
from .engine import Engine, SqlError

MAX_BYTES = 8 * 1024 * 1024


def column_metadata(name, dtype):
    dtype = str(dtype).upper()
    col = {"name": name, "nullable": True, "length": 16777216,
           "precision": None, "scale": None, "byteLength": None}
    decimal = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", dtype)
    if decimal:
        col.update(type="fixed", precision=int(decimal[1]), scale=int(decimal[2]))
    elif "INT" in dtype:
        col.update(type="fixed", precision=38, scale=0)
    elif dtype in {"DOUBLE", "FLOAT", "REAL"}:
        col.update(type="real", precision=53)
    elif dtype == "BOOLEAN":
        col.update(type="boolean")
    elif dtype == "DATE":
        col.update(type="date")
    elif dtype.startswith("TIMESTAMP"):
        col.update(type="timestamp_ntz", scale=6)
    elif dtype in {"JSON", "VARIANT"} or dtype.startswith(("STRUCT", "MAP")) or dtype.endswith("[]"):
        col.update(type="variant")
    elif dtype in {"VARCHAR", "CHAR", "TEXT", "NULL"}:
        col.update(type="text")
    else:
        raise SqlError(f"Result type {dtype} is not supported by the local JSON protocol.")
    return col


def encode_value(value, col):
    if value is None:
        return None
    if col["type"] == "boolean":
        return "1" if value else "0"
    if col["type"] == "date":
        return str((value - date(1970, 1, 1)).days)
    if col["type"] == "timestamp_ntz":
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        delta = value - datetime(1970, 1, 1)
        return str(Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000)
    if col["type"] == "variant" and not isinstance(value, str):
        return json.dumps(value, default=str)
    return str(value)


def envelope(result, session):
    columns = [column_metadata(c[0], c[1]) for c in result.columns]
    rows = [[encode_value(v, c) for v, c in zip(row, columns)] for row in result.rows]
    return {"success": True, "code": "0", "message": "",
            "data": {"queryId": result.query_id, "sqlState": "00000", "rowtype": columns,
                     "rowset": rows, "total": len(rows), "returned": len(rows),
                     "queryResultFormat": "json", "statementTypeId": 0,
                     "finalDatabaseName": session.database, "finalSchemaName": session.schema,
                     "finalWarehouseName": session.warehouse, "finalRoleName": session.role,
                     "parameters": []}}


def failure(error):
    return {"success": False, "code": getattr(error, "code", "001003"),
            "message": str(error), "data": {"queryId": getattr(error, "query_id", None),
            "sqlState": getattr(error, "state", "42000")}}


def bind_parameters(sql, bindings):
    """Use tokenizer positions; never replace placeholders inside SQL strings."""
    if not bindings:
        return sql
    placeholders = [t for t in Dialect.get_or_raise("snowflake").tokenize(sql)
                    if sql[t.start:t.end + 1] == "?"]
    if len(placeholders) != len(bindings):
        raise SqlError("Binding count does not match '?' placeholders.", "07001")
    replacements = []
    for i, token in enumerate(placeholders, 1):
        binding = bindings.get(str(i))
        if not binding:
            raise SqlError("Bindings must be numbered consecutively from 1.")
        value, kind = binding.get("value"), binding.get("type", "TEXT")
        if isinstance(value, list):
            raise SqlError("Array binding is unsupported; use scalar bindings or batched SELECT.")
        if value is None:
            replacement = "NULL"
        elif kind == "TEXT":
            replacement = exp.Literal.string(str(value)).sql(dialect="snowflake")
        elif kind in {"FIXED", "REAL"}:
            decimal = Decimal(str(value))
            if not decimal.is_finite():
                raise SqlError("Non-finite numeric binding.")
            replacement = format(decimal, "f")
        elif kind == "BOOLEAN":
            if str(value).lower() not in {"true", "false", "1", "0"}:
                raise SqlError("Invalid boolean binding.")
            replacement = "TRUE" if str(value).lower() in {"true", "1"} else "FALSE"
        else:
            raise SqlError(f"Server binding type {kind} is unsupported in v0.1.")
        replacements.append((token.start, token.end + 1, replacement))
    for start, end, replacement in reversed(replacements):
        sql = sql[:start] + replacement + sql[end:]
    return sql


async def payload(request):
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        raise SqlError("Request exceeds 8 MiB.")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(raw, MAX_BYTES + 1)
        if decoder.unconsumed_tail or len(raw) > MAX_BYTES:
            raise SqlError("Decompressed request exceeds 8 MiB.")
    return json.loads(raw or b"{}")


def create_app(database_path=":memory:", history_path=None, max_rows=20000):
    engine = Engine(database_path, max_rows=max_rows, history_path=history_path)

    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.close()

    def get_session(request):
        match = re.search(r'token="([^"]+)"', request.headers.get("authorization", ""), re.IGNORECASE)
        session = engine.sessions.get(match[1]) if match else None
        if session is None:
            raise SqlError("Unknown local session. Connect again.", "08003")
        return session

    async def login(request):
        try:
            body = await payload(request)
            info, query = body.get("data", {}), request.query_params
            session = await run_in_threadpool(
                engine.session, query.get("databaseName"), query.get("schemaName"),
                info.get("LOGIN_NAME", "LOCAL"), query.get("warehouse"), query.get("roleName"),
                info.get("SESSION_PARAMETERS"))
            return JSONResponse({"success": True, "data": {
                "token": session.token, "masterToken": session.master_token,
                "validityInSeconds": 86400, "masterValidityInSeconds": 86400,
                "sessionId": session.session_id, "serverVersion": "LOCAL-" + __version__,
                "sessionInfo": {"databaseName": session.database, "schemaName": session.schema,
                                "warehouseName": session.warehouse, "roleName": session.role},
                "parameters": [{"name": "CLIENT_TELEMETRY_ENABLED", "value": False}]}})
        except Exception as error:
            return JSONResponse(failure(error))

    def run_query(session, body, request_id):
        with engine.lock:
            fingerprint = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if request_id in session.requests:
                previous, result = session.requests[request_id]
                if previous != fingerprint:
                    return failure(SqlError("A requestId was reused for a different query."))
                return result
            try:
                if body.get("asyncExec"):
                    raise SqlError("Asynchronous SQL execution is unsupported in v0.1.")
                sql = bind_parameters(body["sqlText"], body.get("bindings"))
                result = envelope(engine.execute(session, sql), session)
                if len(json.dumps(result).encode()) > MAX_BYTES:
                    raise SqlError("Result exceeds 8 MiB; use smaller batches.")
            except Exception as error:
                result = failure(error)
            if request_id:
                session.requests[request_id] = (fingerprint, result)
                while len(session.requests) > 16:
                    session.requests.popitem(last=False)
            return result

    async def query(request):
        try:
            body = await payload(request)
            session = get_session(request)
            result = await run_in_threadpool(run_query, session, body, request.query_params.get("requestId"))
            return JSONResponse(result)
        except Exception as error:
            return JSONResponse(failure(error))

    async def session_action(request):
        try:
            session = get_session(request)
            if request.query_params.get("delete") == "true":
                await run_in_threadpool(engine.close_session, session)
            return JSONResponse({"success": True, "data": {}})
        except Exception as error:
            return JSONResponse(failure(error))

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__, "engine": "DuckDB SQL",
                             "sql_handlers": ["SQL"], "protocol": "Snowflake connector subset"})

    async def history(request):
        with engine.lock:
            rows = list(engine.history)
        return JSONResponse({"queries": rows})

    app = Starlette(routes=[
        Route("/session/v1/login-request", login, methods=["POST"]),
        Route("/queries/v1/query-request", query, methods=["POST"]),
        Route("/session", session_action, methods=["POST"]),
        Route("/session/heartbeat", session_action, methods=["POST"]),
        Route("/_emulator/health", health), Route("/_emulator/queries", history),
    ], lifespan=lifespan)
    app.state.engine = engine
    return app
