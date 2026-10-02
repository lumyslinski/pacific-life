"""The run and its configuration props.

A run has one column per createRun.* prop of workbook sheet 'POC Data'
(gea_api/fields.py is that list). This module is the only place that goes from
props to columns and back:

    validate()          a request body -> the props to change, each checked against its type
    update_statement()  those props -> one UPDATE of gea."Run"
    represent()         a row -> the run as the API returns it, with what is still missing
"""
from __future__ import annotations

import datetime
import json
from typing import Any

from .errors import bad_request, issue, validation_failed
from .fields import CONFIG_STEPS, RUN_CONFIG, RUN_FIELD, RUN_FIELDS, STEPS, Field

MAX_NAME = 200
_SQL_TYPE = {"string": "text", "boolean": "boolean"}
# Columns that carry the configuration, for copying it from a clone source.
CONFIG_COLUMNS: tuple[str, ...] = tuple(column for f in RUN_CONFIG for column in f.columns)


# ----------------------------------------------------------------------- input
def _date(value: Any) -> bool:
    try:
        if not isinstance(value, str) or len(value) != 10:
            return False
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _range_problem(value: Any, name: str) -> str | None:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        return f"{name} must be an object with start and end."
    if not _date(value["start"]) or not _date(value["end"]):
        return f"{name} needs start and end as dates written YYYY-MM-DD."
    if value["start"] > value["end"]:
        return f"{name} must not end before it starts."
    return None


def _check(f: Field, value: Any) -> tuple[str, str, str] | None:
    """(issue code, message, pointer suffix) for a value that does not fit the prop, or None."""
    name = f.display_name
    if f.kind == "string":
        if not isinstance(value, str):
            return "invalid_type", f"{name} must be text.", ""
        if not value.strip():
            return "required", f"{name} must not be empty; send null to clear it.", ""
        if f.prop == "name" and len(value) > MAX_NAME:
            return "invalid", f"{name} must be at most {MAX_NAME} characters.", ""
    elif f.kind == "boolean":
        if not isinstance(value, bool):
            return "invalid_type", f"{name} must be true or false.", ""
    elif f.kind == "stringArray":
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            return "invalid_type", f"{name} must be a list of values.", ""
        if len(set(value)) != len(value):
            return "invalid", f"{name} must not repeat a value.", ""
    elif f.kind == "dateRange":
        problem = _range_problem(value, name)
        if problem:
            return "invalid_type", problem, ""
    elif f.kind == "dateRangeList":
        if not isinstance(value, list):
            return "invalid_type", f"{name} must be a list of date ranges.", ""
        for index, item in enumerate(value):
            problem = _range_problem(item, f"{name} {index + 1}")
            if problem:
                return "invalid_type", problem, f"/{index}"
    return None


def validate(body: Any, *, also: dict[str, str] | None = None, required: tuple[str, ...] = (),
             at_least_one: bool = False) -> dict[str, Any]:
    """The props of a createRun or merge-patch body, checked. Raises a 422 that lists every issue.

    `also` names members that are not run props (projectId, cloneSourceId) with their type.
    An empty list is the same as null: the prop is not set.
    """
    if not isinstance(body, dict):
        raise bad_request("The request body must be a JSON object.")
    also = also or {}
    issues = [issue("unknown_field", f"{name} is not a prop of a run.", field=name, pointer=f"/{name}")
              for name in body if name not in RUN_FIELD and name not in also]
    issues += [issue("required", f"{name} is required.", field=name, pointer=f"/{name}")
               for name in required if name not in body]
    cleaned: dict[str, Any] = {}
    for name, value in body.items():
        f = RUN_FIELD.get(name)
        if f is None:
            if name in also:
                cleaned[name] = value
            continue
        if value is None:
            if f.step == "main":
                issues.append(issue("required", f"{f.display_name} cannot be empty.", field=name, pointer=f"/{name}",
                                    step_key=f.step))
            cleaned[name] = None
            continue
        problem = _check(f, value)
        if problem:
            issues.append(issue(problem[0], problem[1], field=name, step_key=f.step, pointer=f"/{name}{problem[2]}"))
            continue
        if f.kind == "string":
            value = value.strip()
        elif f.kind in ("stringArray", "dateRangeList") and not value:
            value = None
        cleaned[name] = value
    if at_least_one and not body:
        issues.append(issue("required", "Send at least one prop to change."))
    if issues:
        raise validation_failed(issues)
    return cleaned


# ----------------------------------------------------------------------- props -> columns
def update_statement(changes: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One UPDATE of gea."Run" for the given props. Column names come from fields.py, values are parameters."""
    assignments, params = [], {}
    for prop, value in changes.items():
        f = RUN_FIELD.get(prop)
        if f is None or f.kind == "dateRangeList":
            continue                                  # not a column: handled by the caller
        if f.kind == "dateRange":
            start, end = f.columns
            assignments += [f'"{start}" = %({prop}_start)s::date', f'"{end}" = %({prop}_end)s::date']
            params[f"{prop}_start"] = value["start"] if value else None
            params[f"{prop}_end"] = value["end"] if value else None
        elif f.kind == "stringArray":
            assignments.append(f'"{f.columns[0]}" = (SELECT array_agg(v.value ORDER BY v.ordinality) FROM '
                               f"jsonb_array_elements_text(%({prop})s::jsonb) WITH ORDINALITY AS v(value, ordinality))")
            params[prop] = json.dumps(value) if value is not None else None
        else:
            assignments.append(f'"{f.columns[0]}" = %({prop})s::{_SQL_TYPE[f.kind]}')
            params[prop] = value
    assignments.append('"UpdatedBy" = %(user)s::uuid')
    return f'UPDATE gea."Run" SET {", ".join(assignments)} WHERE "Id" = %(id)s::uuid', params


def copy_statement() -> str:
    """Copy the configuration columns of one run into another (Run.CloneSourceId)."""
    columns = ", ".join(f'"{column}"' for column in CONFIG_COLUMNS)
    return (f'UPDATE gea."Run" t SET ({columns}) = (SELECT {columns} FROM gea."Run" s WHERE s."Id" = %(source)s::uuid) '
            f'WHERE t."Id" = %(id)s::uuid')


# ----------------------------------------------------------------------- row -> representation
def _empty(value: Any) -> bool:
    return value is None or value == [] or value == ""


def issues_of(configuration: dict[str, Any]) -> list[dict[str, str]]:
    """What still has to be filled in before the run can be submitted, in workbook order."""
    found = []
    for f in RUN_CONFIG:
        if not _empty(configuration.get(f.prop)):
            continue
        if f.required:
            found.append(issue("required", f"{f.display_name} is required.", field=f.prop, step_key=f.step,
                               pointer=f"/{f.prop}"))
        elif f.required_when and configuration.get(f.required_when[0]) == f.required_when[1]:
            other = RUN_FIELD[f.required_when[0]].display_name
            found.append(issue("required", f"{f.display_name} is required when {other} is set.", field=f.prop,
                               step_key=f.step, pointer=f"/{f.prop}"))
    return found


def _steps(issues: list[dict[str, str]]) -> list[dict[str, Any]]:
    steps = []
    for ordinal, (key, title) in enumerate(STEPS):
        missing = [i["field"] for i in issues if i.get("stepKey") == key]
        steps.append({"key": key, "title": title, "ordinal": ordinal,
                      "status": "incomplete" if missing else "complete", "missing": missing})
    return steps


def represent(body: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    """The run as the API returns it: identity, every createRun prop (null when empty), and what is missing."""
    config = {f.prop: configuration.get(f.prop) for f in RUN_CONFIG}
    issues = issues_of(config)
    return {
        "id": body["id"], "projectId": body["projectId"], "name": body["name"], "treaty": body["treaty"],
        "region": body["region"], "status": body["status"], "locked": body["locked"],
        "cloneSourceId": body["cloneSourceId"],
        **config,
        "steps": _steps(issues), "issues": issues, "ready": body["status"] == "draft" and not issues,
        "currentContract": body["currentContract"], "submittedAt": body["submittedAt"],
        "submittedBy": body["submittedBy"], "failureMessage": body["failureMessage"],
        "jobId": body["jobId"], "cancelRequestedAt": body["cancelRequestedAt"], "resolution": body["resolution"],
        "revision": body["revision"], "createdAt": body["createdAt"], "updatedAt": body["updatedAt"]}


def summarise(body: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    """One row of the runs list."""
    issues = issues_of(configuration)
    incomplete = {i["stepKey"] for i in issues}
    contract = body["currentContract"]
    return {
        "id": body["id"], "projectId": body["projectId"], "projectName": body["projectName"],
        "name": body["name"], "treaty": body["treaty"], "region": body["region"],
        "status": body["status"], "locked": body["locked"],
        "steps": len(STEPS), "completeSteps": len(STEPS) - len(incomplete),
        "ready": body["status"] == "draft" and not issues,
        "currentContractVersion": contract["version"] if contract else None,
        "snowflakeDelivery": contract["snowflakeDelivery"] if contract else None,
        "jobId": body["jobId"], "jobOrdinal": body["jobOrdinal"],
        "resolutionAction": body["resolution"]["action"] if body["resolution"] else None,
        "cancelRequestedAt": body["cancelRequestedAt"],
        "revision": body["revision"], "createdAt": body["createdAt"], "updatedAt": body["updatedAt"]}


__all__ = ["CONFIG_COLUMNS", "CONFIG_STEPS", "RUN_FIELDS", "copy_statement", "issues_of", "represent", "summarise",
           "update_statement", "validate"]
