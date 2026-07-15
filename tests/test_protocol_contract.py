from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from lecturecast.protocol import (
    ClientCapabilities,
    CreativeBrief,
    DecisionCardSet,
    ProductionManifest,
    ProtocolValidationError,
    canonical_digest,
)
from lecturecast.protocol.update import ProtocolImportError, update_protocol


FIXTURE_DIR = Path(__file__).parent / "fixtures"
PROTOCOL_ROOT = Path(__file__).parents[1] / "src" / "lecturecast" / "protocol"
SCHEMA_DIR = PROTOCOL_ROOT / "schemas"
LOCK_PATH = PROTOCOL_ROOT / "protocol.lock"
FIXTURE_MODELS = {
    "client-capabilities-v1.json": ClientCapabilities,
    "creative-brief-v1.json": CreativeBrief,
    "decision-card-set-v1.json": DecisionCardSet,
    "production-manifest-v1.json": ProductionManifest,
}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


@pytest.mark.parametrize(("filename", "model"), sorted(FIXTURE_MODELS.items()))
def test_imported_server_fixtures_validate(filename: str, model: type) -> None:
    document = model.model_validate(_fixture(filename))

    assert document.model_dump() == _fixture(filename)


def test_protocol_lock_covers_exact_schema_bytes() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["bundle_version"] == "1.0"
    assert lock["bundle_digest"] == canonical_digest(lock["files"])
    assert sorted(lock["files"]) == sorted(path.name for path in SCHEMA_DIR.glob("*.json"))
    for filename, expected_digest in lock["files"].items():
        assert _sha256((SCHEMA_DIR / filename).read_bytes()) == expected_digest


def test_golden_fixture_lock_covers_exact_bytes() -> None:
    lock = _fixture("fixture.lock")

    assert lock["bundle_version"] == "1.0"
    assert lock["bundle_digest"] == canonical_digest(lock["files"])
    for filename, expected_digest in lock["files"].items():
        assert _sha256((FIXTURE_DIR / filename).read_bytes()) == expected_digest


def test_public_and_server_canonical_manifest_digests_match() -> None:
    brief = CreativeBrief.model_validate(_fixture("creative-brief-v1.json"))
    capabilities = ClientCapabilities.model_validate(_fixture("client-capabilities-v1.json"))
    manifest = ProductionManifest.model_validate(_fixture("production-manifest-v1.json"))

    assert manifest.payload["brief_digest"] == canonical_digest(brief)
    assert manifest.payload["capability_digest"] == canonical_digest(capabilities)
    assert canonical_digest(manifest) == (
        "sha256:16eaeb9dd156c0636f4c6eaac62f17140a963c9da65174d9a5e7aec77b430263"
    )
    assert canonical_digest(manifest, exclude_top_level={"signature"}) == (
        "sha256:1aacf90909da89b2c0cd504f6afdb18f16f52c3cd82b64ec8c250894fad29edf"
    )


def test_validated_public_document_does_not_expose_mutable_internal_state() -> None:
    manifest = ProductionManifest.model_validate(_fixture("production-manifest-v1.json"))
    external = manifest.payload
    external["total_frames"] = 1

    assert manifest.payload["total_frames"] == 1800


def test_manifest_rejects_unknown_fields_versions_and_negative_duration() -> None:
    unknown = _fixture("production-manifest-v1.json")
    unknown["server_prompt"] = "private"
    with pytest.raises(ProtocolValidationError):
        ProductionManifest.model_validate(unknown)

    wrong_version = _fixture("production-manifest-v1.json")
    wrong_version["schema_version"] = "2.0"
    with pytest.raises(ProtocolValidationError):
        ProductionManifest.model_validate(wrong_version)

    negative = _fixture("production-manifest-v1.json")
    negative["scenes"][0]["duration_frames"] = -1
    with pytest.raises(ProtocolValidationError):
        ProductionManifest.model_validate(negative)


def test_manifest_rejects_duplicate_path_traversal_and_executable_fields() -> None:
    duplicate = _fixture("production-manifest-v1.json")
    duplicate["scenes"].append(copy.deepcopy(duplicate["scenes"][0]))
    with pytest.raises(ProtocolValidationError, match="duplicate scene_id"):
        ProductionManifest.model_validate(duplicate)

    traversal = _fixture("production-manifest-v1.json")
    traversal["scenes"][1]["assets"][0]["uri"] = "asset://../../private/key"
    with pytest.raises(ProtocolValidationError, match="unsafe local path"):
        ProductionManifest.model_validate(traversal)

    executable = _fixture("production-manifest-v1.json")
    executable["scenes"][0]["props"]["tsx"] = "export default function Payload() {}"
    with pytest.raises(ProtocolValidationError, match="executable field"):
        ProductionManifest.model_validate(executable)


def test_update_protocol_verifies_source_before_atomic_import(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target_schemas = tmp_path / "target" / "schemas"
    target_lock = tmp_path / "target" / "protocol.lock"
    shutil.copytree(SCHEMA_DIR, source)
    shutil.copy2(LOCK_PATH, source / "protocol.lock")

    imported = update_protocol(source, target_schemas, target_lock)

    assert imported["bundle_digest"] == json.loads(LOCK_PATH.read_text())["bundle_digest"]
    assert target_lock.read_bytes() == LOCK_PATH.read_bytes()
    for filename in imported["files"]:
        assert (target_schemas / filename).read_bytes() == (SCHEMA_DIR / filename).read_bytes()

    first_schema = next(iter(imported["files"]))
    (source / first_schema).write_text("{}\n", encoding="utf-8")
    with pytest.raises(ProtocolImportError, match="schema digest mismatch"):
        update_protocol(source, target_schemas, target_lock)
