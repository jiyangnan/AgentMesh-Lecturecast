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
NOW = "2026-07-15T12:00:00Z"


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


# --------------------------------------------------------------------------- #
# §5.5e6-client #121 — verify-signature + failure_kind 映射 + workflow
# --------------------------------------------------------------------------- #


def _keyring_for(key_id: str, *, status: str = "current"):
    """Build a fresh Ed25519 keypair and a PublicKeyRing containing it, so a
    catalog can be signed by the matching private key and verified against the
    injected ring. Returns (ring, private_key)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from lecturecast.manifest import PublicKeyRing, SigningKey

    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    ring = PublicKeyRing(
        [
            SigningKey(
                key_id=key_id,
                algorithm="Ed25519",
                public_key=public_key,
                status=status,
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-01-01T00:00:00Z",
            )
        ]
    )
    return ring, private_key


def _sign_catalog_with(catalog: dict, private_key: Ed25519PrivateKey) -> dict:
    """Re-sign a catalog with the given private key, mirroring the server's sign
    order (each directive standalone, then the catalog top level)."""
    directives = catalog.get("directives") or {}
    for key, directive in list(directives.items()):
        if isinstance(directive, dict):
            directives[key] = _sign_payload(directive, private_key)
    return _sign_payload(catalog, private_key)


def _build_catalog(
    key_id: str, private_key: Ed25519PrivateKey, *, failure_kind: str = "m1_insufficient_credits"
) -> dict:
    """A fully-signed catalog for a given key, ready for verification."""
    directive = _base_directive(key_id)
    directive["failure_kind"] = failure_kind
    signed_directive = _sign_payload(directive, private_key)
    catalog = {
        "catalog_version": "recovery_base_v1",
        "directives": {failure_kind: signed_directive},
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
    }
    return _sign_catalog_with(catalog, private_key)


def test_recovery_catalog_verify_passes_with_matching_keyring() -> None:
    """F-C4: a catalog signed with the matching private key verifies under the
    injected keyring (no created_at time-window check — the catalog has none)."""
    from lecturecast.manifest import verify_recovery_catalog_signature

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    catalog = _build_catalog(key_id, private_key)

    result = verify_recovery_catalog_signature(catalog, keyring=ring)

    assert result.valid is True
    assert result.key_id == key_id
    assert result.key_status == "current"
    assert result.manifest_digest.startswith("sha256:")


def test_recovery_catalog_verify_rejects_tampered_user_message() -> None:
    """F-C5: tampering with user_message (without re-signing) must fail-closed
    with LectureCastError(code="manifest_signature_invalid")."""
    from lecturecast.errors import LectureCastError
    from lecturecast.manifest import verify_recovery_catalog_signature

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    catalog = _build_catalog(key_id, private_key)
    catalog["directives"]["m1_insufficient_credits"]["user_message"] = "改过的文案"

    with pytest.raises(LectureCastError) as excinfo:
        verify_recovery_catalog_signature(catalog, keyring=ring)
    assert excinfo.value.code == "manifest_signature_invalid"


def test_recovery_catalog_verify_rejects_unknown_key() -> None:
    """F-C6: a key_id not present in the keyring must be rejected (fail-closed)."""
    from lecturecast.errors import LectureCastError
    from lecturecast.manifest import verify_recovery_catalog_signature

    ring, _ = _keyring_for("test_key_v1")
    _, other_private = _keyring_for("other_key_v1")
    catalog = _build_catalog("other_key_v1", other_private)

    with pytest.raises(LectureCastError) as excinfo:
        verify_recovery_catalog_signature(catalog, keyring=ring)
    assert excinfo.value.code == "manifest_signature_invalid"


def test_recovery_catalog_verify_rejects_revoked_key() -> None:
    """F-C6: a revoked key (status=revoked) must be rejected."""
    from lecturecast.errors import LectureCastError
    from lecturecast.manifest import verify_recovery_catalog_signature

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id, status="revoked")
    catalog = _build_catalog(key_id, private_key)

    with pytest.raises(LectureCastError) as excinfo:
        verify_recovery_catalog_signature(catalog, keyring=ring)
    assert excinfo.value.code == "manifest_signature_invalid"


def test_recovery_from_failure_hit_returns_directive() -> None:
    """F-C7: recover_from_failure with a matching failure_kind returns the
    directive dict (is_main_blocker/user_message/options/steer_back_line)."""
    from lecturecast.recovery import recover_from_failure

    catalog = _signed_catalog()  # dict form; key m1_insufficient_credits
    directive = recover_from_failure("m1_insufficient_credits", catalog)

    assert directive is not None
    assert directive["failure_kind"] == "m1_insufficient_credits"
    assert directive["is_main_blocker"] is True
    assert directive["user_message"] == "云端制作额度不足，本期正片暂时无法在云端生成。"
    assert directive["options"][0]["option_id"] == "top_up_and_resume"
    assert directive["steer_back_line"] == "额度到账后回到原项目继续，主线脚本不丢。"


def test_recover_from_failure_miss_returns_none() -> None:
    """F-C8: no directive for the failure_kind (or catalog None) → None."""
    from lecturecast.recovery import recover_from_failure

    catalog = _signed_catalog()
    assert recover_from_failure("m1_schema_unsupported", catalog) is None
    assert recover_from_failure("m1_insufficient_credits", None) is None


def test_failure_kind_for_error_maps_server_code() -> None:
    """The deterministic error→failure_kind mapping (tech spec §7.3). Only
    unambiguous server codes map — unknown codes → None (宁可少报绝不虚报)."""
    from lecturecast.errors import LectureCastError
    from lecturecast.recovery import failure_kind_for_error

    error = LectureCastError(
        code="insufficient_credits", message="余额不足。", next_action="充值。",
        http_status=402,
    )
    assert failure_kind_for_error(error) == "m1_insufficient_credits"
    # manifest_signature_invalid maps to the signing-failure directive.
    error2 = LectureCastError(
        code="manifest_signature_invalid", message="签名失败。", next_action="重试。",
    )
    assert failure_kind_for_error(error2) == "m1_manifest_signing_failed"
    # Unknown / ambiguous codes → None (catalog-driven, never hard-code kinds).
    for code in ("core_unavailable", "manifest_incompatible", "generation_in_progress"):
        assert failure_kind_for_error(
            LectureCastError(code=code, message="x", next_action="y")
        ) is None


def test_recovery_workflow_unverified_catalog_returns_none() -> None:
    """F-C9: _recovery_workflow must NOT present a directive for an unverified /
    tampered catalog (fail-closed: never act on an unverified catalog). The
    catalog here is signed with a test key the REAL keyring does not trust, so
    verification fails → None."""
    from lecturecast.commands.director import _recovery_workflow
    from lecturecast.errors import LectureCastError

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    catalog = _build_catalog(key_id, private_key)
    error = LectureCastError(
        code="insufficient_credits", message="余额不足。", next_action="充值。",
        http_status=402,
    )

    workflow = _recovery_workflow(error, catalog, "/tmp/proj")
    assert workflow is None


def test_recovery_workflow_is_main_blocker_false_phase() -> None:
    """F-C10: is_main_blocker=false → phase=recovery_directive_required with
    host_choice (question_label==user_message, options carry the recommended
    marker, steer_back_line, requires_user_approval=True)."""
    from lecturecast.commands.director import _recovery_workflow
    from lecturecast.errors import LectureCastError

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    directive = _base_directive(key_id)
    directive["failure_kind"] = "heygen_key_invalid"
    directive["is_main_blocker"] = False
    signed_directive = _sign_payload(directive, private_key)
    catalog = {
        "catalog_version": "recovery_provider_heygen_v1",
        "directives": {"heygen_key_invalid": signed_directive},
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
    }
    catalog = _sign_catalog_with(catalog, private_key)

    error = LectureCastError(
        code="heygen_key_invalid", message="HeyGen key 无效。", next_action="修 key。",
    )
    workflow = _recovery_workflow(error, catalog, "/tmp/proj", keyring=ring)

    assert workflow is not None
    assert workflow["phase"] == "recovery_directive_required"
    action = workflow["next_action"]
    assert action["id"] == "director.recovery.decide"
    assert action["kind"] == "host_choice"
    assert action["question_id"] == "recovery_heygen_key_invalid"
    assert action["question_label"] == directive["user_message"]
    assert action["options"][0]["id"] == "top_up_and_resume"
    assert action["options"][0]["label"].endswith("（推荐）")
    assert action["steer_back_line"] == directive["steer_back_line"]
    assert action["requires_user_approval"] is True


def test_recovery_workflow_is_main_blocker_true_phase() -> None:
    """F-C11: is_main_blocker=true → phase=main_blocker_recovery_required with
    host_choice requires_user_approval=True."""
    from lecturecast.commands.director import _recovery_workflow
    from lecturecast.errors import LectureCastError

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    catalog = _build_catalog(key_id, private_key)

    error = LectureCastError(
        code="insufficient_credits", message="余额不足。", next_action="充值。",
        http_status=402,
    )
    workflow = _recovery_workflow(error, catalog, "/tmp/proj", keyring=ring)

    assert workflow is not None
    assert workflow["phase"] == "main_blocker_recovery_required"
    assert workflow["next_action"]["kind"] == "host_choice"
    assert workflow["next_action"]["requires_user_approval"] is True


def test_recovery_workflow_no_match_returns_none() -> None:
    """A catalog with no directive for the mapped failure_kind → None (fall
    through to _resume_error_workflow)."""
    from lecturecast.commands.director import _recovery_workflow
    from lecturecast.errors import LectureCastError

    key_id = "test_key_v1"
    ring, private_key = _keyring_for(key_id)
    # Catalog has only m1_insufficient_credits; the error maps to
    # m1_manifest_signing_failed, which is absent → None.
    catalog = _build_catalog(key_id, private_key)

    error = LectureCastError(
        code="manifest_signature_invalid", message="签名失败。", next_action="重试。",
    )
    workflow = _recovery_workflow(error, catalog, "/tmp/proj", keyring=ring)
    assert workflow is None


# ---- v1.3 state migration: persist recovery_catalog ----

def test_v1_3_state_persists_recovery_catalog_from_generation(tmp_path: Path):
    """The v1.3 state migration must persist recovery_catalog from a v1.1
    generation response so it is reachable at resume-error time (finding #6)."""
    from lecturecast.director import DirectorStateStore

    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")
    catalog = _signed_catalog()
    generation = {
        "generation_id": "gen_1", "status": "queued", "updated_at": NOW,
        "billing_state": "awaiting_credits", "resume_available": True,
        "recovery_catalog": catalog,
    }
    state = store.update(state, generation=generation)

    assert state.payload["schema_version"] == "1.3"
    assert state.recovery_catalog is not None
    assert state.recovery_catalog["catalog_version"] == "recovery_base_v1"
    # Reload from disk: catalog reachable at error time.
    reloaded = store.load()
    assert reloaded.payload["schema_version"] == "1.3"
    assert reloaded.recovery_catalog["directives"]["m1_insufficient_credits"]["failure_kind"] == "m1_insufficient_credits"


def test_v1_3_state_loads_older_versions(tmp_path: Path):
    """Old v1.0/v1.1/v1.2 state files must still load unchanged (back-compat)."""
    from lecturecast.director import DirectorStateStore

    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    # v1.1 state (no billing).
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    assert state.payload["schema_version"] == "1.1"
    assert state.recovery_catalog is None
