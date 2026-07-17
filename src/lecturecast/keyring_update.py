from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .manifest import PublicKeyRing, SigningKey


def _entry(path: Path) -> SigningKey:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if set(envelope) != {"entry_version", "fingerprint", "key"}:
            raise ValueError
        if envelope["entry_version"] != "1.0":
            raise ValueError
        key = SigningKey(**envelope["key"])
        validated = PublicKeyRing([key])
        if validated.public_key_fingerprint(key) != envelope["fingerprint"]:
            raise ValueError
        return key
    except (OSError, TypeError, KeyError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid signing public-entry envelope") from None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update_keyring(*, keyring_path: Path, entry_path: Path) -> dict[str, Any]:
    ring = PublicKeyRing.load(keyring_path)
    incoming = _entry(entry_path)
    existing = ring.get(incoming.key_id)
    if existing is not None:
        if existing != incoming:
            raise ValueError("existing signing key_id cannot be mutated")
        ring.validate_for_release()
        return ring.to_dict()
    keys = [
        replace(key, status="previous") if key.status == "current" else key
        for key in ring.keys
    ]
    keys.append(incoming)
    updated = PublicKeyRing(keys)
    updated.validate_for_release()
    payload = updated.to_dict()
    _atomic_write(keyring_path, payload)
    return payload


def revoke_key(*, keyring_path: Path, key_id: str) -> dict[str, Any]:
    ring = PublicKeyRing.load(keyring_path)
    target = ring.get(key_id)
    if target is None:
        raise ValueError("signing key_id is unknown")
    if target.status == "current":
        raise ValueError("publish and activate a replacement before revoking the current key")
    if target.status == "revoked":
        ring.validate_for_release()
        return ring.to_dict()
    updated = PublicKeyRing(
        [replace(key, status="revoked") if key.key_id == key_id else key for key in ring.keys]
    )
    updated.validate_for_release()
    payload = updated.to_dict()
    _atomic_write(keyring_path, payload)
    return payload
