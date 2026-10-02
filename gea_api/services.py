"""One function per API operation.

Each takes a `Call` (the open transaction, the caller and the conditional
headers) and returns a `Reply`, or raises an `ApiError`. Raising rolls the
transaction back, so an operation is applied completely or not at all.

The order inside every write is the same:

    1. replay a stored answer when the Idempotency-Key was seen before   (creates only)
    2. lock the row, read the current representation
    3. compare If-Match with its ETag                                     (412 / 428)
    4. validate the input                                                 (422, every issue at once)
    5. write; the database enforces the business rules                    (409 / 422 by SQLSTATE)
    6. answer with the new representation and its ETag

Before any of that, a function marked `@needs("prepare" | "review" | "administer")` refuses a
caller whose role does not carry that permission (403). Reading needs none.
"""
from __future__ import annotations

import functools
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import errors, queries, runconfig, validation
from .db import Connection
from .http import (Reply, canonical_json, conditional, decode_cursor, encode_cursor, etag_of, idempotency_key,
                   like_pattern, merge_patch, page_limit, request_fingerprint, require_match, sha256_hex)
from .errors import ApiError, issue
from .fields import RUN_FIELDS, SPEC_VERSION

API_PREFIX = "/gea/v1"
PROJECT_STATES = ("draft", "in-progress", "pending-review", "ready", "signed-off")
RUN_STATUSES = ("draft", "queued", "running", "complete", "failed")
JOB_STATUSES = ("queued", "running", "complete", "completed-with-failures", "failed")


@dataclass
class Call:
    connection: Connection
    user_id: str
    request_id: str
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    if_match: str | None = None
    if_none_match: str | None = None
    idempotency_key: str | None = None
    role_name: str = "Viewer"
    permissions: frozenset[str] = frozenset()        # what the caller's role allows: prepare, review, administer


# ----------------------------------------------------------------------- permissions
# Which permission an operation needs, by function name. The contract says the same per
# operation (x-permission in gea/api/openapi.yaml); tests/gea/test_api.py compares the two.
PERMISSIONS: dict[str, str] = {}
_ALLOWS = {"prepare": "prepare projects and runs", "review": "review and sign off",
           "administer": "administer users and operations"}


def needs(permission: str) -> Callable[[Callable[..., Reply]], Callable[..., Reply]]:
    """The operation is refused (403) unless the caller's role carries `permission`."""
    def mark(operation: Callable[..., Reply]) -> Callable[..., Reply]:
        PERMISSIONS[operation.__name__] = permission

        @functools.wraps(operation)
        def checked(call: Call, *args: Any, **kwargs: Any) -> Reply:
            if permission not in call.permissions:
                raise errors.forbidden(f"Your role ({call.role_name}) does not allow this. "
                                       f"It takes a role that may {_ALLOWS[permission]}.")
            return operation(call, *args, **kwargs)
        return checked
    return mark


# ----------------------------------------------------------------------- helpers
def _body(call: Call, sql: str, **params: Any) -> Any:
    """The `body` column of the first row, or None when the query returns no row."""
    row = call.connection.execute(sql, params).fetchone()
    return None if row is None else row["body"]


def _audit(call: Call, kind: str, aggregate_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
    call.connection.execute(queries.AUDIT, {
        "type": kind, "id": aggregate_id, "event": event, "payload": json.dumps(payload or {}),
        "user": call.user_id, "request_id": call.request_id})


def _idempotent(call: Call, operation: str, body: Any, action: Callable[[], Reply]) -> Reply:
    """Run `action` once per (caller, Idempotency-Key); a repeat gets the stored answer."""
    key = idempotency_key(call.idempotency_key)
    fingerprint = request_fingerprint(operation, call.path, body)
    stored = _body(call, queries.FIND_RECEIPT, user=call.user_id, key=key)
    if stored is not None:
        if stored["requestHash"] != fingerprint:
            raise ApiError(422, "idempotency_key_reused")
        headers = stored["headers"]
        return Reply(stored["status"], stored["body"], etag=headers.get("ETag"), location=headers.get("Location"))
    reply = action()
    headers = {name: value for name, value in (("ETag", reply.etag), ("Location", reply.location)) if value}
    call.connection.execute(queries.STORE_RECEIPT, {
        "key": key, "operation": operation, "hash": fingerprint, "status": reply.status,
        "headers": json.dumps(headers), "body": json.dumps(reply.body), "user": call.user_id})
    return reply


def _page(call: Call, member: str, sql: str, item: Callable[[Any], Any], *, numeric_id: bool = False,
          **filters: Any) -> Reply:
    """One page of a list: {"<member>": [...], "nextCursor": ... or null}."""
    limit = page_limit(call.query.get("limit"))
    after_time, after_id = decode_cursor(call.query.get("cursor"), numeric_id=numeric_id)
    rows = call.connection.execute(sql, {**filters, "after_time": after_time, "after_id": after_id,
                                         "limit": limit + 1}).fetchall()
    last = rows[limit - 1] if len(rows) > limit else None
    return Reply(200, {member: [item(row) for row in rows[:limit]],
                       "nextCursor": encode_cursor(last["cursor_time"], last["cursor_id"]) if last else None})


def _one_of(call: Call, name: str, allowed: tuple[str, ...]) -> str | None:
    value = call.query.get(name)
    if value is not None and value not in allowed:
        raise errors.validation_failed(
            [issue("unknown_option", f"{name} must be one of: {', '.join(allowed)}.", field=name)])
    return value


def _uuid_query(call: Call, name: str) -> str | None:
    value = call.query.get(name)
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise errors.validation_failed([issue("invalid_type", f"{name} must be a UUID.", field=name)]) from None


# ----------------------------------------------------------------------- health, users
def health(connection: Connection) -> Reply:
    row = connection.execute(queries.HEALTH, {"spec_version": SPEC_VERSION}).fetchone()
    return Reply(200, row["body"] if row else None)


def get_me(call: Call) -> Reply:
    """The caller: the home region that is offered first when a project is created, the role and
    what it allows (`permissions`), which is what a form asks before it offers an action."""
    return conditional(_body(call, queries.GET_USER, id=call.user_id), call.if_none_match)


def update_me(call: Call, data: Any) -> Reply:
    current = _body(call, queries.GET_USER, id=call.user_id)
    require_match(call.if_match, etag_of(current), "Your profile")
    patch = validation.validate(data, validation.UPDATE_USER, ("region",))
    _check_project_references(call, {"region": patch["region"]})
    call.connection.execute(queries.UPDATE_USER_REGION, {"id": call.user_id, "region": patch["region"]})
    user = _body(call, queries.GET_USER, id=call.user_id)
    return Reply(200, user, etag=etag_of(user))


def list_users(call: Call) -> Reply:
    """The people a project can be given to (Project.Owner), with their role and home region."""
    return _page(call, "users", queries.LIST_USERS, lambda row: row["body"],
                 role=call.query.get("role") or None, region=call.query.get("region") or None,
                 q=like_pattern(call.query.get("q")))


def _person(call: Call, user_id: str, *, lock: bool = False) -> dict[str, Any]:
    found = call.connection.execute(queries.LOCK_PERSON if lock else queries.PERSON_EXISTS, {"id": user_id}).fetchone()
    if found is None:
        raise errors.not_found("The user")
    return _body(call, queries.GET_USER, id=user_id)


@needs("administer")
def get_user(call: Call, user_id: str) -> Reply:
    return conditional(_person(call, user_id), call.if_none_match)


@needs("administer")
def update_user(call: Call, user_id: str, data: Any) -> Reply:
    """Give a user a role or another home region. Nobody changes their own role."""
    current = _person(call, user_id, lock=True)
    require_match(call.if_match, etag_of(current), "The user")
    patch = validation.validate(data, validation.ADMINISTER_USER, at_least_one=True)
    issues = []
    if "role" in patch and call.connection.execute(queries.ROLE_EXISTS, {"id": patch["role"]}).fetchone() is None:
        issues.append(issue("unknown_option", "role is not an active role.", field="role", pointer="/role"))
    if "region" in patch and call.connection.execute(queries.REGION_EXISTS, {"id": patch["region"]}).fetchone() is None:
        issues.append(issue("unknown_option", "region is not an active region.", field="region", pointer="/region"))
    if issues:
        raise errors.validation_failed(issues)
    if patch.get("role", current["role"]) != current["role"]:
        if user_id == call.user_id:
            raise errors.forbidden("You cannot change your own role. Ask another administrator.")
        call.connection.execute(queries.UPDATE_USER_ROLE, {"id": user_id, "role": patch["role"]})
        _audit(call, "User", user_id, "user.role_changed", {"from": current["role"], "to": patch["role"]})
    if patch.get("region", current["region"]) != current["region"]:
        call.connection.execute(queries.UPDATE_USER_REGION, {"id": user_id, "region": patch["region"]})
        _audit(call, "User", user_id, "user.region_changed", {"from": current["region"], "to": patch["region"]})
    user = _person(call, user_id)
    return Reply(200, user, etag=etag_of(user))


# ----------------------------------------------------------------------- reference data
def roles(call: Call) -> Reply:
    """The roles a user can have, and what each allows."""
    return Reply(200, {"roles": _body(call, queries.ROLES)})


def regions(call: Call) -> Reply:
    """Project.Region: the values of createProject.region."""
    return Reply(200, {"regions": _body(call, queries.REGIONS)})


def business_purposes(call: Call) -> Reply:
    return Reply(200, {"businessPurposes": _body(call, queries.BUSINESS_PURPOSES)})


def benefits(call: Call) -> Reply:
    return Reply(200, {"benefits": _body(call, queries.BENEFITS)})


def treaties(call: Call) -> Reply:
    """The treaties a run can analyse (createRun.treaty), optionally those of one region."""
    region = call.query.get("region") or None
    if region is not None and call.connection.execute(queries.REGION_EXISTS, {"id": region}).fetchone() is None:
        raise errors.validation_failed([issue("unknown_option", "region is not an active region.", field="region")])
    return Reply(200, {"treaties": _body(call, queries.TREATIES, region=region, q=like_pattern(call.query.get("q")))})


def datasets(call: Call) -> Reply:
    """getDatasets: the datasets a run can select as its data scope (createRun.dataScope)."""
    return Reply(200, {"datasets": _body(call, queries.DATASETS)})


def run_parameters(call: Call, project_id: str) -> Reply:
    """The createRun fields of sheet 'POC Data' as they apply in a project: display name, step, type,
    control, required, and for a dropdown the values offered and the pre-populated default.

    The values are those of the project's region, business purpose and benefits; `investigation`
    narrows them further. This is the workbook's methodology profile, for one project.
    """
    scope = _body(call, queries.PROJECT_SCOPE, id=project_id)
    if scope is None:
        raise errors.not_found("The project")
    investigation = call.query.get("investigation") or None
    offered = _body(call, queries.PARAMETER_OPTIONS, region=scope["region"], business_purpose=scope["businessPurpose"],
                    benefits=json.dumps(scope["benefit"]), investigation=investigation)
    parameters = []
    for f in RUN_FIELDS:
        entry: dict[str, Any] = {
            "name": f.prop, "displayName": f.display_name, "step": f.step, "type": f.kind,
            "control": f.control, "required": f.required,
            "requiredWhen": {"prop": f.required_when[0], "equals": f.required_when[1]} if f.required_when else None,
            "defaultFrom": f.default_from, "options": None, "default": None}
        if f.control in ("select", "multiselect") and f.kind != "boolean":
            entry["options"] = offered.get(f.prop, {}).get("options", [])
            entry["default"] = offered.get(f.prop, {}).get("default")
        parameters.append(entry)
    return Reply(200, {"scope": {**scope, "investigation": investigation}, "parameters": parameters})


# ----------------------------------------------------------------------- projects
def _project(call: Call, project_id: str) -> dict[str, Any]:
    body = _body(call, queries.GET_PROJECT, id=project_id)
    if body is None:
        raise errors.not_found("The project")
    return body


def _check_project_references(call: Call, body: dict[str, Any]) -> None:
    found = _body(call, queries.CHECK_PROJECT_REFERENCES,
                  region=body.get("region"), business_purpose=body.get("businessPurpose"),
                  benefits=json.dumps(body["benefit"]) if "benefit" in body else None,
                  parent=body.get("inheritedFrom"), owner=body.get("owner"))
    issues = []
    if not found["region"]:
        issues.append(issue("unknown_option", "region is not an active region.", field="region", pointer="/region"))
    if not found["businessPurpose"]:
        issues.append(issue("unknown_option", "businessPurpose is not an active business purpose.",
                            field="businessPurpose", pointer="/businessPurpose"))
    for code in found["unknownBenefits"]:
        issues.append(issue("unknown_option", f"{code} is not an active benefit.", field="benefit",
                            pointer=f"/benefit/{body['benefit'].index(code)}"))
    if not found["inheritedFrom"]:
        issues.append(issue("unknown_option", "inheritedFrom is not an existing project.",
                            field="inheritedFrom", pointer="/inheritedFrom"))
    if not found["owner"]:
        issues.append(issue("unknown_option", "owner is not a known user.", field="owner", pointer="/owner"))
    if issues:
        raise errors.validation_failed(issues)


def list_projects(call: Call) -> Reply:
    return _page(call, "projects", queries.LIST_PROJECTS, lambda row: row["body"],
                 business_purpose=call.query.get("businessPurpose"), region=call.query.get("region"),
                 state=_one_of(call, "state", PROJECT_STATES), cycle=call.query.get("cycle"),
                 q=like_pattern(call.query.get("q")))


def get_project(call: Call, project_id: str) -> Reply:
    return conditional(_project(call, project_id), call.if_none_match)


@needs("prepare")
def create_project(call: Call, data: Any) -> Reply:
    def create() -> Reply:
        body = validation.validate(data, validation.CREATE_PROJECT, validation.CREATE_PROJECT_REQUIRED)
        validation.period_order(body.get("periodFrom"), body.get("periodTo"))
        _check_project_references(call, body)
        row = call.connection.execute(queries.INSERT_PROJECT, {
            "name": body["name"].strip(), "region": body["region"], "business_purpose": body["businessPurpose"],
            "description": body.get("description"), "cycle": body.get("cycle"),
            "period_from": body.get("periodFrom"), "period_to": body.get("periodTo"),
            "parent": body.get("inheritedFrom"), "owner": body.get("owner"), "state": body.get("state"),
            "user": call.user_id}).fetchone()
        project_id = row["id"]
        call.connection.execute(queries.INSERT_PROJECT_BENEFITS, {"id": project_id, "benefits": json.dumps(body["benefit"])})
        _audit(call, "Project", project_id, "project.created")
        project = _project(call, project_id)
        return Reply(201, project, etag=etag_of(project), location=f"{API_PREFIX}/projects/{project_id}")

    return _idempotent(call, "createProject", data, create)


@needs("prepare")
def update_project(call: Call, project_id: str, data: Any) -> Reply:
    if call.connection.execute(queries.LOCK_PROJECT, {"id": project_id}).fetchone() is None:
        raise errors.not_found("The project")
    current = _project(call, project_id)
    require_match(call.if_match, etag_of(current), "The project")
    patch = validation.validate(data, validation.UPDATE_PROJECT, at_least_one=True)
    merged = merge_patch(current, patch)
    validation.period_order(merged.get("periodFrom"), merged.get("periodTo"))
    _check_project_references(call, {name: patch[name] for name in ("benefit", "owner") if name in patch})
    call.connection.execute(queries.UPDATE_PROJECT, {
        "id": project_id, "user": call.user_id,
        "set_name": "name" in patch, "name": (patch.get("name") or "").strip() or None,
        "set_description": "description" in patch, "description": patch.get("description"),
        "set_cycle": "cycle" in patch, "cycle": patch.get("cycle"),
        "set_period_from": "periodFrom" in patch, "period_from": patch.get("periodFrom"),
        "set_period_to": "periodTo" in patch, "period_to": patch.get("periodTo"),
        "set_owner": "owner" in patch, "owner": patch.get("owner")})
    if "benefit" in patch:
        call.connection.execute(queries.DELETE_PROJECT_BENEFITS, {"id": project_id})
        call.connection.execute(queries.INSERT_PROJECT_BENEFITS, {"id": project_id, "benefits": json.dumps(patch["benefit"])})
    _audit(call, "Project", project_id, "project.updated", {"fields": sorted(patch)})
    project = _project(call, project_id)
    return Reply(200, project, etag=etag_of(project))


# ----------------------------------------------------------------------- runs
def _run(call: Call, run_id: str) -> dict[str, Any]:
    row = call.connection.execute(queries.GET_RUN, {"id": run_id}).fetchone()
    if row is None:
        raise errors.not_found("The run")
    return runconfig.represent(row["body"], row["configuration"])


def _locked_run(call: Call, run_id: str) -> dict[str, Any]:
    """Lock the run against every other writer, then check If-Match against what is there now."""
    if call.connection.execute(queries.LOCK_RUN, {"id": run_id}).fetchone() is None:
        raise errors.not_found("The run")
    current = _run(call, run_id)
    require_match(call.if_match, etag_of(current), "The run")
    return current


def _uuid_member(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(uuid.UUID(value))
    except ValueError:
        raise errors.validation_failed([issue("invalid_type", f"{name} must be a UUID.", field=name,
                                              pointer=f"/{name}")]) from None


def _apply(call: Call, run_id: str, changes: dict[str, Any]) -> None:
    """Write the changed props: one UPDATE of gea."Run", and the exclusion rows when that list changed."""
    statement, params = runconfig.update_statement(changes)
    call.connection.execute(statement, {**params, "id": run_id, "user": call.user_id})
    if "studyPeriodExclusions" in changes:
        call.connection.execute(queries.DELETE_EXCLUSIONS, {"id": run_id})
        if changes["studyPeriodExclusions"]:
            call.connection.execute(queries.INSERT_EXCLUSIONS, {
                "id": run_id, "ranges": json.dumps(changes["studyPeriodExclusions"])})


def list_runs(call: Call) -> Reply:
    return _page(call, "runs", queries.LIST_RUNS,
                 lambda row: runconfig.summarise(row["body"], row["configuration"]),
                 project_id=_uuid_query(call, "projectId"), job_id=_uuid_query(call, "jobId"),
                 status=_one_of(call, "status", RUN_STATUSES), q=like_pattern(call.query.get("q")))


def get_run(call: Call, run_id: str) -> Reply:
    return conditional(_run(call, run_id), call.if_none_match)


@needs("prepare")
def create_run(call: Call, data: Any) -> Reply:
    """createRun: the draft. Name and treaty are enough; any other prop can come with it or later.

    With cloneSourceId the configuration of that run is copied first (Run.CloneSourceId).
    """
    def create() -> Reply:
        body = runconfig.validate(data, also={"projectId": "uuid", "cloneSourceId": "uuid"},
                                  required=("projectId", "name", "treaty"))
        project_id, source = _uuid_member(body, "projectId"), _uuid_member(body, "cloneSourceId")
        if call.connection.execute(queries.PROJECT_EXISTS, {"id": project_id}).fetchone() is None:
            raise errors.validation_failed([issue("unknown_option", "projectId is not an existing project.",
                                                  field="projectId", pointer="/projectId")])
        if source is not None and call.connection.execute(queries.RUN_EXISTS, {"id": source}).fetchone() is None:
            raise errors.validation_failed([issue("unknown_option", "cloneSourceId is not an existing run.",
                                                  field="cloneSourceId", pointer="/cloneSourceId")])
        row = call.connection.execute(queries.INSERT_RUN, {
            "project_id": project_id, "name": body["name"], "treaty": body["treaty"], "source": source,
            "user": call.user_id}).fetchone()
        run_id = row["id"]
        if source is not None:
            call.connection.execute(runconfig.copy_statement(), {"id": run_id, "source": source})
            call.connection.execute(queries.COPY_EXCLUSIONS, {"id": run_id, "source": source})
        changes = {name: value for name, value in body.items()
                   if name not in ("projectId", "cloneSourceId", "name", "treaty")}
        if changes:
            _apply(call, run_id, changes)
        _audit(call, "Run", run_id, "run.created", {"cloneSourceId": source} if source else None)
        run = _run(call, run_id)
        return Reply(201, run, etag=etag_of(run), location=f"{API_PREFIX}/runs/{run_id}")

    return _idempotent(call, "createRun", data, create)


@needs("prepare")
def update_run(call: Call, run_id: str, data: Any) -> Reply:
    """Save the wizard: a JSON Merge Patch with the props that changed; null empties a prop.

    A run with required props still missing is saved and answered with 200, with the
    missing props in `issues`: saving a draft never fails because the user is not finished.
    A value of the wrong type, or a prop that does not exist, is refused (422, nothing stored).
    """
    current = _locked_run(call, run_id)
    changes = runconfig.validate(data, at_least_one=True)
    if current["status"] != "draft":
        raise errors.locked(f"Run \"{current['name']}\" is {current['status']} and its configuration is locked; "
                            "clone it instead.")
    _apply(call, run_id, changes)
    _audit(call, "Run", run_id, "run.updated", {"fields": sorted(changes)})
    run = _run(call, run_id)
    return Reply(200, run, etag=etag_of(run))


@needs("prepare")
def delete_run(call: Call, run_id: str) -> Reply:
    current = _locked_run(call, run_id)
    call.connection.execute(queries.DELETE_RUN, {"id": run_id})        # refused by trigger once submitted
    _audit(call, "Run", run_id, "run.deleted", {"name": current["name"]})
    return Reply(200, None)            # the envelope with data null: every answer has the same shape


# ----------------------------------------------------------------------- review, submit
def review_run(call: Call, run_id: str) -> Reply:
    run = _run(call, run_id)
    preview = _body(call, queries.BUILD_CONTRACT, run_id=run_id, contract_id=None, user=call.user_id)
    review = {"runId": run_id, "ready": run["ready"], "issues": run["issues"], "steps": run["steps"],
              "nextContractVersion": preview["version"],
              "preview": {name: value for name, value in preview.items() if name not in ("contractId", "createdAt")}}
    # The review carries the run's ETag: submitting with it proves nothing changed since.
    return Reply(200, review, etag=etag_of(run))


def _submit(call: Call, run_id: str) -> str:
    """Freeze the saved configuration of a ready draft into its next contract version; returns the contract id.

    The insert does the rest in the database: it verifies the hash and that the document is the
    saved configuration, queues the delivery to Snowflake and moves the run to 'queued'.
    """
    contract_id = str(uuid.uuid4())
    document = _body(call, queries.BUILD_CONTRACT, run_id=run_id, contract_id=contract_id, user=call.user_id)
    text = canonical_json(document)
    call.connection.execute(queries.INSERT_CONTRACT, {
        "contract_id": contract_id, "run_id": run_id, "project_id": document["projectId"],
        "version": document["version"], "spec_version": document["specVersion"],
        "document": text, "hash": sha256_hex(text), "user": call.user_id})
    _audit(call, "DataContract", contract_id, "contract.published", {"runId": run_id, "version": document["version"]})
    return contract_id


@needs("prepare")
def submit_run(call: Call, run_id: str) -> Reply:
    """Submit one run on its own. Snowflake starts executing when the contract arrives."""
    def submit() -> Reply:
        current = _locked_run(call, run_id)
        if current["status"] != "draft":
            raise errors.locked(f"Run \"{current['name']}\" is {current['status']} and has already been submitted.")
        if current["issues"]:
            raise ApiError(422, "not_ready", issues=current["issues"])
        contract_id = _submit(call, run_id)
        contract = _body(call, queries.GET_CONTRACT, id=contract_id)
        return Reply(201, contract, location=f"{API_PREFIX}/contracts/{contract_id}")

    return _idempotent(call, "submitRun", None, submit)


def list_run_contracts(call: Call, run_id: str) -> Reply:
    if call.connection.execute(queries.RUN_EXISTS, {"id": run_id}).fetchone() is None:
        raise errors.not_found("The run")
    return Reply(200, {"contracts": _body(call, queries.LIST_RUN_CONTRACTS, run_id=run_id)})


def get_run_contract(call: Call, run_id: str, version: int) -> Reply:
    body = _body(call, queries.GET_RUN_CONTRACT, run_id=run_id, version=version)
    if body is None:
        raise errors.not_found(f"Contract version {version} of this run")
    return Reply(200, body)


def get_contract(call: Call, contract_id: str) -> Reply:
    body = _body(call, queries.GET_CONTRACT, id=contract_id)
    if body is None:
        raise errors.not_found("The contract")
    return Reply(200, body)


# ----------------------------------------------------------------------- execution: observe and decide
def get_run_execution(call: Call, run_id: str) -> Reply:
    """Where the execution of the run's current contract is: delivery, status, every step."""
    body = _body(call, queries.GET_RUN_EXECUTION, run_id=run_id)
    if body is None:
        if call.connection.execute(queries.RUN_EXISTS, {"id": run_id}).fetchone() is None:
            raise errors.not_found("The run")
        raise ApiError(404, "not_found", "The run has not been submitted yet, so there is no execution.")
    return conditional(body, call.if_none_match)


def run_logs(call: Call, run_id: str) -> Reply:
    if call.connection.execute(queries.RUN_EXISTS, {"id": run_id}).fetchone() is None:
        raise errors.not_found("The run")
    return _page(call, "logs", queries.LIST_RUN_LOGS, lambda row: row["body"], numeric_id=True, run_id=run_id)


@needs("prepare")
def cancel_run(call: Call, run_id: str) -> Reply:
    """Cancel an execution. Not sent yet: withdrawn, the run fails at once. Under way: a request
    that the relay passes to Snowflake, which stops before its next step."""
    current = _locked_run(call, run_id)
    if current["status"] not in ("queued", "running"):
        raise errors.invalid_state(
            f"Only a run that is queued or running can be cancelled; this run is {current['status']}.")
    outcome = call.connection.execute(queries.CANCEL_RUN, {"id": run_id, "user": call.user_id}).fetchone()["outcome"]
    _audit(call, "Run", run_id, "run.cancelled" if outcome == "cancelled" else "run.cancel_requested")
    run = _run(call, run_id)
    return Reply(200, run, etag=etag_of(run))


@needs("prepare")
def resolve_run(call: Call, run_id: str, data: Any) -> Reply:
    """Run.ResolutionAction: return a failed run to draft to fix and run again, or mark it resolved."""
    current = _locked_run(call, run_id)
    body = validation.validate(data, validation.RESOLVE_RUN, ("action",))
    if current["status"] != "failed":
        raise errors.invalid_state(f"Only a failed run can be resolved; this run is {current['status']}.")
    note = (body.get("note") or "").strip() or None
    call.connection.execute(queries.RESOLVE_RUN, {"id": run_id, "action": body["action"], "note": note,
                                                  "user": call.user_id})
    _audit(call, "Run", run_id, "run.resolved", {"action": body["action"]})
    run = _run(call, run_id)
    return Reply(200, run, etag=etag_of(run))


# ----------------------------------------------------------------------- jobs
def _job(call: Call, job_id: str) -> dict[str, Any]:
    body = _body(call, queries.GET_JOB, id=job_id)
    if body is None:
        raise errors.not_found("The job")
    return body


def list_jobs(call: Call) -> Reply:
    return _page(call, "jobs", queries.LIST_JOBS, lambda row: row["body"],
                 project_id=_uuid_query(call, "projectId"), status=_one_of(call, "status", JOB_STATUSES),
                 q=like_pattern(call.query.get("q")))


def get_job(call: Call, job_id: str) -> Reply:
    return conditional(_job(call, job_id), call.if_none_match)


@needs("prepare")
def create_job(call: Call, data: Any) -> Reply:
    """Submit several draft runs together, all or none.

    The order of `runIds` is the order of execution and `maxParallel` how many execute at once;
    gea."ClaimContractDeliveries" enforces both when it hands the contracts to the relay.
    """
    def create() -> Reply:
        body = validation.validate(data, validation.CREATE_JOB, ("name", "runIds"))
        run_ids = [str(uuid.UUID(run_id)) for run_id in body["runIds"]]
        found = {row["id"] for row in call.connection.execute(queries.LOCK_RUNS, {"ids": json.dumps(run_ids)}).fetchall()}
        unknown = [issue("unknown_option", "This run does not exist.", field="runIds", pointer=f"/runIds/{index}")
                   for index, run_id in enumerate(run_ids) if run_id not in found]
        if unknown:
            raise errors.validation_failed(unknown)

        problems = []
        for index, run_id in enumerate(run_ids):
            run = _run(call, run_id)
            if run["status"] != "draft":
                problems.append(issue("not_draft", f"Run \"{run['name']}\" is {run['status']}; only a draft can be submitted.",
                                      field="runIds", pointer=f"/runIds/{index}"))
            problems += [issue(i["code"], f"Run \"{run['name']}\": {i['message']}", field=i["field"],
                               step_key=i["stepKey"], pointer=f"/runIds/{index}") for i in run["issues"]]
        if problems:
            raise ApiError(422, "not_ready", "Every run of a job has to be a complete draft.", issues=problems)

        job_id = call.connection.execute(queries.INSERT_JOB, {
            "name": body["name"].strip(), "note": (body.get("note") or "").strip() or None,
            "max_parallel": body.get("maxParallel"), "user": call.user_id, "ids": json.dumps(run_ids)}).fetchone()["id"]
        for ordinal, run_id in enumerate(run_ids, 1):
            call.connection.execute(queries.ASSIGN_JOB, {"job_id": job_id, "ordinal": ordinal, "id": run_id,
                                                         "user": call.user_id})
            _submit(call, run_id)
        _audit(call, "Job", job_id, "job.submitted", {"runIds": run_ids, "maxParallel": body.get("maxParallel")})
        job = _job(call, job_id)
        return Reply(201, job, etag=etag_of(job), location=f"{API_PREFIX}/jobs/{job_id}")

    return _idempotent(call, "createJob", data, create)


# ----------------------------------------------------------------------- operations
@needs("administer")
def retry_delivery(call: Call, contract_id: str) -> Reply:
    """Send a contract to Snowflake again after its delivery gave up."""
    delivery = _body(call, queries.GET_DELIVERY, id=contract_id)
    if delivery is None:
        raise errors.not_found("The contract")
    retried = call.connection.execute(queries.RETRY_DELIVERY, {"id": contract_id}).fetchone()
    if not retried["retried"]:
        raise errors.invalid_state(f"Only a failed delivery can be retried; this one is {delivery['status']}.")
    _audit(call, "DataContract", contract_id, "delivery.retried", {"target": "snowflake"})
    return Reply(202, _body(call, queries.GET_DELIVERY, id=contract_id))
