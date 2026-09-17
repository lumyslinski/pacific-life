"""Snowflake adapter: retrieves DB bindings, invokes DB SQL functions, saves audit."""
from decimal import Decimal
import json
import re

from .contracts import Binding
from .worker import ModelError


def qualified(name):
    parts = name.split(".")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"[A-Z_][A-Z0-9_]*", p) for p in parts):
        raise ModelError("Invalid published database function identifier.")
    return ".".join('"' + p + '"' for p in parts)


class SnowflakeGateway:
    def __init__(self, connection, batch_size=256):
        self.connection = connection
        self.batch_size = batch_size
        self.function_queries = []

    def rows(self, sql, params=()):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def load(self, version):
        rows = self.rows("""SELECT CALCULATION,REGION,PARAMETER,FUNCTION_NAME,
            ARGS_JSON,CONSTANTS_JSON,DEPENDS_JSON FROM INSURANCE.CALC.PARAMETER_BINDINGS
            WHERE MODEL_VERSION=%s ORDER BY CALCULATION,REGION,PARAMETER""", (version,))
        if not rows:
            raise ModelError(f"Unknown model version: {version}")
        groups = {}
        for calculation, region, param, function, args, constants, depends in rows:
            groups.setdefault((calculation, region), []).append(Binding(
                param, function, json.loads(args), json.loads(constants), json.loads(depends)))
        objective = self.rows("SELECT FUNCTION_NAME FROM INSURANCE.CALC.MODEL_OBJECTIVES WHERE MODEL_VERSION=%s", (version,))
        if len(objective) != 1:
            raise ModelError("Model must have one objective function binding.")
        return groups, objective[0][0]

    def execute_batch(self, function_name, calls):
        results = {}
        for offset in range(0, len(calls), self.batch_size):
            chunk = calls[offset:offset + self.batch_size]
            arity = len(chunk[0].arguments)
            if any(len(c.arguments) != arity for c in chunk):
                raise ModelError("All calls in a function batch must use the same signature.")
            columns = ["CALL_ID"] + [f"A{i}" for i in range(arity)]
            row = "(" + ",".join(["%s"] * len(columns)) + ")"
            sql = (f"SELECT B.CALL_ID, {qualified(function_name)}(" +
                   ",".join(f"B.A{i}" for i in range(arity)) + ") AS RESULT FROM (VALUES " +
                   ",".join([row] * len(chunk)) + ") AS B(" + ",".join(columns) + ")")
            params = [v for c in chunk for v in [c.call_id, *c.arguments]]
            with self.connection.cursor() as cursor:
                cursor.execute(sql, params)
                returned = cursor.fetchall()
                if len(returned) != len(chunk) or {r[0] for r in returned} != {c.call_id for c in chunk}:
                    raise ModelError("Database returned incorrect function result IDs.")
                for call_id, value in returned:
                    results[call_id] = value
                self.function_queries.append({"queryId": cursor.sfqid, "function": function_name,
                                               "invocations": len(chunk)})
        return results

    def objective(self, function_name, baseline, current):
        # The objective formula AND aggregation execute in the database.
        totals = {}
        # Keep all parameters of each policy together, avoiding Python aggregation.
        policies = list(current)
        for offset in range(0, len(policies), 50):
            ids = set(policies[offset:offset + 50])
            chunk = [(p, baseline[p][k], current[p][k]) for p in ids for k in current[p]]
            sql = (f"SELECT B.POLICY_ID, SUM({qualified(function_name)}(B.BASE_VALUE,B.NEW_VALUE)) "
                   "FROM (VALUES " + ",".join(["(%s,%s,%s)"] * len(chunk)) +
                   ") AS B(POLICY_ID,BASE_VALUE,NEW_VALUE) GROUP BY B.POLICY_ID")
            with self.connection.cursor() as cursor:
                cursor.execute(sql, [v for c in chunk for v in c])
                returned = cursor.fetchall()
                if {r[0] for r in returned} != ids:
                    raise ModelError("Objective results do not match the input policies.")
                totals.update(returned)
                self.function_queries.append({"queryId": cursor.sfqid, "function": function_name,
                                               "invocations": len(chunk)})
        return totals

    def audit(self, records):
        for offset in range(0, len(records), self.batch_size):
            chunk = records[offset:offset + self.batch_size]
            sql = "INSERT INTO INSURANCE.CALC.CALCULATION_AUDIT VALUES " + ",".join(["(%s,%s,%s,%s)"] * len(chunk))
            params = [v for r in chunk for v in [r["runId"], r["policyId"], r["iteration"], json.dumps(r)]]
            self.rows(sql, params)
