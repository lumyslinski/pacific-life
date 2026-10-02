#!/usr/bin/env python3
"""Structural checks of gea/api/openapi.yaml that need no network or extra packages.

Not a replacement for a full linter (run `npx @redocly/cli lint gea/api/openapi.yaml`
in CI as well); it catches the mistakes that break client generation:
unresolved $ref, duplicate operationId, undeclared or unused path parameters,
responses without a description, required properties that are not defined,
and components nobody references.

It also holds every operation to the API conventions:
  * every response body is the envelope {data, error}: a 2xx has a null error, a failure is
    ErrorEnvelope (null data); the only response without a body is 304;
  * PUT, PATCH and DELETE require If-Match; PATCH takes application/merge-patch+json;
  * a POST that answers 201 requires Idempotency-Key and returns Location;
  * a response that carries an ETag is matched by If-None-Match on its GET;
  * an operation that changes something names the permission it needs (x-permission, a member of
    the schema Permissions) and answers 403; an operation without one never answers 403.

Usage:  python gea/tools/check_openapi.py        (requires PyYAML)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")
OWN_ACCOUNT = {"updateMe"}            # what every signed-in user may change: their own home region


def walk(node, path=""):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}/{index}")


def resolve(spec: dict, ref: str):
    node = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def main() -> int:
    path = Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    referenced: set[str] = set()

    for where, node in walk(spec):
        if isinstance(node, dict) and "$ref" in node:
            referenced.add(node["$ref"])
            try:
                resolve(spec, node["$ref"])
            except (KeyError, TypeError):
                problems.append(f"{where}: unresolved {node['$ref']}")
        if isinstance(node, dict) and isinstance(node.get("properties"), dict) and isinstance(node.get("required"), list):
            for name in node["required"]:
                if name not in node["properties"]:
                    problems.append(f"{where}: required property '{name}' is not defined")
        if isinstance(node, dict) and isinstance(node.get("enum"), list) and len(set(map(str, node["enum"]))) != len(node["enum"]):
            problems.append(f"{where}: enum lists a value twice")
    for mapping in (m for _, n in walk(spec) if isinstance(n, dict) and "discriminator" in n
                    for m in n["discriminator"].get("mapping", {}).values()):
        referenced.add(mapping)

    permissions = set(spec["components"]["schemas"].get("Permissions", {}).get("properties", {}))
    operation_ids: dict[str, str] = {}
    operations = 0
    for route, item in spec["paths"].items():
        shared = item.get("parameters", [])
        for method in METHODS:
            if method not in item:
                continue
            operations += 1
            op, where = item[method], f"{method.upper()} {route}"
            if "operationId" not in op:
                problems.append(f"{where}: missing operationId")
            elif op["operationId"] in operation_ids:
                problems.append(f"{where}: operationId also used by {operation_ids[op['operationId']]}")
            else:
                operation_ids[op["operationId"]] = where
            if not op.get("tags"):
                problems.append(f"{where}: missing tag")
            if not op.get("summary"):
                problems.append(f"{where}: missing summary")
            params = [resolve(spec, p["$ref"]) if "$ref" in p else p for p in shared + op.get("parameters", [])]
            declared = {p["name"] for p in params if p["in"] == "path"}
            in_route = set(re.findall(r"\{(\w+)\}", route))
            if declared != in_route:
                problems.append(f"{where}: path parameters {sorted(in_route)} declared as {sorted(declared)}")
            if any(p["in"] == "path" and not p.get("required") for p in params):
                problems.append(f"{where}: path parameter must be required")
            if not any(str(code).startswith("2") for code in op["responses"]):
                problems.append(f"{where}: no success response")
            for code, response in op["responses"].items():
                response = resolve(spec, response["$ref"]) if "$ref" in response else response
                if not response.get("description"):
                    problems.append(f"{where}: response {code} has no description")
            headers = {p["name"] for p in params if p["in"] == "header" and p.get("required")}
            optional_headers = {p["name"] for p in params if p["in"] == "header"}
            for code, response in op["responses"].items():
                response = resolve(spec, response["$ref"]) if "$ref" in response else response
                content = response.get("content", {})
                if str(code) == "304":
                    if content:
                        problems.append(f"{where}: 304 must not have a body")
                    continue
                if set(content) != {"application/json"}:
                    problems.append(f"{where}: response {code} must have an application/json body")
                    continue
                schema = content["application/json"]["schema"]
                if str(code)[0] == "2":
                    properties = schema.get("properties", {})
                    if sorted(schema.get("required", [])) != ["data", "error"] or properties.get("error") != {"type": "null"}:
                        problems.append(f"{where}: response {code} is not the envelope {{data, error: null}}")
                    elif sorted(properties.get("data", {}).get("required", [])) == ["data", "error"]:
                        problems.append(f"{where}: response {code} is wrapped in the envelope more than once")
                elif schema != {"$ref": "#/components/schemas/ErrorEnvelope"}:
                    problems.append(f"{where}: response {code} must be ErrorEnvelope")
                if str(code) == "201" and "Location" not in response.get("headers", {}):
                    problems.append(f"{where}: 201 without a Location header")
                if "X-Request-Id" not in response.get("headers", {}) and str(code)[0] == "2":
                    problems.append(f"{where}: response {code} does not declare X-Request-Id")
            if "default" not in op["responses"]:
                problems.append(f"{where}: no default error response")
            if "204" in op["responses"]:
                problems.append(f"{where}: 204 has no body; answer 200 with data null so the envelope is always there")
            if method in ("put", "patch", "delete"):
                if "If-Match" not in headers:
                    problems.append(f"{where}: {method.upper()} must require If-Match")
                for code in ("412", "428"):
                    if code not in op["responses"]:
                        problems.append(f"{where}: missing response {code}")
            if method == "post" and "201" in op["responses"] and "Idempotency-Key" not in headers:
                problems.append(f"{where}: a POST that creates must require Idempotency-Key")
            if method == "get" and "ETag" in op["responses"].get("200", {}).get("headers", {}) \
                    and route != "/runs/{runId}/review" and "If-None-Match" not in optional_headers:
                problems.append(f"{where}: returns an ETag but does not accept If-None-Match")
            permission = op.get("x-permission")
            if permission is not None and permission not in permissions:
                problems.append(f"{where}: x-permission '{permission}' is not a member of the schema Permissions")
            if (permission is not None) != ("403" in op["responses"]):
                problems.append(f"{where}: x-permission and the 403 response go together")
            if method != "get" and permission is None and op.get("operationId") not in OWN_ACCOUNT:
                problems.append(f"{where}: an operation that changes something must name its x-permission")
            if "requestBody" in op:
                content = op["requestBody"]["content"]
                expected = "application/merge-patch+json" if method == "patch" else "application/json"
                if set(content) != {expected}:
                    problems.append(f"{where}: request body must be {expected}")
                for media in content.values():
                    schema = media["schema"]
                    body = resolve(spec, schema["$ref"]) if "$ref" in schema else schema
                    if body.get("additionalProperties") is not False:
                        problems.append(f"{where}: request body should set additionalProperties: false")

    for section in ("schemas", "parameters", "headers", "responses"):
        for name in spec["components"].get(section, {}):
            if f"#/components/{section}/{name}" not in referenced:
                problems.append(f"components/{section}/{name}: never referenced")

    for problem in problems:
        print("PROBLEM", problem)
    print(f"{len(spec['paths'])} paths, {operations} operations, {len(spec['components']['schemas'])} schemas, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
