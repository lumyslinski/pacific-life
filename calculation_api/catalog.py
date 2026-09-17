"""Editable SQL drafts, immutable SQL releases, and versioned parameter settings."""
import copy
import json
import re
import uuid

import sqlglot
from sqlglot import exp

from .contracts import Binding
from .gateway import qualified
from .models import canonical_json, fingerprint
from .repository import Conflict, NotFound
from .worker import ModelError


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", value):
        raise ModelError("Function IDs and argument names must be uppercase SQL identifiers (max 64 characters).")
    return value


def revision(value):
    if type(value) is not int or value < 1:
        raise ModelError("A positive integer revision is required; 'latest' is not a reproducible reference.")
    return value


def sql_type(value):
    if not isinstance(value, str) or not re.fullmatch(r"NUMBER\(\d{1,2},\d{1,2}\)|VARCHAR|BOOLEAN|INTEGER", value):
        raise ModelError("Use NUMBER(precision,scale), INTEGER, VARCHAR, or BOOLEAN.")
    if value.startswith("NUMBER"):
        precision, scale = map(int, re.findall(r"\d+", value))
        if not 0 <= scale <= precision <= 38 or precision < 1:
            raise ModelError("Invalid NUMBER precision or scale.")
    return value


class FunctionCatalog:
    def __init__(self, database):
        self.db = database
        self.cache = {}

    def validate(self, definition):
        definition = copy.deepcopy(definition)
        args = definition.get("arguments")
        if not isinstance(args, list) or len(args) > 64:
            raise ModelError("Function arguments must be an array of at most 64 typed arguments.")
        for arg in args:
            identifier(arg["name"])
            sql_type(arg["type"])
        if len({a["name"] for a in args}) != len(args):
            raise ModelError("Duplicate function argument.")
        sql_type(definition["returns"])
        body = definition["body"]
        if not isinstance(body, str) or not body.strip() or len(body) > 20000 or "$$" in body:
            raise ModelError("Supply one SQL expression, up to 20,000 characters, without dollar delimiters.")
        try:
            expressions = [x for x in sqlglot.parse(body, read="snowflake") if x is not None]
        except sqlglot.errors.SqlglotError as error:
            raise ModelError(f"Invalid SQL expression: {error}") from error
        if len(expressions) != 1:
            raise ModelError("One SQL expression is required.")
        tree = expressions[0]
        if any(isinstance(x, (exp.DDL, exp.DML, exp.Command, exp.Query, exp.Table)) for x in tree.walk()):
            raise ModelError("Versioned calculation functions use scalar expressions; pass lookup data as versioned constants.")
        names = {a["name"] for a in args}
        if any(c.table or c.name.upper() not in names for c in tree.find_all(exp.Column)):
            raise ModelError("The expression may only reference its declared argument names.")
        dependencies = []
        allowed = {"LEAST", "GREATEST", "ROUND", "ABS", "IF", "COALESCE", "NULLIF", "POW", "POWER",
                   "CAST", "TRY_CAST", "CASE", "CEIL", "FLOOR", "SQRT", "MOD"}
        for fn in tree.find_all(exp.Func):
            if isinstance(fn, exp.Anonymous):
                parent = fn.parent
                if not isinstance(parent, exp.Dot):
                    raise ModelError("Custom function dependencies must reference INSURANCE.RELEASES.<versioned_name>.")
                sql_name = parent.sql(dialect="snowflake").split("(", 1)[0].replace('"', '').upper()
                if not sql_name.startswith("INSURANCE.RELEASES."):
                    raise ModelError("Only immutable function releases can be dependencies.")
                rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.FUNCTION_RELEASES WHERE FUNCTION_NAME=%s", (sql_name,))
                if len(rows) != 1:
                    raise ModelError("Unknown published function dependency.")
                dependencies.append(json.loads(rows[0][0]))
            elif fn.sql_name() not in allowed:
                raise ModelError(f"Unsupported calculation function: {fn.sql_name()}.")
        return {"arguments": args, "returns": definition["returns"], "body": body, "dependencies": dependencies}

    def draft(self, function_id):
        rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.FUNCTION_DRAFTS WHERE FUNCTION_ID=%s", (function_id,))
        if not rows:
            raise NotFound("Function draft not found.")
        return json.loads(rows[0][0])

    def edit(self, function_id, expected_revision, definition):
        identifier(function_id)
        definition = self.validate(definition)
        rows = self.db.rows("SELECT REVISION FROM INSURANCE.CALC.FUNCTION_DRAFTS WHERE FUNCTION_ID=%s", (function_id,))
        previous = rows[0][0] if rows else 0
        if type(expected_revision) is not int or expected_revision != previous:
            raise Conflict("Function draft changed; supply its current expectedRevision (0 for creation).")
        doc = {"functionId": function_id, "revision": previous + 1, "definition": definition}
        if rows:
            updated = self.db.rows("UPDATE INSURANCE.CALC.FUNCTION_DRAFTS SET REVISION=%s,DOCUMENT_JSON=%s WHERE FUNCTION_ID=%s AND REVISION=%s",
                (doc["revision"], canonical_json(doc), function_id, previous))
            if updated[0][0] != 1:
                raise Conflict("Function draft changed concurrently.")
        else:
            self.db.rows("INSERT INTO INSURANCE.CALC.FUNCTION_DRAFTS VALUES (%s,%s,%s)", (function_id, doc["revision"], canonical_json(doc)))
        return doc

    def ddl(self, name, definition, if_not_exists=False):
        args = ",".join(f'{qualified(a["name"])} {a["type"]}' for a in definition["arguments"])
        clause = "IF NOT EXISTS " if if_not_exists else ""
        return (f"CREATE FUNCTION {clause}{qualified(name)}({args}) RETURNS {definition['returns']} "
                f"LANGUAGE SQL AS $$ {definition['body']} $$")

    def release(self, function_id, version):
        key = (identifier(function_id), revision(version))
        if key not in self.cache:
            rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.FUNCTION_RELEASES WHERE FUNCTION_ID=%s AND REVISION=%s", key)
            if not rows:
                raise NotFound(f"Unpublished function: {function_id} revision {version}.")
            self.cache[key] = json.loads(rows[0][0])
        return copy.deepcopy(self.cache[key])

    def publish(self, function_id, expected_revision):
        draft = self.draft(identifier(function_id))
        if draft["revision"] != revision(expected_revision):
            raise Conflict("The function draft has changed.")
        rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.FUNCTION_RELEASES WHERE FUNCTION_ID=%s AND REVISION=%s", (function_id, expected_revision))
        if rows:
            return json.loads(rows[0][0])
        name = f"INSURANCE.RELEASES.{function_id}_R{expected_revision}"
        definition = self.validate(draft["definition"])
        release = {**draft, "definition": definition, "sqlName": name, "definitionHash": fingerprint(definition)}
        # DDL is published before the registry insert (Snowflake DDL commits).
        # IF NOT EXISTS makes retry after a crash between these statements safe;
        # only this publisher owns INSURANCE.RELEASES in the production design.
        self.db.rows(self.ddl(name, definition, if_not_exists=True))
        self.db.rows("INSERT INTO INSURANCE.CALC.FUNCTION_RELEASES VALUES (%s,%s,%s,%s)",
                     (function_id, expected_revision, name, canonical_json(release)))
        return release

    def preview(self, function_id, expected_revision, arguments):
        draft = self.draft(function_id)
        if draft["revision"] != revision(expected_revision):
            raise Conflict("The function draft has changed.")
        if len(arguments) != len(draft["definition"]["arguments"]):
            raise ModelError("Incorrect preview argument count.")
        name = "INSURANCE.CALC.PREVIEW_" + uuid.uuid4().hex.upper()
        self.db.rows(self.ddl(name, draft["definition"]))
        try:
            with self.db.connection.cursor() as cursor:
                cursor.execute(f"SELECT {qualified(name)}(" + ",".join(["%s"] * len(arguments)) + ")", arguments)
                value = cursor.fetchone()[0]
                return {"value": None if value is None else str(value), "queryId": cursor.sfqid, "draftRevision": expected_revision}
        finally:
            self.db.rows(f"DROP FUNCTION {qualified(name)}")


class ConfigCatalog:
    def __init__(self, database):
        self.db, self.functions = database, FunctionCatalog(database)

    def resolve_bindings(self, bindings):
        result = []
        if not isinstance(bindings, list):
            raise ModelError("bindings must be an array.")
        for b in bindings:
            param = b["parameter"]
            if not isinstance(param, str) or not param or len(param) > 100:
                raise ModelError("Parameter names must contain 1..100 characters.")
            release = self.functions.release(b["functionId"], b["functionRevision"])
            args, constants, depends = b["arguments"], b.get("constants", {}), b.get("dependsOn", [])
            if len(args) != len(release["definition"]["arguments"]):
                raise ModelError(f"Function argument count mismatch for {param}.")
            if not isinstance(constants, dict) or not isinstance(depends, list) or any(not isinstance(x, str) for x in depends):
                raise ModelError("Invalid binding constants or dependencies.")
            for spec in args:
                if len(set(spec) & {"param", "constant", "literal"}) != 1:
                    raise ModelError("Each argument needs exactly one param, constant, or literal source.")
                if "constant" in spec and spec["constant"] not in constants:
                    raise ModelError(f"Missing binding constant: {spec['constant']}.")
                if "param" in spec and (not isinstance(spec["param"], str) or not spec["param"]):
                    raise ModelError("Invalid argument parameter reference.")
                if "constant" in spec and spec.get("type", "NUMBER") == "NUMBER":
                    from decimal import Decimal, InvalidOperation
                    try:
                        number = Decimal(str(constants[spec["constant"]]))
                        if not number.is_finite():
                            raise ValueError()
                    except (ValueError, InvalidOperation):
                        raise ModelError("Numeric constants must be finite decimals.")
            result.append({"parameter": param, "functionId": release["functionId"], "functionRevision": release["revision"],
                "arguments": copy.deepcopy(args), "constants": copy.deepcopy(constants), "dependsOn": list(depends),
                "function": release})
        if len({b["parameter"] for b in result}) != len(result):
            raise ModelError("Only one binding per parameter is allowed in a process.")
        # All cross-parameter reads need an explicit dependency if transformed
        # in the same pass, so evaluation never depends on batch/group order.
        selected = {b["parameter"] for b in result}
        for b in result:
            reads = {a["param"] for a in b["arguments"] if "param" in a} - {b["parameter"]}
            if (reads & selected) - set(b["dependsOn"]):
                raise ModelError("Declare dependsOn for parameters transformed earlier in this pass.")
        return result

    def get(self, config_id, version):
        rows = self.db.rows("SELECT DOCUMENT_JSON FROM INSURANCE.CALC.CONFIG_REVISIONS WHERE CONFIG_ID=%s AND REVISION=%s", (config_id, revision(version)))
        if not rows:
            raise NotFound("Configuration revision not found.")
        return json.loads(rows[0][0])

    def publish(self, config_id, data):
        identifier(config_id)
        rows = self.db.rows("SELECT MAX(REVISION) FROM INSURANCE.CALC.CONFIG_REVISIONS WHERE CONFIG_ID=%s", (config_id,))
        previous = rows[0][0] or 0
        if type(data["expectedRevision"]) is not int or data["expectedRevision"] != previous:
            raise Conflict("Configuration changed; supply its current expectedRevision.")
        kind = data["kind"]
        if kind not in {"core", "variation"}:
            raise ModelError("Configuration kind must be core or variation.")
        name = "Core Calculation" if kind == "core" else data.get("processName", "Regional Calculation")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ModelError("Invalid process name.")
        if kind == "variation" and name.strip().casefold() == "core calculation":
            raise ModelError("Core Calculation is reserved for immutable core models.")
        bindings = self.resolve_bindings(data["bindings"])
        if kind == "core" and not bindings:
            raise ModelError("A core configuration needs parameter bindings.")
        objective = self.functions.release(data["objective"]["functionId"], data["objective"]["functionRevision"])
        if len(objective["definition"]["arguments"]) != 2:
            raise ModelError("The objective requires exactly two arguments: baseline and calculated.")
        doc = {"configId": config_id, "revision": previous + 1, "kind": kind,
               "processName": name, "region": data.get("region", "*"), "bindings": bindings, "objective": objective}
        doc["contentHash"] = fingerprint(doc)
        self.db.rows("INSERT INTO INSURANCE.CALC.CONFIG_REVISIONS VALUES (%s,%s,%s)", (config_id, doc["revision"], canonical_json(doc)))
        return doc


def worker_bindings(settings):
    return [Binding(b["parameter"], b["function"]["sqlName"], b["arguments"], b["constants"], b["dependsOn"])
            for b in settings["bindings"]]
