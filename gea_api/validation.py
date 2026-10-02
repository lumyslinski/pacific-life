"""Validation of request bodies against the shapes in gea/api/openapi.yaml.

Every problem found is reported, not only the first, each with a JSON Pointer
to the member, so the form can mark all fields in one round trip. Values that
live in the database (regions, benefits, users) are checked there; this module
checks shape only. The props of a run are checked in runconfig.py.
"""
from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from .errors import bad_request, issue, validation_failed


@dataclass(frozen=True)
class Text:
    min_length: int = 1
    max_length: int | None = None
    nullable: bool = False


@dataclass(frozen=True)
class Date:
    nullable: bool = False


@dataclass(frozen=True)
class Uuid:
    nullable: bool = False


@dataclass(frozen=True)
class Choice:
    values: tuple[str, ...]


@dataclass(frozen=True)
class CodeList:
    """Non-empty array of distinct, non-empty strings."""


@dataclass(frozen=True)
class UuidList:
    """Array of distinct UUIDs."""
    min_items: int = 1
    max_items: int = 200


@dataclass(frozen=True)
class Whole:
    """A whole number in a range (true and false are not numbers)."""
    minimum: int
    maximum: int
    nullable: bool = False


Rule = Text | Date | Uuid | Choice | CodeList | UuidList | Whole

# createProject: name, region, businessPurpose and benefit are sheet 'POC Data'; the rest is '04. API Data'.
CREATE_PROJECT: dict[str, Rule] = {
    "name": Text(max_length=200), "region": Text(), "businessPurpose": Text(), "benefit": CodeList(),
    "description": Text(min_length=0, max_length=4000, nullable=True),
    "cycle": Text(min_length=0, max_length=50, nullable=True),
    "periodFrom": Date(nullable=True), "periodTo": Date(nullable=True),
    "inheritedFrom": Uuid(nullable=True), "owner": Uuid(nullable=True),
    "state": Choice(("draft", "in-progress")),
}
CREATE_PROJECT_REQUIRED = ("name", "region", "businessPurpose", "benefit")

UPDATE_PROJECT: dict[str, Rule] = {
    "name": Text(max_length=200), "benefit": CodeList(),
    "description": Text(min_length=0, max_length=4000, nullable=True),
    "cycle": Text(min_length=0, max_length=50, nullable=True),
    "periodFrom": Date(nullable=True), "periodTo": Date(nullable=True), "owner": Uuid(),
}

UPDATE_USER: dict[str, Rule] = {"region": Text()}

# What an Admin changes about another user: the role and the home region.
ADMINISTER_USER: dict[str, Rule] = {"role": Text(), "region": Text()}

# Run.ResolutionAction and Run.ResolutionNote of '03. Model Data'.
RESOLUTION_ACTIONS = ("rerun-with-fixed-config", "mark-resolved")
RESOLVE_RUN: dict[str, Rule] = {"action": Choice(RESOLUTION_ACTIONS),
                                "note": Text(min_length=0, max_length=2000, nullable=True)}

# Job.Name, Job.Note and the runs to submit, in the order they are to execute.
CREATE_JOB: dict[str, Rule] = {
    "name": Text(max_length=200), "note": Text(min_length=0, max_length=2000, nullable=True),
    "runIds": UuidList(min_items=2, max_items=200), "maxParallel": Whole(1, 100, nullable=True),
}


def _check(name: str, value: Any, rule: Rule) -> tuple[str, str] | None:
    """(issue code, message) for an invalid value, or None."""
    if value is None:
        return None if getattr(rule, "nullable", False) else ("invalid_type", f"{name} must not be null.")
    if isinstance(rule, Text):
        if not isinstance(value, str):
            return "invalid_type", f"{name} must be text."
        if len(value.strip()) < rule.min_length:
            return "required", f"{name} must not be empty."
        if rule.max_length is not None and len(value) > rule.max_length:
            return "invalid", f"{name} must be at most {rule.max_length} characters."
    elif isinstance(rule, Date):
        try:
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError
            datetime.date.fromisoformat(value)
        except ValueError:
            return "invalid_type", f"{name} must be a date written as YYYY-MM-DD."
    elif isinstance(rule, Uuid):
        try:
            if not isinstance(value, str):
                raise ValueError
            uuid.UUID(value)
        except ValueError:
            return "invalid_type", f"{name} must be a UUID."
    elif isinstance(rule, Choice):
        if value not in rule.values:
            return "unknown_option", f"{name} must be one of: {', '.join(rule.values)}."
    elif isinstance(rule, CodeList):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            return "invalid_type", f"{name} must be a list of values."
        if not value:
            return "empty", f"{name} needs at least one value."
        if len(set(value)) != len(value):
            return "invalid", f"{name} must not repeat a value."
    elif isinstance(rule, UuidList):
        try:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError
            ids = [str(uuid.UUID(item)) for item in value]
        except ValueError:
            return "invalid_type", f"{name} must be a list of UUIDs."
        if len(ids) < rule.min_items:
            return "invalid", f"{name} needs at least {rule.min_items} entries."
        if len(ids) > rule.max_items:
            return "invalid", f"{name} can have at most {rule.max_items} entries."
        if len(set(ids)) != len(ids):
            return "invalid", f"{name} must not repeat a value."
    elif isinstance(rule, Whole):
        if isinstance(value, bool) or not isinstance(value, int):
            return "invalid_type", f"{name} must be a whole number."
        if not rule.minimum <= value <= rule.maximum:
            return "invalid", f"{name} must be from {rule.minimum} to {rule.maximum}."
    return None


def validate(body: Any, rules: dict[str, Rule], required: tuple[str, ...] = (), *,
             at_least_one: bool = False) -> dict[str, Any]:
    """Return the body unchanged when it is valid; raise a 422 ApiError listing every issue."""
    if not isinstance(body, dict):
        raise bad_request("The request body must be a JSON object.")
    issues = [issue("unknown_field", f"{name} is not a field of this request.", field=name, pointer=f"/{name}")
              for name in body if name not in rules]
    issues += [issue("required", f"{name} is required.", field=name, pointer=f"/{name}")
               for name in required if name not in body]
    for name, rule in rules.items():
        if name in body:
            problem = _check(name, body[name], rule)
            if problem:
                issues.append(issue(problem[0], problem[1], field=name, pointer=f"/{name}"))
    if at_least_one and not body:
        issues.append(issue("required", "Send at least one field to change."))
    if issues:
        raise validation_failed(issues)
    return body


def period_order(period_from: str | None, period_to: str | None) -> None:
    if period_from and period_to and period_from > period_to:
        raise validation_failed([issue("invalid", "periodTo must not be before periodFrom.",
                                       field="periodTo", pointer="/periodTo")])
