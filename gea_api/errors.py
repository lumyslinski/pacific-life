"""The one error object of the API.

Every response body is the same envelope, `{"data": ..., "error": ...}`: exactly
one of the two is null. Whatever goes wrong (validation, a database rule, a
stale ETag, an unexpected exception) leaves the API as the same error object
under `error`, with the matching HTTP status, so a client has one shape to handle:

    {"data": null,
     "error": {"code": "validation_failed", "message": "2 fields are not valid.", "status": 422,
               "issues": [{"field": "...", "code": "...", "message": "...", "pointer": "/..."}],
               "requestId": "..."}}
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("gea_api")

# Every code of the contract (schema ErrorCode) with the message used when none is given.
MESSAGES = {
    "bad_request": "The request could not be read.",
    "unauthorized": "Sign in to continue.",
    "forbidden": "You are not allowed to do this.",
    "not_found": "This does not exist.",
    "locked": "This is locked and cannot be changed.",
    "invalid_state": "This is not possible in the current state.",
    "precondition_failed": "This was changed after you loaded it. Reload it and apply your change again.",
    "precondition_required": "Send the ETag of the resource in the If-Match header.",
    "idempotency_key_reused": "This Idempotency-Key was already used for a different request. Use a new key.",
    "validation_failed": "Some fields are not valid.",
    "not_ready": "Complete every step before submitting the run.",
    "internal_error": "Something went wrong on our side. Quote the request id to support.",
    "service_unavailable": "The service is busy. Try again in a moment.",
}



def issue(code: str, message: str, *, field: str | None = None, step_key: str | None = None,
          pointer: str | None = None) -> dict[str, str]:
    """One problem with one prop; the same object in a run, a review and an error."""
    entry = {"code": code, "message": message}
    if field is not None:
        entry["field"] = field
    if step_key is not None:
        entry["stepKey"] = step_key
    if pointer is not None:
        entry["pointer"] = pointer
    return entry


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, *,
                 issues: list[dict[str, str]] | None = None, headers: dict[str, str] | None = None):
        self.status, self.code = status, code
        self.message = message or MESSAGES[code]
        self.issues = issues or []
        self.headers = headers or {}
        super().__init__(self.message)

    def body(self, *, request_id: str) -> dict[str, Any]:
        """The value of `error` in the response envelope."""
        return {"code": self.code, "message": self.message, "status": self.status,
                "issues": self.issues, "requestId": request_id}


def bad_request(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def unauthorized(message: str = "Send a valid bearer token.") -> ApiError:
    return ApiError(401, "unauthorized", message, headers={"WWW-Authenticate": "Bearer"})


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", message)


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} does not exist.")


def locked(message: str) -> ApiError:
    return ApiError(409, "locked", message)


def invalid_state(message: str) -> ApiError:
    return ApiError(409, "invalid_state", message)


def precondition_failed(what: str) -> ApiError:
    return ApiError(412, "precondition_failed",
                   f"{what} was changed after you loaded it. Reload it and apply your change again.")


def precondition_required() -> ApiError:
    return ApiError(428, "precondition_required")


def validation_failed(issues: list[dict[str, str]], message: str | None = None) -> ApiError:
    if message is None:
        message = issues[0]["message"] if len(issues) == 1 else f"{len(issues)} fields are not valid."
    return ApiError(422, "validation_failed", message, issues=issues)


def _message(error: Any) -> str:
    return getattr(getattr(error, "diag", None), "message_primary", None) or str(error)


def from_database_error(error: Any) -> ApiError:
    """Translate a PostgreSQL error into an ApiError by SQLSTATE; no message parsing.

    The schema raises its own SQLSTATE values for business rules (GEA03..GEA06);
    see gea/db/postgres/migrations/sql/0001_baseline.up.sql and 0007_user_roles.up.sql.
    """
    state = getattr(error, "sqlstate", None) or ""
    constraint = getattr(getattr(error, "diag", None), "constraint_name", None)

    if state == "GEA03":
        return locked(_sentence(_message(error)))
    if state == "GEA04":
        return invalid_state(_sentence(_message(error)))
    if state == "GEA05":
        return precondition_failed("The run")
    if state == "GEA06":
        return forbidden(_sentence(_message(error)))
    if state == "23514" and constraint == "CK_Run_RequiredWhenSubmitted":
        # The API reports the missing props itself before it submits; this is the database's backstop.
        return ApiError(422, "not_ready")
    if state in ("23503", "23514", "23502", "22P02", "22007", "22008", "22001", "22023"):
        return validation_failed([issue("invalid", _sentence(_message(error)))])
    if state == "23505":
        return invalid_state(_sentence(_message(error)))
    if state in ("40001", "40P01", "55P03", "57014") or state[:2] in ("08", "53", "57") or not state:
        # serialisation failure, deadlock, lock or statement timeout, connection trouble: safe to retry
        log.warning("database unavailable (%s): %s", state or "no SQLSTATE", error)
        return ApiError(503, "service_unavailable", headers={"Retry-After": "2"})
    log.error("unmapped database error %s: %s", state, error)
    return ApiError(500, "internal_error")


def _sentence(message: str) -> str:
    message = message.strip()
    message = message[:1].upper() + message[1:]
    return message if message.endswith(".") else message + "."
