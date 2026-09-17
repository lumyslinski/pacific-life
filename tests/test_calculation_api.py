from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import copy
import json
from pathlib import Path
import socket
from threading import Thread
from time import monotonic, sleep
import urllib.request
import urllib.error
import uuid

import pytest
import snowflake.connector
from snowflake.connector.errors import ProgrammingError
import uvicorn

from calculation_api.app import create_app
from calculation_api.bootstrap import seed
from calculation_api.models import FrozenSnapshot
from calculation_api.gateway import SnowflakeGateway

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"policyId": "P-1001", "parameters": {"Death": "250000.00", "AccidentalDeath": "300000.00",
    "TotalPermanentDisability": "200000.00", "CriticalIllness": "150000.00"}}


@contextmanager
def running_calculation_api(options):
    app = create_app(options)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = monotonic() + 10
    try:
        while not server.started:
            if not thread.is_alive() or monotonic() > deadline:
                raise RuntimeError("Calculation API did not start.")
            sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Calculation API did not stop.")


def request_json(url, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def api(emulator):
    with snowflake.connector.connect(**emulator) as connection:
        seed(connection)
        seed(connection)
    with running_calculation_api(emulator) as url:
        yield url


def call(api, path, method="GET", data=None, status=200):
    code, response = request_json(api + path, method, data)
    assert code == status, response
    return response


def test_canonical_openapi_document_is_served(api):
    with urllib.request.urlopen(api + "/openapi.yaml", timeout=10) as response:
        document = response.read().decode()
    assert response.headers.get_content_type() == "application/yaml"
    assert "openapi: 3.1.0" in document
    assert "/core-models:" in document and "/variations/{modelId}/calculate:" in document


def core_request(**extra):
    return {"requestId": uuid.uuid4().hex, "configId": "CORE_INSURANCE", "configRevision": 1,
            "policies": [copy.deepcopy(POLICY)], **extra}


def fork(api, core, config="REGION_A", **extra):
    return call(api, "/variations", "POST", {"requestId": uuid.uuid4().hex, "coreModelId": core["modelId"],
        "configId": config, "configRevision": 1, **extra}, status=201)


def calculate(api, variation, **extra):
    return call(api, f"/variations/{variation['modelId']}/calculate", "POST", {
        "requestId": uuid.uuid4().hex, "expectedRevision": variation["revision"], **extra})


def values(model):
    return model["results"][0]["parameters"]


def test_frontend_immutable_core_independent_and_chained_variations(api, emulator):
    request = core_request()
    core = call(api, "/core-models", "POST", request, status=201)
    assert core["immutable"] is True
    assert values(core) == {"Death": "250000.00", "AccidentalDeath": "250000.00",
        "TotalPermanentDisability": "200000.00", "CriticalIllness": "125000.00"}
    assert core["databaseFunctionQueryCount"] == 3  # two dependency waves + objective
    assert call(api, "/core-models", "POST", request, status=201) == core
    call(api, "/core-models", "POST", {**request, "configRevision": 2}, status=409)
    call(api, f"/core-models/{core['modelId']}", "PATCH", {}, status=405)

    a_draft = fork(api, core)
    a = calculate(api, a_draft)
    assert values(a) == {"Death": "200000.00", "AccidentalDeath": "150000.00",
        "TotalPermanentDisability": "200000.00", "CriticalIllness": "100000.00"}
    independent_b = calculate(api, fork(api, core, "REGION_B"))
    assert values(independent_b)["Death"] == "250000.00"
    chained_b = calculate(api, fork(api, core, "REGION_B", parentVariationId=a["modelId"], parentRevision=a["revision"]))
    assert values(chained_b) == {"Death": "200000.00", "AccidentalDeath": "100000.00",
        "TotalPermanentDisability": "150000.00", "CriticalIllness": "100000.00"}
    edit_data = {"requestId": uuid.uuid4().hex, "expectedRevision": a["revision"],
        "parameterEdits": {"P-1001": {"Death": "180000.00"}}}
    edited = call(api, f"/variations/{a['modelId']}", "PATCH", edit_data)
    assert edited["revision"] == 3 and values(edited)["Death"] == "180000.00"
    assert call(api, f"/variations/{a['modelId']}", "PATCH", edit_data) == edited
    call(api, f"/variations/{a['modelId']}", "PATCH", {**edit_data, "requestId": uuid.uuid4().hex}, status=409)
    assert call(api, f"/variations/{a['modelId']}/revisions/2") == a
    assert call(api, f"/core-models/{core['modelId']}") == core
    audit = call(api, f"/audit/{a['modelId']}")["events"]
    assert [x["type"] for x in audit] == ["VariationCreated", "VariationCalculated", "VariationEdited"]
    assert audit[1]["payload"]["iterations"][0]["before"] == values(core)
    assert len(call(api, f"/audit/{a['modelId']}?after_revision=1&limit=1")["events"]) == 1
    with snowflake.connector.connect(**emulator) as c:
        for sql in ["UPDATE INSURANCE.CALC.CORE_MODELS SET DOCUMENT_JSON='{}'",
                    "DELETE FROM INSURANCE.CALC.CORE_MODELS", "DROP TABLE INSURANCE.CALC.CORE_MODELS",
                    "DELETE FROM INSURANCE.CALC.VARIATION_REVISIONS", "DELETE FROM INSURANCE.CALC.AUDIT_EVENTS"]:
            with pytest.raises(ProgrammingError, match="append-only"):
                c.cursor().execute(sql)


def test_edit_preview_publish_custom_function_and_pin_versions(api, emulator):
    definition = {"arguments": [{"name": "AMOUNT", "type": "NUMBER(18,2)"}, {"name": "FACTOR", "type": "NUMBER(18,8)"}],
                  "returns": "NUMBER(18,2)", "body": "ROUND(AMOUNT * FACTOR, 2)"}
    draft = call(api, "/functions/CUSTOM_CI", "PUT", {"expectedRevision": 0, "definition": definition})
    preview = call(api, "/functions/CUSTOM_CI/preview", "POST", {"expectedRevision": 1, "arguments": ["125000.00", "0.90"]})
    assert preview["value"] == "112500.00" and preview["queryId"]
    release1 = call(api, "/functions/CUSTOM_CI/publish", "POST", {"expectedRevision": 1})
    core = call(api, "/core-models", "POST", core_request(), status=201)
    overrides = [{"parameter": "CriticalIllness", "functionId": "CUSTOM_CI", "functionRevision": 1,
        "arguments": [{"param": "CriticalIllness"}, {"constant": "factor"}], "constants": {"factor": "0.90"}, "dependsOn": []}]
    variation = fork(api, core, bindingOverrides=overrides)
    calculated = calculate(api, variation)
    assert values(calculated)["CriticalIllness"] == "112500.00"
    assert values(calculated)["TotalPermanentDisability"] == values(core)["TotalPermanentDisability"]
    call(api, "/functions/CUSTOM_CI", "PUT", {"expectedRevision": 1, "definition": {**definition, "body": "ROUND(AMOUNT * FACTOR, 2) - 1000"}})
    release2 = call(api, "/functions/CUSTOM_CI/publish", "POST", {"expectedRevision": 2})
    assert release1["sqlName"] != release2["sqlName"]
    old_again = calculate(api, fork(api, core, bindingOverrides=overrides))
    assert values(old_again)["CriticalIllness"] == "112500.00"
    new_overrides = copy.deepcopy(overrides)
    new_overrides[0]["functionRevision"] = 2
    new_result = calculate(api, fork(api, core, bindingOverrides=new_overrides))
    assert values(new_result)["CriticalIllness"] == "111500.00"
    assert call(api, f"/core-models/{core['modelId']}") == core
    # A custom-named process reuses the core functions when no configId is given.
    reuse = call(api, "/variations", "POST", {"requestId": uuid.uuid4().hex, "coreModelId": core["modelId"],
        "processName": "Scenario Calculation", "region": "C"}, status=201)
    assert values(calculate(api, reuse)) == values(core)
    with snowflake.connector.connect(**emulator) as c:
        with pytest.raises(ProgrammingError, match="immutable"):
            c.cursor().execute(f"CREATE OR REPLACE FUNCTION {release1['sqlName']}(AMOUNT NUMBER(18,2),FACTOR NUMBER(18,8)) RETURNS NUMBER(18,2) LANGUAGE SQL AS $$ 0 $$")
        with pytest.raises(ProgrammingError, match="cannot be dropped"):
            c.cursor().execute(f"DROP FUNCTION {release1['sqlName']}")


def test_invalid_core_or_calculation_does_not_commit_partial_results(api, emulator):
    bad = core_request()
    bad["policies"][0]["parameters"]["UnknownParameter"] = "100.00"
    call(api, "/core-models", "POST", bad, status=422)
    with snowflake.connector.connect(**emulator) as c:
        for table in ["CORE_MODELS", "AUDIT_EVENTS", "COMMAND_RESULTS", "AUDIT_OUTBOX"]:
            assert c.cursor().execute(f"SELECT COUNT(*) FROM INSURANCE.CALC.{table}").fetchone() == (0,)
    core = call(api, "/core-models", "POST", core_request(), status=201)
    bad_fn = {"arguments": [{"name": "AMOUNT", "type": "NUMBER(18,2)"}], "returns": "NUMBER(18,2)", "body": "-1"}
    call(api, "/functions/BAD_AMOUNT", "PUT", {"expectedRevision": 0, "definition": bad_fn})
    call(api, "/functions/BAD_AMOUNT/publish", "POST", {"expectedRevision": 1})
    variation = fork(api, core, bindingOverrides=[{"parameter": "CriticalIllness", "functionId": "BAD_AMOUNT", "functionRevision": 1,
        "arguments": [{"param": "CriticalIllness"}], "constants": {}, "dependsOn": []}])
    call(api, f"/variations/{variation['modelId']}/calculate", "POST", {"requestId": uuid.uuid4().hex, "expectedRevision": 1}, status=422)
    assert call(api, f"/variations/{variation['modelId']}") == variation
    assert len(call(api, f"/audit/{variation['modelId']}")["events"]) == 1
    call(api, "/simulations", "POST", {}, status=410)
    call(api, "/functions/BAD_AMOUNT", "PUT", {"expectedRevision": 1, "definition": {**bad_fn, "body": "RANDOM()"}}, status=422)
    call(api, "/functions/BAD_AMOUNT", "PUT", {"expectedRevision": 1, "definition": {**bad_fn, "body": "SELECT 1"}}, status=422)


def test_frozen_snapshot_never_shares_nested_mutable_values():
    source = {"results": [{"parameters": {"Death": "100.00"}}]}
    frozen = FrozenSnapshot.create(source)
    source["results"][0]["parameters"]["Death"] = "0.00"
    first = frozen.to_dict()
    first["results"][0]["parameters"]["Death"] = "0.00"
    assert frozen.to_dict()["results"][0]["parameters"]["Death"] == "100.00"
    with pytest.raises(FrozenInstanceError):
        frozen.document_json = "{}"


def test_edit_core_function_and_config_only_changes_new_core_models(api):
    first = call(api, "/core-models", "POST", core_request(), status=201)
    draft = call(api, "/functions/CORE_LIMIT_AMOUNT")
    changed_definition = {**draft["definition"], "body": "ROUND(LEAST(AMOUNT, CAP) * 0.90, 2)"}
    call(api, "/functions/CORE_LIMIT_AMOUNT", "PUT", {"expectedRevision": 1, "definition": changed_definition})
    call(api, "/functions/CORE_LIMIT_AMOUNT/publish", "POST", {"expectedRevision": 2})
    cfg = call(api, "/configs/CORE_INSURANCE/revisions/1")
    bindings = copy.deepcopy(cfg["bindings"])
    next(b for b in bindings if b["parameter"] == "Death")["functionRevision"] = 2
    data = {"expectedRevision": 1, "kind": "core", "bindings": bindings,
        "objective": {"functionId": "OBJECTIVE_RELATIVE_CHANGE", "functionRevision": 1}}
    config2 = call(api, "/configs/CORE_INSURANCE/revisions", "POST", data, status=201)
    assert config2["revision"] == 2
    second = call(api, "/core-models", "POST", core_request(configRevision=2), status=201)
    assert values(second)["Death"] == "225000.00"
    assert values(second)["CriticalIllness"] == "112500.00"
    pinned = call(api, "/core-models", "POST", core_request(), status=201)
    assert values(pinned) == values(first)
    assert call(api, f"/core-models/{first['modelId']}") == first
    call(api, "/configs/CORE_INSURANCE/revisions", "POST", data, status=409)


def test_concurrent_edit_has_one_winner(api):
    from concurrent.futures import ThreadPoolExecutor
    core = call(api, "/core-models", "POST", core_request(), status=201)
    variation = fork(api, core)
    def edit(value):
        return request_json(api + f"/variations/{variation['modelId']}", "PATCH", {
            "requestId": uuid.uuid4().hex, "expectedRevision": 1,
            "parameterEdits": {"P-1001": {"Death": value}}})[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["180000.00", "190000.00"])) == [200, 409]
    assert len(call(api, f"/audit/{variation['modelId']}")["events"]) == 2


def test_100_policies_use_batched_function_queries(api):
    policies = [{**copy.deepcopy(POLICY), "policyId": f"P-{i:04d}"} for i in range(100)]
    result = call(api, "/core-models", "POST", core_request(policies=policies), status=201)
    assert len(result["results"]) == 100
    assert {p["policyId"] for p in result["results"]} == {p["policyId"] for p in policies}
    assert all(p["parameters"]["CriticalIllness"] == "125000.00" for p in result["results"])
    assert result["databaseFunctionQueryCount"] == 5
    assert sum(q["invocations"] for q in result["databaseFunctionQueries"]) == 800


def test_custom_function_can_compose_a_pinned_sql_release(api):
    definition = {"arguments": [{"name": "AMOUNT", "type": "NUMBER(18,2)"},
        {"name": "CAP", "type": "NUMBER(18,2)"}], "returns": "NUMBER(18,2)",
        "body": "INSURANCE.RELEASES.CORE_LIMIT_AMOUNT_R1(AMOUNT, CAP) * 0.90"}
    draft = call(api, "/functions/COMPOSED_CAP", "PUT", {"expectedRevision": 0, "definition": definition})
    assert draft["definition"]["dependencies"][0]["sqlName"] == "INSURANCE.RELEASES.CORE_LIMIT_AMOUNT_R1"
    preview = call(api, "/functions/COMPOSED_CAP/preview", "POST",
        {"expectedRevision": 1, "arguments": ["125000.00", "100000.00"]})
    assert preview["value"] == "90000.00"
