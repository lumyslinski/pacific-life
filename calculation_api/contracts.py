from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass
class Binding:
    parameter: str
    function_name: str
    arguments: list[dict]
    constants: dict
    depends_on: list[str]


@dataclass
class Invocation:
    call_id: str
    arguments: list[Any]


class DatabaseFunctions(Protocol):
    def execute_batch(self, function_name: str, calls: list[Invocation]) -> dict[str, Decimal]: ...
    def objective(self, function_name: str, baseline: dict, current: dict) -> dict[str, Decimal]: ...
    def audit(self, records: list[dict]) -> None: ...
