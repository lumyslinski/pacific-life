"""Who is calling.

The identity provider is not decided yet (open question 7 in gea/README.md), so
the token check is a plug-in point: create_app(authenticator=...) takes any
object with `authenticate(headers) -> Caller`. Two are provided:

  DevAuthenticator   local development and tests only: trusts the X-Dev-User
                     header (default "dev") and, for a user seen for the first
                     time, X-Dev-Region and X-Dev-Role (default "admin", so
                     that a developer can do everything). Selected with
                     GEA_AUTH_MODE=dev.
  (none)             the default. The application refuses to start, so an API
                     without authentication is never deployed by accident.

What the caller may do is not the identity provider's business after the first
sight: the role is a column of gea."User" (revision 0007) and an Admin changes
it. A role is three permissions; the API asks for a permission, never for a role.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from . import errors, queries
from .db import Connection


@dataclass(frozen=True)
class Caller:
    subject: str            # stable identifier from the identity provider
    name: str
    email: str | None = None
    region: str | None = None   # home region of a user seen for the first time; 'global' when unknown
    role: str | None = None     # role of a user seen for the first time; 'viewer' when unknown


PERMISSIONS = ("prepare", "review", "administer")


@dataclass(frozen=True)
class Principal:
    """The caller as the database knows them: the user row and what its role allows."""
    user_id: str
    role: str
    role_name: str
    permissions: frozenset[str]


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Caller: ...


class DevAuthenticator:
    """Development only: the caller is whoever the X-Dev-User header names."""

    def authenticate(self, headers: Mapping[str, str]) -> Caller:
        subject = (headers.get("x-dev-user") or "dev").strip()
        if not subject or len(subject) > 200:
            raise errors.unauthorized("X-Dev-User is empty or too long.")
        return Caller(subject=f"dev:{subject}", name=subject, region=(headers.get("x-dev-region") or None),
                      role=(headers.get("x-dev-role") or "admin"))


def authenticator_for(mode: str) -> Authenticator:
    if mode == "dev":
        return DevAuthenticator()
    raise SystemExit(
        "No authenticator is configured. Pass create_app(authenticator=...) with your identity "
        "provider's token check, or set GEA_AUTH_MODE=dev for local development only.")


def ensure_user(connection: Connection, caller: Caller) -> Principal:
    """The gea."User" row of the caller with its role; created on first sight, in the caller's
    home region and with the caller's role (a Viewer when the identity provider names none)."""
    row = connection.execute(queries.ENSURE_USER, {
        "subject": caller.subject, "name": caller.name, "email": caller.email, "region": caller.region,
        "role": caller.role}).fetchone()
    assert row is not None
    return Principal(user_id=row["id"], role=row["role"], role_name=row["role_name"],
                     permissions=frozenset(name for name in PERMISSIONS if row[name]))
