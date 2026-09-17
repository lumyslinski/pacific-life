"""Compile Snowflake SQL into database SQL. No Python calculation callbacks.

Each logical Snowflake database/schema maps to a schema in one DuckDB file.
SQL UDF bodies are parsed independently and compiled to persistent SQL macros.
The HTTP layer, SQL compiler, and worker are separate execution boundaries.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from time import perf_counter
from typing import Any
import json
import re
import tempfile
import uuid

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel


DUCKDB_CONFIG = {
    "enable_external_access": False,
    # Typed SQL macros are required to emulate Snowflake scalar UDF signatures.
    "storage_compatibility_version": "v1.4.0",
}

# Local application policy, mirroring insert/select-only grants in production.
APPEND_ONLY = {"CORE_MODELS", "VARIATION_REVISIONS", "FUNCTION_RELEASES",
               "CONFIG_REVISIONS", "AUDIT_EVENTS", "COMMAND_RESULTS"}


class SqlError(Exception):
    def __init__(self, message: str, state: str = "0A000", code: str = "001003"):
        super().__init__(message)
        self.state, self.code = state, code


def ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def string(value: str) -> str:
    return exp.Literal.string(value).sql(dialect="duckdb")


def canonical(node: exp.Expression | str) -> str:
    if isinstance(node, exp.Identifier):
        return node.name if node.args.get("quoted") else node.name.upper()
    return str(node).upper()


@dataclass
class Session:
    connection: Any
    database: str | None = None
    schema: str | None = "PUBLIC"
    user: str = "LOCAL"
    warehouse: str | None = None
    role: str = "LOCAL"
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    master_token: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: int = field(default_factory=lambda: uuid.uuid4().int % (2**53))
    autocommit: bool = True
    in_transaction: bool = False
    parameters: dict = field(default_factory=dict)
    requests: OrderedDict = field(default_factory=OrderedDict)


@dataclass
class Result:
    rows: list
    columns: list
    query_id: str = ""


def status(message="Statement executed successfully.") -> Result:
    return Result([(message,)], [("status", "VARCHAR", None, None, None, None, None)])


class Engine:
    def __init__(self, path=":memory:", max_rows=20000, history_path=None):
        self.lock = RLock()
        self.max_rows = max_rows
        self.history = deque(maxlen=1000)
        self.history_path = Path(history_path) if history_path else None
        self._temporary_directory = None
        if path == ":memory:":
            # Separate connector sessions need separate DuckDB connections so
            # transaction isolation is real. A private temporary database gives
            # us shared state without exposing a durable file to the caller.
            self._temporary_directory = tempfile.TemporaryDirectory(prefix="local-snowflake-")
            path = str(Path(self._temporary_directory.name) / "emulator.duckdb")
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if self.history_path:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = duckdb.connect(path, config=DUCKDB_CONFIG)
        self.sessions: dict[str, Session] = {}
        self.root.execute("CREATE SCHEMA IF NOT EXISTS _SF")
        self.root.execute("CREATE TABLE IF NOT EXISTS _SF.DATABASES (NAME VARCHAR PRIMARY KEY)")
        self.root.execute("""CREATE TABLE IF NOT EXISTS _SF.SCHEMAS (
            DB VARCHAR, NAME VARCHAR, PHYSICAL VARCHAR UNIQUE, PRIMARY KEY (DB,NAME))""")
        self.root.execute("""CREATE TABLE IF NOT EXISTS _SF.FUNCTIONS (
            DB VARCHAR, SCHEMA_NAME VARCHAR, NAME VARCHAR, ARGS_JSON VARCHAR,
            RETURN_TYPE VARCHAR, BODY VARCHAR, DDL VARCHAR,
            PRIMARY KEY (DB,SCHEMA_NAME,NAME))""")

    def close(self):
        with self.lock:
            for s in list(self.sessions.values()):
                self.close_session(s)
            self.root.close()
            if self._temporary_directory is not None:
                self._temporary_directory.cleanup()

    def session(self, database=None, schema=None, user="LOCAL", warehouse=None, role=None, parameters=None):
        with self.lock:
            # Like unquoted connection options in Snowflake, names are uppercase.
            db = database.upper() if database else None
            sc = schema.upper() if schema else "PUBLIC"
            if db:
                self.physical(db, sc)
            # A session owns its DuckDB connection. Engine.lock serializes SQL
            # execution because this compact emulator is intended for tests.
            s = Session(duckdb.connect(self.path, config=DUCKDB_CONFIG),
                        db, sc, user, warehouse, role or "LOCAL")
            s.parameters.update(parameters or {})
            s.autocommit = bool(s.parameters.get("AUTOCOMMIT", True))
            self.sessions[s.token] = s
            return s

    def close_session(self, session):
        with self.lock:
            self.sessions.pop(session.token, None)
            session.connection.close()  # Closing rolls back an open transaction.

    def physical(self, db, schema):
        if not db or not schema:
            raise SqlError("Select a database and schema with USE first.", "3D000")
        row = self.root.execute("SELECT PHYSICAL FROM _SF.SCHEMAS WHERE DB=? AND NAME=?", [db, schema]).fetchone()
        if not row:
            raise SqlError(f"Database/schema does not exist: {db}.{schema}", "3F000")
        return row[0]

    def create_schema(self, db, schema, exists=False):
        if not self.root.execute("SELECT 1 FROM _SF.DATABASES WHERE NAME=?", [db]).fetchone():
            raise SqlError(f"Database does not exist: {db}", "3D000")
        row = self.root.execute("SELECT 1 FROM _SF.SCHEMAS WHERE DB=? AND NAME=?", [db, schema]).fetchone()
        if row:
            if not exists:
                raise SqlError(f"Schema already exists: {db}.{schema}", "42710")
            return
        physical = "S_" + sha256(json.dumps([db, schema]).encode()).hexdigest()[:24]
        self.root.execute(f"CREATE SCHEMA {ident(physical)}")
        self.root.execute("INSERT INTO _SF.SCHEMAS VALUES (?,?,?)", [db, schema, physical])

    def create_database(self, name, exists=False):
        if self.root.execute("SELECT 1 FROM _SF.DATABASES WHERE NAME=?", [name]).fetchone():
            if not exists:
                raise SqlError(f"Database already exists: {name}", "42710")
            return
        self.root.execute("INSERT INTO _SF.DATABASES VALUES (?)", [name])
        self.create_schema(name, "PUBLIC")
        self.create_schema(name, "INFORMATION_SCHEMA")
        info = ident(self.physical(name, "INFORMATION_SCHEMA"))
        self.root.execute(f"""CREATE VIEW {info}.FUNCTIONS AS
            SELECT DB AS FUNCTION_CATALOG, SCHEMA_NAME AS FUNCTION_SCHEMA,
                   NAME AS FUNCTION_NAME, ARGS_JSON AS ARGUMENT_SIGNATURE,
                   RETURN_TYPE AS DATA_TYPE, 'SQL' AS FUNCTION_LANGUAGE,
                   BODY AS FUNCTION_DEFINITION
            FROM _SF.FUNCTIONS WHERE DB={string(name)}""")
        self.root.execute(f"""CREATE VIEW {info}.SCHEMATA AS
            SELECT DB AS CATALOG_NAME, NAME AS SCHEMA_NAME FROM _SF.SCHEMAS
            WHERE DB={string(name)}""")

    def resolve_table(self, table, session):
        db = canonical(table.args["catalog"]) if table.args.get("catalog") else session.database
        sc = canonical(table.args["db"]) if table.args.get("db") else session.schema
        name = canonical(table.this)
        return db, sc, name

    def function(self, db, schema, name):
        return self.root.execute("""SELECT ARGS_JSON,RETURN_TYPE,BODY,DDL FROM _SF.FUNCTIONS
            WHERE DB=? AND SCHEMA_NAME=? AND NAME=?""", [db, schema, name]).fetchone()

    def compile(self, expression, session):
        """AST-based rewriting preserves literals and nested function arguments."""
        tree = expression.copy()
        ctes = {canonical(c.args["alias"].this) for c in tree.find_all(exp.CTE)}

        def function_call(node, qualifiers):
            name = canonical(node.this)
            if len(qualifiers) == 2:
                db, sc = qualifiers
            elif len(qualifiers) == 1:
                db, sc = session.database, qualifiers[0]
            else:
                db, sc = session.database, session.schema
            found = self.function(db, sc, name)
            if not found:
                raise SqlError(f"Unknown SQL function: {db}.{sc}.{name}", "42883")
            args = json.loads(found[0])
            if len(args) != len(node.expressions):
                raise SqlError(f"Function {name} requires {len(args)} arguments.", "07001")
            # Snowflake coerces arguments to a SQL UDF signature. DuckDB typed
            # macros require explicit casts, especially for connector Decimal
            # values serialized as strings inside a VALUES relation.
            call = exp.Anonymous(this=exp.to_identifier(name, quoted=True),
                expressions=[exp.Cast(this=visit(a), to=exp.DataType.build(t, dialect="duckdb"))
                             for a, (_, t) in zip(node.expressions, args)])
            return exp.Dot(this=exp.to_identifier(self.physical(db, sc), quoted=True), expression=call)

        def qualifiers(node):
            if isinstance(node, exp.Identifier):
                return [canonical(node)]
            if isinstance(node, exp.Dot):
                return qualifiers(node.this) + qualifiers(node.expression)
            raise SqlError("Unsupported function qualification.")

        def visit(node):
            if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Anonymous):
                return function_call(node.expression, qualifiers(node.this))
            if isinstance(node, exp.Anonymous):
                return function_call(node, [])
            if isinstance(node, exp.CurrentDatabase):
                return exp.Literal.string(session.database) if session.database else exp.Null()
            if isinstance(node, exp.CurrentSchema):
                return exp.Literal.string(session.schema) if session.schema else exp.Null()
            if isinstance(node, exp.CurrentUser):
                return exp.Literal.string(session.user)
            if isinstance(node, exp.Identifier):
                return exp.to_identifier(canonical(node), quoted=True)
            for key, value in list(node.args.items()):
                if isinstance(value, exp.Expression):
                    node.set(key, visit(value))
                elif isinstance(value, list):
                    node.set(key, [visit(v) if isinstance(v, exp.Expression) else v for v in value])
            if isinstance(node, exp.Table):
                if not isinstance(node.this, exp.Identifier):
                    raise SqlError("Table functions are not supported in v0.1.")
                if not node.args.get("db") and not node.args.get("catalog") and node.name in ctes:
                    return node
                db, sc, _ = self.resolve_table(node, session)
                node.set("catalog", None)
                node.set("db", exp.to_identifier(self.physical(db, sc), quoted=True))
            if isinstance(node, exp.Column) and node.args.get("db"):
                # `alias.column` is common in VALUES/table expressions. Only
                # rewrite a column qualifier when it is demonstrably a
                # database/schema qualifier; otherwise preserve the alias.
                db = canonical(node.args["catalog"]) if node.args.get("catalog") else session.database
                qualifier = canonical(node.args["db"])
                is_schema = bool(node.args.get("catalog")) or bool(
                    db and self.root.execute(
                        "SELECT 1 FROM _SF.SCHEMAS WHERE DB=? AND NAME=?", [db, qualifier]
                    ).fetchone()
                )
                if is_schema:
                    node.set("catalog", None)
                    node.set("db", exp.to_identifier(self.physical(db, qualifier), quoted=True))
            return node

        return visit(tree).sql(dialect="duckdb", unsupported_level=ErrorLevel.RAISE)

    def create_function(self, node, session, original_sql):
        if session.in_transaction:
            raise SqlError("Publish SQL functions outside a transaction in v0.1.")
        definition = node.this
        if not isinstance(definition, exp.UserDefinedFunction):
            raise SqlError("Expected a named SQL function and typed arguments.")
        if node.args.get("exists") and node.args.get("replace"):
            raise SqlError("OR REPLACE and IF NOT EXISTS cannot be combined.")
        props = node.args.get("properties")
        props = props.expressions if props else []
        returns = next((p for p in props if isinstance(p, exp.ReturnsProperty)), None)
        language = next((p for p in props if isinstance(p, exp.LanguageProperty)), None)
        if language and language.this.name.upper() != "SQL":
            raise SqlError("Only LANGUAGE SQL is supported. No Python/JavaScript handlers are executed.")
        if not returns or returns.args.get("is_table"):
            raise SqlError("Only scalar RETURNS <type> SQL functions are supported.")
        allowed = (exp.ReturnsProperty, exp.LanguageProperty)
        if any(not isinstance(p, allowed) for p in props):
            raise SqlError("Function properties other than RETURNS and LANGUAGE SQL are unsupported.")
        db, sc, name = self.resolve_table(definition.this, session)
        physical = self.physical(db, sc)
        arguments = []
        for arg in definition.expressions:
            if not isinstance(arg, exp.ColumnDef) or arg.args.get("constraints"):
                raise SqlError("Use ordinary typed function arguments; defaults are unsupported.")
            arguments.append([canonical(arg.this), arg.args["kind"].sql(dialect="duckdb")])
        if len({a[0] for a in arguments}) != len(arguments):
            raise SqlError("Duplicate function argument names.", "42701")
        old = self.function(db, sc, name)
        if old:
            if node.args.get("exists"):
                return status("Function already exists.")
            if (db, sc) == ("INSURANCE", "RELEASES"):
                raise SqlError("Published function releases are immutable; publish a new revision.", "42501")
            if not node.args.get("replace"):
                raise SqlError(f"Function already exists: {name}", "42710")
            if json.loads(old[0]) != arguments:
                raise SqlError("Overloading/signature changes are unsupported; publish a new versioned name.")
        body_node = node.args.get("expression")
        if not isinstance(body_node, (exp.Literal, exp.RawString)):
            raise SqlError("SQL function bodies must use AS $$ ... $$ or AS ' ... '.")
        body = body_node.this
        expressions = [e for e in sqlglot.parse(body, read="snowflake") if e is not None]
        if len(expressions) != 1:
            raise SqlError("A scalar function must contain one SQL expression or SELECT.")
        parsed_body = expressions[0]
        if isinstance(parsed_body, (exp.DDL, exp.DML, exp.Command)):
            raise SqlError("SQL functions cannot contain DDL/DML statements.")
        fn_session = Session(session.connection, db, sc, session.user)
        compiled = self.compile(parsed_body, fn_session)
        if isinstance(parsed_body, exp.Query):
            compiled = "(" + compiled + ")"
        return_type = returns.this.sql(dialect="duckdb")
        arg_sql = ", ".join(f"{ident(n)} {t}" for n, t in arguments)
        macro = f"CREATE OR REPLACE MACRO {ident(physical)}.{ident(name)}({arg_sql}) AS CAST({compiled} AS {return_type})"
        # Catalog metadata and executable SQL definition commit together.
        self.root.execute("BEGIN")
        try:
            self.root.execute(macro)
            self.root.execute("DELETE FROM _SF.FUNCTIONS WHERE DB=? AND SCHEMA_NAME=? AND NAME=?", [db, sc, name])
            self.root.execute("INSERT INTO _SF.FUNCTIONS VALUES (?,?,?,?,?,?,?)",
                              [db, sc, name, json.dumps(arguments), return_type, body, original_sql])
            self.root.execute("COMMIT")
        except Exception:
            self.root.execute("ROLLBACK")
            raise
        return status(f"Function {name} successfully created.")

    def execute(self, session, sql):
        with self.lock:
            query_id = str(uuid.uuid4())
            started = perf_counter()
            entry = {"query_id": query_id, "session_id": session.session_id, "sql": sql,
                     "database": session.database, "schema": session.schema,
                     "query_tag": session.parameters.get("QUERY_TAG"),
                     "started_at": datetime.now(timezone.utc).isoformat()}
            try:
                statements = [s for s in sqlglot.parse(sql, read="snowflake") if s is not None]
                if len(statements) != 1:
                    raise SqlError("Send one SQL statement per execute() call.")
                result = self._execute(session, statements[0], sql)
                result.query_id = query_id
                entry.update(status="SUCCEEDED", row_count=len(result.rows))
                return result
            except Exception as error:
                entry.update(status="FAILED", error=str(error))
                if isinstance(error, SqlError):
                    error.query_id = query_id
                    raise
                state = "42883" if isinstance(error, duckdb.CatalogException) else "42000"
                converted = SqlError(str(error), state)
                converted.query_id = query_id
                raise converted from error
            finally:
                entry["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
                self.history.append(entry)
                if self.history_path:
                    with self.history_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(entry) + "\n")

    def _execute(self, session, node, sql):
        conn = session.connection
        if isinstance(node, exp.Use):
            kind = node.args["kind"].name.upper()
            if kind == "DATABASE":
                db = canonical(node.this.this)
                self.physical(db, "PUBLIC")
                session.database, session.schema = db, "PUBLIC"
            elif kind == "SCHEMA":
                db = canonical(node.this.args["db"]) if node.this.args.get("db") else session.database
                sc = canonical(node.this.this)
                self.physical(db, sc)
                session.database, session.schema = db, sc
            elif kind == "WAREHOUSE":
                session.warehouse = canonical(node.this.this)  # Logical label only.
            else:
                raise SqlError(f"USE {kind} is unsupported.")
            return status()
        if isinstance(node, (exp.Transaction, exp.Commit, exp.Rollback)):
            if isinstance(node, exp.Transaction):
                if session.in_transaction:
                    raise SqlError("Nested transactions are unsupported.")
                conn.execute("BEGIN")
                session.in_transaction = True
            elif session.in_transaction:
                conn.execute("COMMIT" if isinstance(node, exp.Commit) else "ROLLBACK")
                session.in_transaction = False
            return status()
        if isinstance(node, exp.Alter) and node.args.get("kind") == "SESSION":
            for action in node.args["actions"]:
                if not isinstance(action, exp.AlterSession) or action.args.get("unset"):
                    raise SqlError("Only ALTER SESSION SET is supported.")
                for item in action.expressions:
                    key = item.this.this.name.upper()
                    value = item.this.expression
                    if key not in {"QUERY_TAG", "AUTOCOMMIT"}:
                        raise SqlError(f"Unsupported session parameter: {key}")
                    val = value.this
                    if key == "AUTOCOMMIT":
                        if not isinstance(value, exp.Boolean):
                            raise SqlError("AUTOCOMMIT requires TRUE or FALSE.")
                        if val and session.in_transaction:
                            conn.execute("COMMIT")
                            session.in_transaction = False
                        session.autocommit = val
                    session.parameters[key] = val
            return status()
        if isinstance(node, exp.Create):
            kind = node.args.get("kind", "").upper()
            if kind in {"DATABASE", "SCHEMA"}:
                if session.in_transaction or node.args.get("replace"):
                    raise SqlError("Database/schema creation requires autocommit and does not support OR REPLACE.")
                if kind == "DATABASE":
                    self.create_database(canonical(node.this.this), node.args.get("exists"))
                else:
                    db = canonical(node.this.args["catalog"]) if node.this.args.get("catalog") else session.database
                    self.create_schema(db, canonical(node.this.args["db"]), node.args.get("exists"))
                return status()
            if kind == "FUNCTION":
                return self.create_function(node, session, sql)
            if kind not in {"TABLE", "VIEW"}:
                raise SqlError(f"CREATE {kind} is unsupported.")
        if isinstance(node, exp.Drop) and node.args.get("kind") == "FUNCTION":
            if session.in_transaction:
                raise SqlError("DROP FUNCTION inside a transaction is unsupported.")
            db, sc, name = self.resolve_table(node.this, session)
            if (db, sc) == ("INSURANCE", "RELEASES"):
                raise SqlError("Published function releases cannot be dropped.", "42501")
            old = self.function(db, sc, name)
            if not old:
                if node.args.get("exists"):
                    return status()
                raise SqlError(f"Unknown SQL function: {name}", "42883")
            # There is one signature per function name; a supplied signature must match.
            signature = node.args.get("expressions") or []
            if signature and [x.sql(dialect="duckdb") for x in signature] != [x[1] for x in json.loads(old[0])]:
                raise SqlError("Function signature does not match.", "42883")
            self.root.execute("BEGIN")
            try:
                self.root.execute(f"DROP MACRO {ident(self.physical(db, sc))}.{ident(name)}")
                self.root.execute("DELETE FROM _SF.FUNCTIONS WHERE DB=? AND SCHEMA_NAME=? AND NAME=?", [db, sc, name])
                self.root.execute("COMMIT")
            except Exception:
                self.root.execute("ROLLBACK")
                raise
            return status()
        allowed = (exp.Query, exp.Create, exp.Insert, exp.Update, exp.Delete, exp.Drop)
        if not isinstance(node, allowed):
            raise SqlError(f"Unsupported SQL statement: {type(node).__name__}")
        if isinstance(node, exp.Drop) and node.args.get("kind") not in {"TABLE", "VIEW"}:
            raise SqlError("Only DROP TABLE, VIEW, and FUNCTION are supported.")
        target = node.this if not isinstance(node, exp.Query) else None
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table):
            db, sc, name = self.resolve_table(target, session)
            changes_existing = isinstance(node, (exp.Update, exp.Delete, exp.Drop)) or (
                isinstance(node, exp.Create) and node.args.get("replace")) or (
                isinstance(node, exp.Insert) and (node.args.get("overwrite") or node.args.get("conflict")))
            if (db, sc) == ("INSURANCE", "CALC") and name in APPEND_ONLY and changes_existing:
                raise SqlError(f"{name} is append-only; create another model/revision.", "42501")
        compiled = self.compile(node, session)
        if not session.autocommit and not session.in_transaction:
            conn.execute("BEGIN")
            session.in_transaction = True
        conn.execute(compiled)
        # DDL returns a status; DML retains DuckDB's affected-row count so
        # optimistic updates can verify that exactly one revision changed.
        if isinstance(node, (exp.Create, exp.Drop)):
            return status()
        columns = conn.description or []
        rows = conn.fetchmany(self.max_rows + 1)
        if len(rows) > self.max_rows:
            raise SqlError(f"Result exceeds the local {self.max_rows}-row limit; use smaller batches.")
        return Result(rows, columns)
