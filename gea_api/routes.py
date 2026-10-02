"""Litestar route handlers: HTTP in, HTTP out, nothing else.

A handler picks the path parameters and the body, and hands a `Call` to the
service function of the same name. `respond` gives every operation the same
frame: authenticate, one transaction, commit or roll back, and one answer
shape, the envelope:

    2xx        {"data": <the resource>, "error": null}
    4xx, 5xx   {"data": null, "error": {"code", "message", "status", "issues", "requestId"}}

The only answer without a body is 304 Not Modified, which HTTP forbids to have one.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from litestar import Request, Response, Router, delete, get, patch, post
from litestar.params import FromPath

from . import errors, services
from .auth import Authenticator, ensure_user
from .db import Database, DatabaseError
from .http import Reply, request_id_from
from .errors import ApiError
from .services import Call

OPENAPI_FILE = Path(__file__).resolve().parents[1] / "gea" / "api" / "openapi.yaml"
RECEIPT_KEY = "PK_CommandReceipt"


@dataclass
class Gea:
    """What the handlers need from the application (kept in app.state.gea)."""
    db: Database
    authenticator: Authenticator


def request_id(request: Request) -> str:
    scope = request.scope
    if "gea.request_id" not in scope:
        scope["gea.request_id"] = request_id_from(request.headers.get("x-request-id"))   # type: ignore[typeddict-unknown-key]
    return scope["gea.request_id"]                                                      # type: ignore[typeddict-item]


def _json(body: Any, status: int, headers: dict[str, str]) -> Response:
    return Response(content=json.dumps(body, ensure_ascii=False).encode("utf-8"), status_code=status,
                    headers=headers, media_type="application/json")


def to_response(reply: Reply, rid: str) -> Response:
    headers = {"X-Request-Id": rid, **reply.headers}
    if reply.etag:
        headers["ETag"] = reply.etag
    if reply.location:
        headers["Location"] = reply.location
    if reply.status == 304:
        return Response(content=b"", status_code=304, headers=headers)
    return _json({"data": reply.body, "error": None}, reply.status, headers)


def error_response(request: Request, error: ApiError) -> Response:
    rid = request_id(request)
    return _json({"data": None, "error": error.body(request_id=rid)}, error.status,
                 {"X-Request-Id": rid, **error.headers})


def respond(request: Request, action: Callable[[Call], Reply]) -> Response:
    gea: Gea = request.app.state.gea
    rid = request_id(request)
    caller = gea.authenticator.authenticate(request.headers)

    def attempt() -> Reply:
        with gea.db.transaction() as connection:
            principal = ensure_user(connection, caller)
            return action(Call(
                connection=connection, user_id=principal.user_id, request_id=rid,
                path=request.url.path, query=request.query_params,
                if_match=request.headers.get("if-match"), if_none_match=request.headers.get("if-none-match"),
                idempotency_key=request.headers.get("idempotency-key"),
                role_name=principal.role_name, permissions=principal.permissions))

    try:
        try:
            reply = attempt()
        except DatabaseError as error:
            # Two requests with the same Idempotency-Key ran at once: the second one lost on the
            # receipt's primary key and was rolled back. The first answer is stored now; replay it.
            if getattr(error, "sqlstate", None) != "23505" or getattr(error.diag, "constraint_name", None) != RECEIPT_KEY:
                raise
            reply = attempt()
    except DatabaseError as error:
        raise errors.from_database_error(error) from error
    return to_response(reply, rid)


# ----------------------------------------------------------------------- health, contract document
@get("/health", sync_to_thread=True)
def get_health(request: Request) -> Response:
    gea: Gea = request.app.state.gea
    try:
        with gea.db.transaction() as connection:
            return to_response(services.health(connection), request_id(request))
    except DatabaseError as error:
        raise errors.from_database_error(error) from error


@get("/openapi.yaml", sync_to_thread=False)
def get_openapi(request: Request) -> Response:
    if not OPENAPI_FILE.is_file():
        raise errors.not_found("The OpenAPI document")
    return Response(content=OPENAPI_FILE.read_bytes(), status_code=200, media_type="application/yaml",
                    headers={"X-Request-Id": request_id(request)})


# ----------------------------------------------------------------------- users
@get("/users/me", sync_to_thread=True)
def get_me(request: Request) -> Response:
    return respond(request, services.get_me)


@patch("/users/me", sync_to_thread=True)
def update_me(request: Request, data: Any) -> Response:
    return respond(request, lambda call: services.update_me(call, data))


@get("/users", sync_to_thread=True)
def list_users(request: Request) -> Response:
    return respond(request, services.list_users)


@get("/users/{user_id:uuid}", sync_to_thread=True)
def get_user(request: Request, user_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_user(call, str(user_id)))


@patch("/users/{user_id:uuid}", sync_to_thread=True)
def update_user(request: Request, user_id: FromPath[UUID], data: Any) -> Response:
    return respond(request, lambda call: services.update_user(call, str(user_id), data))


# ----------------------------------------------------------------------- reference data
@get("/roles", sync_to_thread=True)
def get_roles(request: Request) -> Response:
    return respond(request, services.roles)


@get("/regions", sync_to_thread=True)
def get_regions(request: Request) -> Response:
    return respond(request, services.regions)


@get("/business-purposes", sync_to_thread=True)
def get_business_purposes(request: Request) -> Response:
    return respond(request, services.business_purposes)


@get("/benefits", sync_to_thread=True)
def get_benefits(request: Request) -> Response:
    return respond(request, services.benefits)


@get("/treaties", sync_to_thread=True)
def get_treaties(request: Request) -> Response:
    return respond(request, services.treaties)


@get("/datasets", sync_to_thread=True)
def get_datasets(request: Request) -> Response:
    return respond(request, services.datasets)


# ----------------------------------------------------------------------- projects
@get("/projects", sync_to_thread=True)
def list_projects(request: Request) -> Response:
    return respond(request, services.list_projects)


@post("/projects", sync_to_thread=True)
def create_project(request: Request, data: Any) -> Response:
    return respond(request, lambda call: services.create_project(call, data))


@get("/projects/{project_id:uuid}", sync_to_thread=True)
def get_project(request: Request, project_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_project(call, str(project_id)))


@patch("/projects/{project_id:uuid}", sync_to_thread=True)
def update_project(request: Request, project_id: FromPath[UUID], data: Any) -> Response:
    return respond(request, lambda call: services.update_project(call, str(project_id), data))


@get("/projects/{project_id:uuid}/run-parameters", sync_to_thread=True)
def get_run_parameters(request: Request, project_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.run_parameters(call, str(project_id)))


# ----------------------------------------------------------------------- runs: the draft and its submission
@get("/runs", sync_to_thread=True)
def list_runs(request: Request) -> Response:
    return respond(request, services.list_runs)


@post("/runs", sync_to_thread=True)
def create_run(request: Request, data: Any) -> Response:
    return respond(request, lambda call: services.create_run(call, data))


@get("/runs/{run_id:uuid}", sync_to_thread=True)
def get_run(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_run(call, str(run_id)))


@patch("/runs/{run_id:uuid}", sync_to_thread=True)
def update_run(request: Request, run_id: FromPath[UUID], data: Any) -> Response:
    return respond(request, lambda call: services.update_run(call, str(run_id), data))


@delete("/runs/{run_id:uuid}", sync_to_thread=True, status_code=200)
def delete_run(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.delete_run(call, str(run_id)))


@get("/runs/{run_id:uuid}/review", sync_to_thread=True)
def review_run(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.review_run(call, str(run_id)))


@post("/runs/{run_id:uuid}/submit", sync_to_thread=True)
def submit_run(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.submit_run(call, str(run_id)))


# ----------------------------------------------------------------------- execution: observe and decide
@get("/runs/{run_id:uuid}/execution", sync_to_thread=True)
def get_run_execution(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_run_execution(call, str(run_id)))


@get("/runs/{run_id:uuid}/logs", sync_to_thread=True)
def get_run_logs(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.run_logs(call, str(run_id)))


@post("/runs/{run_id:uuid}/cancel", sync_to_thread=True)
def cancel_run(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.cancel_run(call, str(run_id)))


@post("/runs/{run_id:uuid}/resolution", sync_to_thread=True)
def resolve_run(request: Request, run_id: FromPath[UUID], data: Any) -> Response:
    return respond(request, lambda call: services.resolve_run(call, str(run_id), data))


# ----------------------------------------------------------------------- contracts
@get("/runs/{run_id:uuid}/contracts", sync_to_thread=True)
def list_run_contracts(request: Request, run_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.list_run_contracts(call, str(run_id)))


@get("/runs/{run_id:uuid}/contracts/{version:int}", sync_to_thread=True)
def get_run_contract(request: Request, run_id: FromPath[UUID], version: FromPath[int]) -> Response:
    return respond(request, lambda call: services.get_run_contract(call, str(run_id), version))


@get("/contracts/{contract_id:uuid}", sync_to_thread=True)
def get_contract(request: Request, contract_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_contract(call, str(contract_id)))


# ----------------------------------------------------------------------- jobs
@get("/jobs", sync_to_thread=True)
def list_jobs(request: Request) -> Response:
    return respond(request, services.list_jobs)


@post("/jobs", sync_to_thread=True)
def create_job(request: Request, data: Any) -> Response:
    return respond(request, lambda call: services.create_job(call, data))


@get("/jobs/{job_id:uuid}", sync_to_thread=True)
def get_job(request: Request, job_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.get_job(call, str(job_id)))


# ----------------------------------------------------------------------- operations
@post("/operations/deliveries/{contract_id:uuid}/retry", sync_to_thread=True)
def retry_delivery(request: Request, contract_id: FromPath[UUID]) -> Response:
    return respond(request, lambda call: services.retry_delivery(call, str(contract_id)))


HANDLERS = [
    get_health, get_openapi, get_me, update_me, list_users, get_user, update_user,
    get_roles, get_regions, get_business_purposes, get_benefits, get_treaties, get_datasets,
    list_projects, create_project, get_project, update_project, get_run_parameters,
    list_runs, create_run, get_run, update_run, delete_run, review_run, submit_run,
    get_run_execution, get_run_logs, cancel_run, resolve_run,
    list_run_contracts, get_run_contract, get_contract,
    list_jobs, create_job, get_job, retry_delivery,
]


def build_router() -> Router:
    """A new Router per application: Litestar lets a Router instance belong to one app only."""
    return Router(path=services.API_PREFIX, route_handlers=HANDLERS)
