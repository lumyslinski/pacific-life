"""Database-independent orchestration. This module contains no model formulas."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json

from .contracts import Binding, DatabaseFunctions, Invocation


class ModelError(ValueError):
    pass


def amount(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0 or value > Decimal("9999999999999999.99"):
            raise ValueError()
        if value.as_tuple().exponent < -2:
            raise ValueError()
        return value
    except (ValueError, InvalidOperation):
        raise ModelError("Parameters must be non-negative decimal amounts with at most two decimal places.")


def serialize(state):
    return {name: format(value, ".2f") for name, value in state.items()}


class CalculationWorker:
    def __init__(self, database: DatabaseFunctions):
        self.database = database

    def run(self, run_id, model_version, policies, stages, bindings, objective_function, objective_baseline=None):
        baseline = {}
        for policy in policies:
            policy_id = policy["policyId"]
            if not policy_id or policy_id in baseline or not policy["parameters"]:
                raise ModelError("Each policy needs a unique non-empty ID and at least one parameter.")
            baseline[policy_id] = {k: amount(v) for k, v in policy["parameters"].items()}
        current = {p: values.copy() for p, values in baseline.items()}
        if objective_baseline is not None:
            baseline = {p: {k: amount(v) for k, v in state.items()} for p, state in objective_baseline.items()}
        trace = []
        # Every configured phase is validated before the first SQL calculation.
        for stage in stages:
            key = (stage["calculation"], stage["region"])
            if key not in bindings:
                raise ModelError(f"No model bindings for {key}.")
            for values in current.values():
                selected = {b.parameter: b for b in bindings[key] if b.parameter in values}
                if stage["calculation"] == "Core Calculation" and set(selected) != set(values):
                    raise ModelError(f"Missing Core Calculation bindings: {sorted(set(values) - set(selected))}")
                missing = {a["param"] for b in selected.values() for a in b.arguments
                           if "param" in a and a["param"] not in values}
                missing.update(dep for b in selected.values() for dep in b.depends_on if dep not in values)
                if missing:
                    raise ModelError(f"Missing parameter dependencies: {sorted(missing)}")
                pending = dict(selected)
                while pending:
                    ready = [k for k, b in pending.items() if not (set(b.depends_on) & pending.keys())]
                    if not ready:
                        raise ModelError("Cyclic parameter dependencies.")
                    for k in ready:
                        pending.pop(k)

        for index, stage in enumerate(stages, 1):
            before = {p: state.copy() for p, state in current.items()}
            configured = bindings[(stage["calculation"], stage["region"])]
            pending = {(p, b.parameter): b for p, values in current.items()
                       for b in configured if b.parameter in values}
            evaluated = defaultdict(list)
            while pending:
                ready = [(key, b) for key, b in pending.items()
                         if all((key[0], dep) not in pending for dep in b.depends_on)]
                if not ready:
                    raise ModelError("Cyclic parameter dependencies.")
                groups = defaultdict(list)
                owners = {}
                for (policy_id, parameter), binding in ready:
                    arguments = []
                    for spec in binding.arguments:
                        if "param" in spec:
                            value = current[policy_id][spec["param"]]
                        elif "constant" in spec:
                            key = spec["constant"]
                            if key not in binding.constants:
                                raise ModelError(f"Missing constant: {parameter}.{key}")
                            value = binding.constants[key]
                            if spec.get("type", "NUMBER") == "NUMBER":
                                value = Decimal(str(value))
                        elif "literal" in spec:
                            value = spec["literal"]
                        else:
                            raise ModelError("Unknown function argument mapping.")
                        arguments.append(value)
                    call_id = json.dumps([policy_id, parameter], separators=(",", ":"))
                    owners[call_id] = (policy_id, parameter, binding, arguments)
                    groups[binding.function_name].append(Invocation(call_id, arguments))
                for function_name, calls in groups.items():
                    results = self.database.execute_batch(function_name, calls)
                    if set(results) != {c.call_id for c in calls}:
                        raise ModelError("Database returned incorrect function result IDs.")
                    for call_id, value in results.items():
                        policy_id, parameter, binding, args = owners[call_id]
                        current[policy_id][parameter] = amount(value)
                        evaluated[policy_id].append({"parameter": parameter, "function": function_name,
                                                     "arguments": [str(a) for a in args]})
                        pending.pop((policy_id, parameter))
            objectives = self.database.objective(objective_function, baseline, current)
            records = []
            for policy_id, state in current.items():
                records.append({"runId": run_id, "modelVersion": model_version,
                    "policyId": policy_id, "iteration": index, **stage,
                    "before": serialize(before[policy_id]), "values": serialize(state),
                    "changedParameters": sorted(k for k in state if state[k] != before[policy_id][k]),
                    "functionCalls": evaluated[policy_id],
                    "objective": format(objectives[policy_id], ".8f")})
            self.database.audit(records)
            trace.extend(records)
        return {"runId": run_id, "modelVersion": model_version, "status": "completed",
                "results": [{"policyId": p, "parameters": serialize(v)} for p, v in current.items()],
                "iterations": trace}
