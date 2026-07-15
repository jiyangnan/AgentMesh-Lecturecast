from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ProtocolImportError(ValueError):
    pass


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _canonical_digest(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(content)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def update_protocol(source: Path, schema_dir: Path, lock_path: Path) -> dict[str, Any]:
    source_lock_path = source / "protocol.lock"
    if not source_lock_path.is_file():
        raise ProtocolImportError(f"missing source lock: {source_lock_path}")
    lock = json.loads(source_lock_path.read_text(encoding="utf-8"))
    files = lock.get("files")
    if not isinstance(files, dict) or not files:
        raise ProtocolImportError("source lock has no schema files")
    if lock.get("bundle_digest") != _canonical_digest(files):
        raise ProtocolImportError("source bundle digest does not match lock")

    contents: dict[str, bytes] = {}
    for filename, expected_digest in sorted(files.items()):
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ProtocolImportError(f"unsafe schema filename: {filename}")
        if not isinstance(expected_digest, str):
            raise ProtocolImportError(f"invalid schema digest: {filename}")
        content = (source / filename).read_bytes()
        if _sha256(content) != expected_digest:
            raise ProtocolImportError(f"schema digest mismatch: {filename}")
        contents[filename] = content

    for filename, content in contents.items():
        _atomic_write(schema_dir / filename, content)
    _atomic_write(lock_path, source_lock_path.read_bytes())
    return lock
