from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import urllib.request

import pytest
import snowflake.connector
from snowflake.connector.errors import ProgrammingError

from snowflake_emulator.testing import running_server


def test_sql_function_is_evaluated_and_replaced(connection):
    q = connection.cursor()
    q.execute("""CREATE FUNCTION INSURANCE.CALC.LIMIT_V1(x NUMBER(18,2), cap NUMBER(18,2))
                 RETURNS NUMBER(18,2) LANGUAGE SQL AS $$ LEAST(x,cap) $$""")
    assert q.execute("SELECT INSURANCE.CALC.LIMIT_V1(%s,%s)", (350000, 300000)).fetchone() == (Decimal("300000.00"),)
    assert q.sfqid
    q.execute("""CREATE OR REPLACE FUNCTION LIMIT_V1(x NUMBER(18,2), cap NUMBER(18,2))
                 RETURNS NUMBER(18,2) LANGUAGE SQL AS 'LEAST(x,cap) - 1'""")
    assert q.execute("SELECT LIMIT_V1(350000,300000)").fetchone() == (Decimal("299999.00"),)
    metadata = q.execute("""SELECT FUNCTION_LANGUAGE,FUNCTION_DEFINITION FROM
        INSURANCE.INFORMATION_SCHEMA.FUNCTIONS WHERE FUNCTION_NAME='LIMIT_V1'""").fetchone()
    assert metadata == ("SQL", "LEAST(x,cap) - 1")


def test_dependency_sql_function_and_lookup_table(connection):
    q = connection.cursor()
    q.execute("CREATE TABLE FACTORS(REGION VARCHAR, FACTOR NUMBER(18,4))")
    q.execute("INSERT INTO FACTORS VALUES ('A',0.9),('B',0.8)")
    q.execute("""CREATE FUNCTION REGION_FACTOR(r VARCHAR) RETURNS NUMBER(18,4) LANGUAGE SQL
                 AS $$ SELECT FACTOR FROM INSURANCE.CALC.FACTORS WHERE REGION=r $$""")
    q.execute("""CREATE FUNCTION APPLY_FACTOR(x NUMBER(18,2), r VARCHAR) RETURNS NUMBER(18,2)
                 LANGUAGE SQL AS $$ ROUND(x * INSURANCE.CALC.REGION_FACTOR(r),2) $$""")
    assert q.execute("SELECT APPLY_FACTOR(%s,%s)", (Decimal("200.50"), "A")).fetchone() == (Decimal("180.45"),)
    q.execute("UPDATE FACTORS SET FACTOR=0.75 WHERE REGION='A'")
    assert q.execute("SELECT APPLY_FACTOR(200.50,'A')").fetchone() == (Decimal("150.38"),)


def test_null_decimal_and_string_handling(connection):
    q = connection.cursor()
    q.execute("CREATE FUNCTION MONEY(x NUMBER(18,3)) RETURNS NUMBER(18,2) LANGUAGE SQL AS $$ ROUND(x,2) $$")
    assert q.execute("SELECT MONEY(1.005), MONEY(-1.005), LEAST(NULL,1), GREATEST(NULL,2)").fetchone() == (
        Decimal("1.01"), Decimal("-1.01"), None, None)
    q.execute("CREATE FUNCTION ECHO_VALUE(x VARCHAR) RETURNS VARCHAR LANGUAGE SQL AS $$ x $$")
    value = "O'Brien; DROP TABLE FACTORS; -- x? $$ \\"
    assert q.execute("SELECT ECHO_VALUE(%s)", (value,)).fetchone() == (value,)
    assert q.execute("SELECT ECHO_VALUE(NULL)").fetchone() == (None,)


def test_qmark_binding(emulator, connection):
    connection.cursor().execute("CREATE FUNCTION ADD_VALUES(x NUMBER(18,2), y NUMBER(18,2)) RETURNS NUMBER(18,2) LANGUAGE SQL AS $$ x+y $$")
    c = snowflake.connector.connect(**emulator, database="INSURANCE", schema="CALC", paramstyle="qmark")
    try:
        assert c.cursor().execute("SELECT ADD_VALUES(?,?), '?'", (Decimal("12.50"), Decimal("0.25"))).fetchone() == (
            Decimal("12.75"), "?")
        assert c.cursor().execute("SELECT ?", ("'; SELECT 999; --",)).fetchone() == ("'; SELECT 999; --",)
    finally:
        c.close()


def test_transaction_isolation_and_rollback(connection, emulator):
    q = connection.cursor()
    q.execute("CREATE TABLE TX_ROWS(N INTEGER)")
    other = snowflake.connector.connect(**emulator, database="INSURANCE", schema="CALC")
    try:
        q.execute("BEGIN")
        q.execute("INSERT INTO TX_ROWS VALUES (1)")
        assert q.execute("SELECT COUNT(*) FROM TX_ROWS").fetchone() == (1,)
        assert other.cursor().execute("SELECT COUNT(*) FROM TX_ROWS").fetchone() == (0,)
        connection.rollback()
        assert q.execute("SELECT COUNT(*) FROM TX_ROWS").fetchone() == (0,)
        connection.autocommit(False)
        q.execute("INSERT INTO TX_ROWS VALUES (2)")
        connection.commit()
        assert other.cursor().execute("SELECT COUNT(*) FROM TX_ROWS").fetchone() == (1,)
        connection.autocommit(True)
    finally:
        other.close()


def test_database_schema_and_qualified_calls(connection):
    q = connection.cursor()
    q.execute("CREATE DATABASE SECOND")
    q.execute("CREATE SCHEMA SECOND.CALC")
    q.execute("CREATE FUNCTION INSURANCE.CALC.F(x INTEGER) RETURNS INTEGER LANGUAGE SQL AS $$ x+1 $$")
    q.execute("CREATE FUNCTION SECOND.CALC.F(x INTEGER) RETURNS INTEGER LANGUAGE SQL AS $$ x+2 $$")
    assert q.execute("SELECT INSURANCE.CALC.F(10),SECOND.CALC.F(10)").fetchone() == (11, 12)
    q.execute("USE SCHEMA SECOND.CALC")
    assert q.execute('SELECT "CALC"."F"(10), CURRENT_DATABASE(),CURRENT_SCHEMA()').fetchone() == (12, "SECOND", "CALC")


def test_persistent_database_function_and_data(tmp_path):
    database = str(tmp_path / "persistent.duckdb")
    with running_server(database) as options:
        c = snowflake.connector.connect(**options)
        for sql in ["CREATE DATABASE D", "USE DATABASE D", "CREATE TABLE T(V INTEGER)",
                    "INSERT INTO T VALUES (12)", "CREATE FUNCTION F(x INTEGER) RETURNS INTEGER LANGUAGE SQL AS $$ x*2 $$"]:
            c.cursor().execute(sql)
        c.close()
    with running_server(database) as options:
        c = snowflake.connector.connect(**options, database="D", schema="PUBLIC")
        assert c.cursor().execute("SELECT F(V) FROM T").fetchone() == (24,)
        c.close()


@pytest.mark.parametrize("sql", [
    "CREATE FUNCTION BAD(x INTEGER) RETURNS INTEGER LANGUAGE PYTHON AS $$ x $$",
    "CREATE FUNCTION BAD(x INTEGER) RETURNS TABLE(y INTEGER) LANGUAGE SQL AS $$ SELECT x $$",
    "CREATE PROCEDURE BAD() RETURNS INTEGER LANGUAGE SQL AS $$ BEGIN RETURN 1; END $$",
    "SELECT DOES_NOT_EXIST(1)",
    "SELECT 1; SELECT 2",
    "ATTACH '/tmp/outside.duckdb' AS OTHER",
])
def test_unsupported_features_return_connector_errors(connection, sql):
    with pytest.raises(ProgrammingError):
        connection.cursor().execute(sql)


def test_concurrent_clients_share_functions(connection, emulator):
    connection.cursor().execute("CREATE FUNCTION F(x INTEGER) RETURNS INTEGER LANGUAGE SQL AS $$ x*3 $$")
    def calculate(n):
        c = snowflake.connector.connect(**emulator, database="INSURANCE", schema="CALC")
        try:
            return c.cursor().execute("SELECT F(%s)", (n,)).fetchone()[0]
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(calculate, range(12))) == [n * 3 for n in range(12)]


def test_query_history_has_failures_and_query_ids(connection, emulator):
    q = connection.cursor()
    q.execute("ALTER SESSION SET QUERY_TAG='frontend-test'")
    q.execute("SELECT 42")
    query_id = q.sfqid
    with pytest.raises(ProgrammingError):
        q.execute("SELECT MISSING_FUNCTION(1)")
    url = f"http://{emulator['host']}:{emulator['port']}/_emulator/queries"
    rows = json.load(urllib.request.urlopen(url))["queries"]
    assert any(r["query_id"] == query_id and r["query_tag"] == "frontend-test" for r in rows)
    assert any(r["status"] == "FAILED" and "MISSING_FUNCTION" in r["sql"] for r in rows)
