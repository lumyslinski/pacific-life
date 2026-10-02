"""HTTP mechanics that do not depend on a web framework: ETags and preconditions,
idempotency fingerprints, merge patch, cursors and canonical JSON."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import errors

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_ENTITY_TAG = re.compile(r'(W/)?"([^"]*)"')


def canonical_json(value: Any) -> str:
    """Same canonical form as calculation_api.models.canonical_json: the text that is hashed,
    stored in gea."DataContract"."DocumentCanonical" and sent to Snowflake."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Reply:
    """What a service function answers; routes.py wraps `body` in the envelope as `data`."""
    status: int
    body: Any = None
    etag: str | None = None
    location: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


# ----------------------------------------------------------------------- request id
def request_id_from(header: str | None) -> str:
    """Keep the caller's X-Request-Id when it is well formed, otherwise make one."""
    return header if header and _REQUEST_ID.match(header) else uuid.uuid4().hex


# ----------------------------------------------------------------------- ETag
def etag_of(representation: Any) -> str:
    """Strong validator of a representation: it changes when, and only when, the JSON changes."""
    digest = hashlib.sha256(canonical_json(representation).encode("utf-8")).digest()
    return '"' + base64.urlsafe_b64encode(digest[:15]).decode("ascii") + '"'


def _tags(header: str) -> list[tuple[bool, str]]:
    return [(bool(weak), value) for weak, value in _ENTITY_TAG.findall(header)]


def require_match(if_match: str | None, current_etag: str, what: str) -> None:
    """RFC 9110 If-Match with strong comparison: 428 when absent, 412 when stale."""
    if if_match is None or not if_match.strip():
        raise errors.precondition_required()
    if if_match.strip() == "*":
        return
    current = current_etag.strip('"')
    if not any(not weak and value == current for weak, value in _tags(if_match)):
        raise errors.precondition_failed(what)


def none_match(if_none_match: str | None, current_etag: str) -> bool:
    """True when the caller already holds this representation (answer 304)."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    current = current_etag.strip('"')
    return any(value == current for _, value in _tags(if_none_match))     # weak comparison


def conditional(body: Any, if_none_match: str | None, status: int = 200) -> Reply:
    """A read: 200 with the ETag, or 304 when If-None-Match still matches."""
    tag = etag_of(body)
    if none_match(if_none_match, tag):
        return Reply(304, etag=tag)
    return Reply(status, body, etag=tag)


# ----------------------------------------------------------------------- idempotency
def idempotency_key(header: str | None) -> str:
    if header is None or not header.strip():
        raise errors.bad_request("This request creates something, so it needs an Idempotency-Key header.")
    key = header.strip()
    if len(key) > 255:
        raise errors.bad_request("Idempotency-Key is longer than 255 characters.")
    return key


def request_fingerprint(operation: str, path: str, body: Any) -> str:
    return sha256_hex(canonical_json({"operation": operation, "path": path, "body": body}))


# ----------------------------------------------------------------------- JSON Merge Patch
def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396: members of `patch` replace those of `target`; null removes a member."""
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = merge_patch(result.get(name), value)
    return result


# ----------------------------------------------------------------------- paging
def encode_cursor(created_at: str, row_id: str) -> str:
    raw = json.dumps([created_at, row_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, *, numeric_id: bool = False) -> tuple[str | None, str | None]:
    """(time, id) of the last row of the previous page. The id is a UUID, or a number for the run log."""
    if not cursor:
        return None, None
    try:
        created_at, row_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if numeric_id:
            if not isinstance(row_id, str) or not row_id.isdigit() or len(row_id) > 18:
                raise ValueError
        else:
            uuid.UUID(row_id)
        if not isinstance(created_at, str):
            raise ValueError
    except (ValueError, TypeError):
        raise errors.validation_failed(
            [errors.issue("invalid", "cursor is not a value returned by this API.", field="cursor")]) from None
    return created_at, row_id


def page_limit(raw: str | None) -> int:
    if raw is None or raw == "":
        return 50
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        raise errors.validation_failed(
            [errors.issue("invalid", "limit must be a whole number from 1 to 100.", field="limit")])
    return int(raw)


def like_pattern(search: str | None) -> str | None:
    """Escape LIKE wildcards so the user's text is matched literally."""
    if search is None or not search.strip():
        return None
    return search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
