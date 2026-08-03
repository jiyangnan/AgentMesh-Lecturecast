from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lecturecast.protocol import (
    ManifestGenerationOutV1_1,
    ProtocolValidationError,
    RecoveryDirectiveCatalog,
    canonical_digest,
    documents_for_protocol_version,
    manifest_signing_bytes,
)


PROTOCOL_ROOT = Path(__file__).parents[1] / "src" / "lecturecast" / "protocol"
V1_1_SCHEMA_DIR = PROTOCOL_ROOT / "schemas" / "v1.1"
V1_1_LOCK_PATH = V1_1_SCHEMA_DIR / "protocol.lock"
PLACEHOLDER = "A" * 86 + "=="


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _base_directive(key_id: str = "test_key_v1") -> dict:
    return {
        "failure_kind": "m1_insufficient_credits",
        "is_main_blocker": True,
        "user_message": "云端制作额度不足，本期正片暂时无法在云端生成。",
        "options": [
            {
                "option_id": "top_up_and_resume",
                "label": "去充值后继续",
                "recommended": True,
                "resume_action": {
                    "action_id": "open_provider_dashboard",
                    "args": {"provider": "lecturecast"},
                },
            },
        ],
        "steer_back_line": "额度到账后回到原项目继续，主线脚本不丢。",
        "do_not": ["不要让用户重复点击生成刷屏"],
        "external_handoff": None,
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
    }


def _sign_payload(payload: dict, private_key: Ed25519PrivateKey) -> dict:
    """Replicate the server's sign_artifact: blank value, sign canonical bytes,
    fill value. Returns a deep copy."""
    signed = copy.deepcopy(payload)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "key_id": signed["signature"]["key_id"],
        "value": "",
    }
    signature = private_key.sign(manifest_signing_bytes(signed))
    signed["signature"]["value"] = base64.b64encode(signature).decode()
    return signed


def _signed_catalog(*, key_id: str = "test_key_v1") -> dict:
    private_key = Ed25519PrivateKey.generate()
    directive = _sign_payload(_base_directive(key_id), private_key)
    catalog = {
        "catalog_version": "recovery_base_v1",
        "directives": {"m1_insufficient_credits": directive},
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
    }
    signed = _sign_payload(catalog, private_key)
    return signed


# --------------------------------------------------------------------------- #
# §5.5e6-client #120 — vendor + parse
# --------------------------------------------------------------------------- #


def test_v1_1_lock_covers_recovery_catalog_exact_bytes() -> None:
    """Re-vendored v1.1 bundle must carry the recovery-directive-catalog schema
    and the updated manifest-generation-out (with recovery_catalog field), and
    the lock must be byte-accurate (same canonical_digest rule as v1.0)."""
    lock = json.loads(V1_1_LOCK_PATH.read_text(encoding="utf-8"))

    assert "recovery-directive-catalog.schema.json" in lock["files"]
    assert lock["bundle_digest"] == canonical_digest(lock["files"])
    schema_files = sorted(path.name for path in V1_1_SCHEMA_DIR.glob("*.json"))
    assert sorted(lock["files"]) == schema_files
    for filename, expected_digest in lock["files"].items():
        assert _sha256((V1_1_SCHEMA_DIR / filename).read_bytes()) == expected_digest


def test_v1_0_lock_unchanged() -> None:
    """v1.0 bundle is frozen — its lock must still be exactly the committed one."""
    lock = json.loads((PROTOCOL_ROOT / "protocol.lock").read_text(encoding="utf-8"))
    assert lock["bundle_version"] == "1.0"
    flat = PROTOCOL_ROOT / "schemas"
    assert sorted(lock["files"]) == sorted(path.name for path in flat.glob("*.json"))
    for filename, expected_digest in lock["files"].items():
        assert _sha256((flat / filename).read_bytes()) == expected_digest


def test_v1_1_generation_schema_carries_recovery_catalog() -> None:
    """The re-vendored manifest-generation-out schema must expose the
    recovery_catalog field (server step 5 already fills it on the resume path),
    and it must be optional (default null — not in required)."""
    schema = json.loads(
        (V1_1_SCHEMA_DIR / "manifest-generation-out.schema.json").read_text(encoding="utf-8")
    )

    assert "recovery_catalog" in schema["properties"]
    assert "recovery_catalog" not in schema.get("required", [])
    recovery = schema["properties"]["recovery_catalog"]
    assert any(sub.get("type") == "null" for sub in recovery.get("anyOf", []))
    assert any("RecoveryDirectiveCatalog" in str(sub.get("$ref", "")) for sub in recovery.get("anyOf", []))


def test_recovery_catalog_model_validates_roundtrip() -> None:
    catalog = _signed_catalog()

    document = RecoveryDirectiveCatalog.model_validate(catalog)

    assert document.model_dump() == catalog


def test_recovery_catalog_model_accepts_json_string() -> None:
    """model_validate_json accepts a JSON string (the wire form) and yields the
    same payload as the dict path."""
    catalog = _signed_catalog()

    document = RecoveryDirectiveCatalog.model_validate_json(json.dumps(catalog))

    assert document.model_dump() == catalog


def test_recovery_catalog_rejects_key_mismatch_failure_kind() -> None:
    """Defense-in-depth semantic check: directives key must equal the directive's
    failure_kind. A swap (signature would still pass) must be rejected here."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    directive["failure_kind"] = "m1_schema_unsupported"
    catalog["directives"] = {"m1_insufficient_credits": directive}

    with pytest.raises(ProtocolValidationError, match="does not match failure_kind"):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_missing_top_level_signature() -> None:
    catalog = _signed_catalog()
    del catalog["signature"]

    with pytest.raises(ProtocolValidationError):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_unknown_top_level_field() -> None:
    catalog = _signed_catalog()
    catalog["malicious"] = "value"

    with pytest.raises(ProtocolValidationError):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_invalid_action_id() -> None:
    """RecoveryAction.action_id is a closed enum (6 values) — the client must
    reject a directive with a non-whitelisted action (never forward to a
    subprocess/shell/URL opener, tech spec §7.3)."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    directive["options"][0]["resume_action"]["action_id"] = "rm -rf"
    catalog["directives"]["m1_insufficient_credits"] = directive

    with pytest.raises(ProtocolValidationError):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_duplicate_option_id() -> None:
    """Same directive must not expose two options with the same option_id —
    downstream mapping (#122 decide command) would be ambiguous."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    dup = copy.deepcopy(directive["options"][0])
    dup["recommended"] = False
    directive["options"].append(dup)
    catalog["directives"]["m1_insufficient_credits"] = directive

    with pytest.raises(ProtocolValidationError, match="duplicate option_id"):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_multiple_recommended() -> None:
    """At most one recommended option per directive (server contract) — two
    recommended flags would make the host's single recommendation ambiguous."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    second = copy.deepcopy(directive["options"][0])
    second["option_id"] = "second_recommended"
    second["recommended"] = True
    directive["options"].append(second)
    catalog["directives"]["m1_insufficient_credits"] = directive

    with pytest.raises(ProtocolValidationError, match="more than one recommended"):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_unsafe_action_args() -> None:
    """args are rendered/acted on by the host — NUL/path-escape/absolute values
    must fail at the parse boundary (never forwarded to a URL opener, §7.3)."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    directive["options"][0]["resume_action"]["args"]["provider"] = "javascript:alert(1)"
    catalog["directives"]["m1_insufficient_credits"] = directive

    with pytest.raises(ProtocolValidationError, match="unsafe"):
        RecoveryDirectiveCatalog.model_validate(catalog)


def test_recovery_catalog_rejects_unsafe_external_handoff() -> None:
    """external_handoff is surfaced to the user / other tools — a path-escape
    or absolute-local-path value must be rejected at parse time."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    directive["external_handoff"] = {"help_url": "/../../etc/passwd"}
    catalog["directives"]["m1_insufficient_credits"] = directive

    with pytest.raises(ProtocolValidationError, match="unsafe"):
        RecoveryDirectiveCatalog.model_validate(catalog)


def _v1_1_mgo_with_catalog(recovery_catalog: dict | None) -> dict:
    return {
        "generation_id": "gen_1",
        "session_id": "sess_1",
        "brief_version": 1,
        "status": "ready",
        "model_policy_version": "flash_all_v1",
        "capability_digest": "sha256:" + "a" * 64,
        "manifest_digest": "sha256:" + "b" * 64,
        "manifest": None,
        "deducted_credits": 30,
        "error_code": None,
        "credit_return_status": "not_required",
        "attempt_count": 1,
        "created_at": "2026-07-28T12:00:00Z",
        "updated_at": "2026-07-28T12:00:00Z",
        "completed_at": "2026-07-28T12:00:00Z",
        "milestone_charges": [
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": "2026-07-28T12:00:00Z"},
        ],
        "billing_state": "charged",
        "resume_available": False,
        "recovery_catalog": recovery_catalog,
    }


def test_mgo_embedded_catalog_passes_semantics() -> None:
    """A well-formed embedded catalog (resume path, server step 5) parses fine."""
    mgo = _v1_1_mgo_with_catalog(_signed_catalog())
    ManifestGenerationOutV1_1.model_validate(mgo)


def test_mgo_embedded_catalog_rejects_key_mismatch() -> None:
    """The defense-in-depth key↔failure_kind check must not be bypassable via
    the embedded mgo path — a swapped failure_kind must be rejected there too."""
    catalog = _signed_catalog()
    directive = copy.deepcopy(catalog["directives"]["m1_insufficient_credits"])
    directive["failure_kind"] = "m1_schema_unsupported"
    catalog["directives"] = {"m1_insufficient_credits": directive}
    mgo = _v1_1_mgo_with_catalog(catalog)

    with pytest.raises(ProtocolValidationError, match="does not match failure_kind"):
        ManifestGenerationOutV1_1.model_validate(mgo)


def test_documents_registers_recovery_catalog() -> None:
    """documents_for_protocol_version("1.1") must expose the recovery_catalog
    document type so the client can parse the delivered catalog."""
    v1_1 = documents_for_protocol_version("1.1")
    assert v1_1["recovery_catalog"] is RecoveryDirectiveCatalog
    assert "recovery_catalog" not in documents_for_protocol_version("1.0")
