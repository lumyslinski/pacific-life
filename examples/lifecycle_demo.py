"""Execute the real HTTP workflow and save every returned model and audit event."""
import argparse
import json
from pathlib import Path
import urllib.request
import urllib.error
import uuid


def run(base_url):
    def call(path, method="GET", data=None):
        request = urllib.request.Request(base_url + path, method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{method} {path}: {error.code} {error.read().decode()}") from error
    def command(path, **data):
        return call(path, "POST", {"requestId": uuid.uuid4().hex, **data})
    def calculate(draft):
        return command(f"/variations/{draft['modelId']}/calculate", expectedRevision=draft["revision"])

    request = json.loads(Path(__file__).with_name("request.json").read_text())
    request["requestId"] = uuid.uuid4().hex
    core = call("/core-models", "POST", request)
    region_a = calculate(command("/variations", coreModelId=core["modelId"], configId="REGION_A", configRevision=1))
    region_b = calculate(command("/variations", coreModelId=core["modelId"], configId="REGION_B", configRevision=1))
    chained_b = calculate(command("/variations", coreModelId=core["modelId"], configId="REGION_B", configRevision=1,
        parentVariationId=region_a["modelId"], parentRevision=region_a["revision"]))

    function_id = "DEMO_CUSTOM_CI_" + uuid.uuid4().hex.upper()
    definition = {"arguments": [{"name": "AMOUNT", "type": "NUMBER(18,2)"},
        {"name": "FACTOR", "type": "NUMBER(18,8)"}], "returns": "NUMBER(18,2)", "body": "ROUND(AMOUNT * FACTOR, 2)"}
    draft = call(f"/functions/{function_id}", "PUT", {"expectedRevision": 0, "definition": definition})
    preview = call(f"/functions/{function_id}/preview", "POST", {"expectedRevision": 1, "arguments": ["125000.00", "0.90"]})
    release = call(f"/functions/{function_id}/publish", "POST", {"expectedRevision": 1})
    overrides = [{"parameter": "CriticalIllness", "functionId": function_id, "functionRevision": 1,
        "arguments": [{"param": "CriticalIllness"}, {"constant": "factor"}], "constants": {"factor": "0.90"}, "dependsOn": []}]
    custom = calculate(command("/variations", coreModelId=core["modelId"], configId="REGION_A", configRevision=1,
        region="A_CUSTOM", bindingOverrides=overrides))
    edited = call(f"/variations/{region_a['modelId']}", "PATCH", {"requestId": uuid.uuid4().hex,
        "expectedRevision": region_a["revision"], "parameterEdits": {"P-1001": {"Death": "180000.00"}}})
    scenario = calculate(command("/variations", coreModelId=core["modelId"], processName="Scenario Calculation", region="C"))
    saved_core = call(f"/core-models/{core['modelId']}")
    assert saved_core == core, "Core output must remain unchanged."
    models = {"core": core, "regionA": region_a, "regionBIndependent": region_b,
              "regionBFromA": chained_b, "customRegion": custom, "regionAEdited": edited, "scenarioReusingCoreFunctions": scenario}
    return {"input": request, "customFunction": {"draft": draft, "preview": preview, "release": release},
        "models": models, "coreUnchanged": True,
        "audit": {model_id: call(f"/audit/{model_id}") for model_id in {m["modelId"] for m in models.values()}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("examples/lifecycle-result.json"))
    args = parser.parse_args()
    result = run(args.base_url.rstrip("/"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for name, model in result["models"].items():
        print(name, "revision", model["revision"], model["results"][0]["parameters"])
    print("Saved:", args.output)


if __name__ == "__main__":
    main()
