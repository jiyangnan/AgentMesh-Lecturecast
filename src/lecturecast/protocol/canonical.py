from __future__ import annotations

import hashlib
import json
from typing import Any, Collection, Protocol


class JsonDumpable(Protocol):
    def model_dump(self) -> dict[str, Any]: ...


def _json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return value


def canonical_bytes(value: Any, *, exclude_top_level: Collection[str] = ()) -> bytes:
    payload = _json_value(value)
    if exclude_top_level:
        if not isinstance(payload, dict):
            raise TypeError("top-level exclusions require a JSON object")
        excluded = set(exclude_top_level)
        payload = {key: child for key, child in payload.items() if key not in excluded}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded.encode("utf-8")


def canonical_digest(value: Any, *, exclude_top_level: Collection[str] = ()) -> str:
    digest = hashlib.sha256(canonical_bytes(value, exclude_top_level=exclude_top_level)).hexdigest()
    return f"sha256:{digest}"


def manifest_signing_bytes(value: Any) -> bytes:
    payload = _json_value(value)
    if not isinstance(payload, dict):
        raise TypeError("manifest signing requires a JSON object")
    signature = payload.get("signature")
    if not isinstance(signature, dict):
        raise ValueError("manifest signature metadata is required")
    signing_payload = dict(payload)
    signing_payload["signature"] = {
        key: child for key, child in signature.items() if key != "value"
    }
    return canonical_bytes(signing_payload)


def manifest_signing_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(manifest_signing_bytes(value)).hexdigest()}"
