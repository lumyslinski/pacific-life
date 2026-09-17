"""Core creates an immutable model. Later processes only create variation revisions."""
import copy
from datetime import datetime, timezone
import uuid

from .catalog import ConfigCatalog, worker_bindings
from .models import FrozenSnapshot
from .repository import Conflict, Repository
from .worker import CalculationWorker, ModelError, amount


def now():
    return datetime.now(timezone.utc).isoformat()


class ModelLifecycle:
    def __init__(self, database):
        self.db = database
        self.repo = Repository(database)
        self.configs = ConfigCatalog(database)

    def calculate(self, run_id, settings, policies, baseline=None):
        stage = {"calculation": settings["processName"], "region": settings["region"]}
        result = CalculationWorker(self.db).run(run_id, f"{settings['configId']}:{settings['revision']}", policies,
            [stage], {(stage["calculation"], stage["region"]): worker_bindings(settings)},
            settings["objective"]["sqlName"], objective_baseline=baseline)
        return {"results": result["results"], "iterations": result["iterations"],
                "databaseFunctionQueries": list(self.db.function_queries),
                "databaseFunctionQueryCount": len(self.db.function_queries)}

    def core(self, data):
        settings = self.configs.get(data["configId"], data["configRevision"])
        if settings["kind"] != "core":
            raise ModelError("Core Calculation requires a core configuration.")
        if "bindingOverrides" in data or "parameterEdits" in data:
            raise ModelError("Publish a new core configuration before changing Core Calculation.")
        policies = data["policies"]
        model_id = "core_" + uuid.uuid4().hex
        calculation = self.calculate(data["requestId"], settings, policies)
        document = FrozenSnapshot.create({"modelId": model_id, "kind": "core", "immutable": True,
            "revision": 1, "createdAt": now(), "settings": settings, "input": policies,
            "calculationStatus": "calculated", "objectiveBaseline": "input", **calculation}).to_dict()
        self.repo.save_core(document)
        self.repo.event(model_id, 1, "CoreCalculated", document)
        return document

    def configure_variation(self, current, data):
        settings = copy.deepcopy(current)
        if "configId" in data:
            settings = self.configs.get(data["configId"], data["configRevision"])
            if settings["kind"] != "variation":
                raise ModelError("Select a variation configuration or inherit the core functions without a configId.")
        settings.pop("contentHash", None)
        settings["kind"] = "variation"
        if settings["processName"] == "Core Calculation":
            settings["processName"] = "Regional Calculation"
        settings["processName"] = data.get("processName", settings["processName"])
        name = settings["processName"]
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or name.strip().casefold() == "core calculation":
            raise ModelError("Choose a process name other than the reserved Core Calculation.")
        settings["region"] = data.get("region", settings["region"])
        if not isinstance(settings["region"], str) or not settings["region"] or len(settings["region"]) > 100:
            raise ModelError("region must be a non-empty string up to 100 characters.")
        bindings = {b["parameter"]: b for b in settings["bindings"]}
        overrides = data.get("bindingOverrides", [])
        if len({b["parameter"] for b in overrides}) != len(overrides):
            raise ModelError("Duplicate parameter overrides.")
        for override in overrides:
            parameter = override["parameter"]
            if override.get("remove") is True:
                bindings.pop(parameter, None)
                continue
            base = copy.deepcopy(bindings.get(parameter, {}))
            base.update(override)
            base.pop("function", None)
            bindings[parameter] = base
        settings["bindings"] = self.configs.resolve_bindings(list(bindings.values()))
        return settings

    def edit_parameters(self, policies, edits):
        policies = copy.deepcopy(policies)
        by_id = {p["policyId"]: p for p in policies}
        for policy_id, parameters in edits.items():
            if policy_id not in by_id or not set(parameters) <= set(by_id[policy_id]["parameters"]):
                raise ModelError("Edits must address existing policy IDs and parameter names.")
            by_id[policy_id]["parameters"].update({k: format(amount(v), ".2f") for k, v in parameters.items()})
        return policies

    def fork(self, data):
        core = self.repo.core(data["coreModelId"])
        source = core
        if data.get("parentVariationId"):
            if not data.get("parentRevision"):
                raise ModelError("Pin parentRevision when forking from another variation.")
            source = self.repo.variation(data["parentVariationId"], data["parentRevision"])
            if source["coreModelId"] != core["modelId"]:
                raise ModelError("Parent variation belongs to a different core model.")
        settings = self.configure_variation(source["settings"], data)
        results = self.edit_parameters(source["results"], data.get("parameterEdits", {}))
        document = FrozenSnapshot.create({"modelId": "variation_" + uuid.uuid4().hex, "kind": "variation",
            "immutable": False, "revision": 1, "createdAt": now(), "coreModelId": core["modelId"],
            "coreContentHash": core["contentHash"], "source": {"modelId": source["modelId"], "revision": source["revision"],
                "contentHash": source["contentHash"]}, "settings": settings, "results": results,
            "calculationStatus": "draft", "objectiveBaseline": "core", "iterations": [],
            "databaseFunctionQueries": [], "databaseFunctionQueryCount": 0}).to_dict()
        self.repo.save_variation(document)
        self.repo.event(document["modelId"], 1, "VariationCreated", document)
        return document

    def change(self, model_id, data, calculate=False):
        if calculate and set(data) - {"requestId", "expectedRevision"}:
            raise ModelError("Edit variation values/settings with PATCH before calculating its new revision.")
        previous = self.repo.variation(model_id)
        if type(data["expectedRevision"]) is not int or data["expectedRevision"] != previous["revision"]:
            raise Conflict("Stale variation revision; reload before editing or calculating.")
        document = copy.deepcopy(previous)
        document.update(revision=previous["revision"] + 1, updatedAt=now(), previousContentHash=previous["contentHash"])
        if calculate:
            core = self.repo.core(document["coreModelId"])
            baseline = {p["policyId"]: p["parameters"] for p in core["results"]}
            document.update(self.calculate(data["requestId"], document["settings"], document["results"], baseline))
            document["calculationStatus"] = "calculated"
        else:
            document["settings"] = self.configure_variation(document["settings"], data)
            document["results"] = self.edit_parameters(document["results"], data.get("parameterEdits", {}))
            document.update(calculationStatus="draft", iterations=[], databaseFunctionQueries=[], databaseFunctionQueryCount=0)
        document = FrozenSnapshot.create(document).to_dict()
        self.repo.save_variation(document, expected_revision=previous["revision"])
        self.repo.event(model_id, document["revision"], "VariationCalculated" if calculate else "VariationEdited", document)
        return document
