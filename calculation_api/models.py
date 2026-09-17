"""Immutable snapshots and canonical JSON: never share mutable core dictionaries."""
from dataclasses import dataclass
from hashlib import sha256
import json


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class FrozenSnapshot:
    document_json: str
    content_hash: str

    @classmethod
    def create(cls, document):
        document = dict(document)
        document.pop("contentHash", None)
        return cls(canonical_json(document), fingerprint(document))

    def to_dict(self):
        # Each caller receives a detached object. Nested data cannot mutate self.
        return {**json.loads(self.document_json), "contentHash": self.content_hash}
