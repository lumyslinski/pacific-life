"""Publish demo SQL functions and bindings. The worker never reads these files."""
from pathlib import Path
import json
import os

import snowflake.connector
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]


def connection_options():
    if os.getenv("SNOWFLAKE_CONNECTION_JSON"):
        return json.loads(os.environ["SNOWFLAKE_CONNECTION_JSON"])
    os.environ["SNOWFLAKE_DISABLE_PLATFORM_DETECTION"] = "true"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    return {"account": "local", "user": "test", "password": "test",
            "host": os.getenv("EMULATOR_HOST", "127.0.0.1"),
            "port": int(os.getenv("EMULATOR_PORT", "8084")), "protocol": "http",
            "login_timeout": 5, "network_timeout": 30,
            "platform_detection_timeout_seconds": 0,
            "session_parameters": {"CLIENT_OUT_OF_BAND_TELEMETRY_ENABLED": False}}


def seed(connection):
    for filename in ("schema.sql", "functions.sql", "lifecycle.sql"):
        script = (ROOT / "fixtures" / filename).read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            for statement in sqlglot.parse(script, read="snowflake"):
                if statement is not None:
                    cursor.execute(statement.sql(dialect="snowflake"))
    seed_lifecycle(connection)


def seed_lifecycle(connection):
    from .catalog import FunctionCatalog, ConfigCatalog
    from .gateway import SnowflakeGateway
    database = SnowflakeGateway(connection)
    functions, configs = FunctionCatalog(database), ConfigCatalog(database)
    for node in sqlglot.parse((ROOT / "fixtures" / "functions.sql").read_text(), read="snowflake"):
        if node is None:
            continue
        name = node.this.this.name.removesuffix("_V1")
        existing = database.rows("SELECT 1 FROM INSURANCE.CALC.FUNCTION_RELEASES WHERE FUNCTION_ID=%s AND REVISION=1", (name,))
        if existing:
            continue
        definition = {"arguments": [{"name": a.name.upper(), "type": a.args["kind"].sql(dialect="snowflake").replace("DECIMAL", "NUMBER").replace(" ", "")}
                                    for a in node.this.expressions],
                      "returns": next(p for p in node.args["properties"].expressions if isinstance(p, exp.ReturnsProperty)).this.sql(dialect="snowflake").replace("DECIMAL", "NUMBER").replace(" ", ""),
                      "body": node.args["expression"].this}
        drafts = database.rows("SELECT REVISION FROM INSURANCE.CALC.FUNCTION_DRAFTS WHERE FUNCTION_ID=%s", (name,))
        if not drafts:
            functions.edit(name, 0, definition)
        functions.publish(name, 1)
    raw = json.loads((ROOT / "fixtures" / "bindings.json").read_text())
    for config_id, calculation, region in [("CORE_INSURANCE", "Core Calculation", "*"),
        ("REGION_A", "Regional Calculation", "A"), ("REGION_B", "Regional Calculation", "B")]:
        if database.rows("SELECT 1 FROM INSURANCE.CALC.CONFIG_REVISIONS WHERE CONFIG_ID=%s AND REVISION=1", (config_id,)):
            continue
        bindings = [{"parameter": b["parameter"], "functionId": b["function"].split(".")[-1].removesuffix("_V1"),
            "functionRevision": 1, "arguments": b["arguments"], "constants": b["constants"], "dependsOn": b["dependsOn"]}
            for b in raw if b["calculation"] == calculation and b["region"] == region]
        configs.publish(config_id, {"expectedRevision": 0, "kind": "core" if region == "*" else "variation",
            "processName": calculation, "region": region, "bindings": bindings,
            "objective": {"functionId": "OBJECTIVE_RELATIVE_CHANGE", "functionRevision": 1}})


if __name__ == "__main__":
    with snowflake.connector.connect(**connection_options()) as connection:
        seed(connection)
    print("Demo SQL functions and database bindings are ready.")
