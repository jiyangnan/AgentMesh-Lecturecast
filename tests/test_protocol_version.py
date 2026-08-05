"""Protocol-version negotiation (§5.5a)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lecturecast.config import PROTOCOL_VERSION_ENV, resolve_protocol_version
from lecturecast.director import DirectorStateStore


NOW = "2026-07-15T12:00:00Z"


# ---- resolve_protocol_version ----

def test_default_protocol_version_is_1_0() -> None:
    assert resolve_protocol_version({}) == "1.0"


def test_env_can_opt_into_1_1() -> None:
    assert resolve_protocol_version({PROTOCOL_VERSION_ENV: "1.1"}) == "1.1"


def test_unsupported_protocol_version_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported protocol version"):
        resolve_protocol_version({PROTOCOL_VERSION_ENV: "2.0"})


# ---- DirectorState backward compat + pinning ----

def _v1_0_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project_id": "proj_1",
        "state_revision": 1,
        "server_url": "https://api.lecturecast.agentmesh360.com",
        "session_id": "sess_1",
        "session_status": "collecting_decisions",
        "brief_version": 0,
        "catalog_version": "2026-07-16.1",
        "adapter_kind": "codex",
        "adapter_version": "1.0.0",
        "generation_id": None,
        "generation_status": None,
        "updated_at": NOW,
    }


def test_old_v1_0_state_defaults_protocol_version_to_1_0() -> None:
    """A v1.0 state file (schema_version 1.0, no protocol_version key) loads as
    1.0 — the frozen v1.0 shape is preserved exactly."""
    payload = _v1_0_payload()
    assert "protocol_version" not in payload
    state = DirectorStateStore._validate(payload)
    assert state.protocol_version == "1.0"
    assert state.payload["schema_version"] == "1.0"


def test_v1_0_state_rejects_protocol_version_key() -> None:
    """The v1.0 state shape is frozen: a protocol_version key is rejected."""
    payload = dict(_v1_0_payload()) | {"protocol_version": "1.0"}
    with pytest.raises(ValueError, match="unexpected or incomplete"):
        DirectorStateStore._validate(payload)


def test_v1_1_state_pins_protocol_version() -> None:
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.1",
        "protocol_version": "1.1",
    }
    state = DirectorStateStore._validate(payload)
    assert state.protocol_version == "1.1"
    assert state.payload["schema_version"] == "1.1"


def test_v1_1_state_rejects_protocol_version_1_1() -> None:
    payload = dict(_v1_0_payload()) | {"schema_version": "1.1", "protocol_version": "1.0"}
    with pytest.raises(ValueError, match="requires protocol_version=1.1"):
        DirectorStateStore._validate(payload)


def test_v1_2_state_with_billing_snapshot() -> None:
    """schema_version 1.2 persists billing_state / resume_available /
    billing_updated_at on the first v1.1 generation response."""
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2",
        "protocol_version": "1.1",
        "billing_state": "awaiting_credits",
        "resume_available": True,
        "billing_updated_at": NOW,
    }
    state = DirectorStateStore._validate(payload)
    assert state.billing_state == "awaiting_credits"
    assert state.resume_available is True
    assert state.payload["billing_updated_at"] == NOW


def test_v1_2_state_rejects_missing_billing_keys() -> None:
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2", "protocol_version": "1.1",
        "billing_state": "charged",
    }
    with pytest.raises(ValueError, match="unexpected or incomplete"):
        DirectorStateStore._validate(payload)


def test_v1_1_state_still_loads_after_d2() -> None:
    """Old v1.1 state files (without billing snapshot) must still load."""
    payload = dict(_v1_0_payload()) | {"schema_version": "1.1", "protocol_version": "1.1"}
    state = DirectorStateStore._validate(payload)
    assert state.protocol_version == "1.1"
    assert state.billing_state is None
    assert state.resume_available is False


# ---- v1.2 strict type/vocab validation (§5.5d2 Codex fix) ----

@pytest.mark.parametrize("bad_state", ["unknown", "ready", 42, None])
def test_v1_2_rejects_invalid_billing_state(bad_state):
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2", "protocol_version": "1.1",
        "billing_state": bad_state, "resume_available": True,
        "billing_updated_at": NOW,
    }
    with pytest.raises(ValueError, match="billing_state"):
        DirectorStateStore._validate(payload)


@pytest.mark.parametrize("bad_val", ["true", "false", 1, 0, None])
def test_v1_2_rejects_non_bool_resume_available(bad_val):
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2", "protocol_version": "1.1",
        "billing_state": "charged", "resume_available": bad_val,
        "billing_updated_at": NOW,
    }
    with pytest.raises(ValueError, match="resume_available"):
        DirectorStateStore._validate(payload)


@pytest.mark.parametrize("bad_val", [None, "", 42])
def test_v1_2_rejects_empty_billing_updated_at(bad_val):
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2", "protocol_version": "1.1",
        "billing_state": "charged", "resume_available": True,
        "billing_updated_at": bad_val,
    }
    with pytest.raises(ValueError, match="billing_updated_at"):
        DirectorStateStore._validate(payload)


@pytest.mark.parametrize("bad_ts", ["banana", "2026-07-28", "not-a-timestamp"])
def test_v1_2_rejects_invalid_billing_timestamp(bad_ts):
    payload = dict(_v1_0_payload()) | {
        "schema_version": "1.2", "protocol_version": "1.1",
        "billing_state": "charged", "resume_available": True,
        "billing_updated_at": bad_ts,
    }
    with pytest.raises(ValueError, match="billing_updated_at"):
        DirectorStateStore._validate(payload)


def test_update_rejects_non_bool_resume_available_from_generation(tmp_path: Path):
    """update(generation=...) with resume_available='false' (string) must NOT
    be silently coerced; _validate must reject it."""
    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")
    generation = {
        "generation_id": "gen_1", "status": "queued", "updated_at": NOW,
        "billing_state": "awaiting_credits", "resume_available": "false",
    }
    with pytest.raises(ValueError, match="resume_available"):
        store.update(state, generation=generation)


# ---- v1.1→1.2 real persistence round-trip (§5.5d2 Codex fix) ----

def test_update_with_billing_generation_upgrades_1_1_to_1_2(tmp_path: Path):
    """DirectorStateStore.update(generation=...) with a v1.1 generation response
    containing billing_state must: upgrade schema 1.1→1.2, persist the three
    snapshot fields, increment revision, and NOT persist milestone_charges or
    any sensitive fields."""
    import json
    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    # Create a v1.1 session (no generation yet).
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0",
        protocol_version="1.1",
    )
    assert state.payload["schema_version"] == "1.1"
    assert state.revision == 1

    # Set the generation_id first (as create_generation would).
    state = store.update(state, generation_id="gen_1", generation_status="queued")

    # Simulate a v1.1 generation response with billing_state.
    generation = {
        "generation_id": "gen_1", "status": "queued", "updated_at": NOW,
        "billing_state": "awaiting_credits", "resume_available": True,
        # Sensitive fields that must NOT be persisted:
        "milestone_charges": [{"milestone": "manifest", "cost": 10, "status": "charged"}],
        "ledger_id": 42, "idempotency_key": "secret",
    }
    updated = store.update(state, generation=generation)
    # Schema upgraded to 1.2.
    assert updated.payload["schema_version"] == "1.2"
    assert updated.revision == 3  # 1=create, 2=set gen_id, 3=billing update
    assert updated.billing_state == "awaiting_credits"
    assert updated.resume_available is True
    assert updated.payload["billing_updated_at"] == NOW

    # Reload from disk.
    reloaded = store.load()
    assert reloaded.payload["schema_version"] == "1.2"
    assert reloaded.billing_state == "awaiting_credits"
    assert reloaded.resume_available is True

    # Sensitive fields NOT persisted.
    raw = json.loads((tmp_path / ".lecturecast" / "director-state.json").read_text())
    assert "milestone_charges" not in raw
    assert "ledger_id" not in raw
    assert "idempotency_key" not in raw


# ---- version-aware model dispatch ----

def test_documents_for_protocol_version_dispatches() -> None:
    from lecturecast.protocol import (
        ClientCapabilitiesV1_1, DecisionCardSetV1_1, documents_for_protocol_version,
    )

    v1_1 = documents_for_protocol_version("1.1")
    v1_0 = documents_for_protocol_version("1.0")
    assert v1_1["decision_card_set"] is DecisionCardSetV1_1
    assert v1_1["client_capabilities"] is ClientCapabilitiesV1_1
    assert "presenter_plan" in v1_1 and "presenter_plan" not in v1_0
    assert v1_0["decision_card_set"].schema_dir != v1_1["decision_card_set"].schema_dir


def test_v1_1_capabilities_payload_rejects_under_v1_0_model() -> None:
    """Version isolation: a v1.1 capabilities payload (schema_version 1.1)
    validates under the V1.1 model but is rejected by the v1.0 strict schema
    (additionalProperties:false + schema_version Literal)."""
    from pathlib import Path

    from lecturecast.protocol import ClientCapabilities, ClientCapabilitiesV1_1

    base = json.loads(
        (Path(__file__).parent / "fixtures" / "client-capabilities-v1.json").read_text()
    )
    v1_1_payload = dict(base)
    v1_1_payload["schema_version"] = "1.1"
    # v1.1 model accepts the widened schema_version.
    ClientCapabilitiesV1_1.model_validate(v1_1_payload)
    # v1.0 model rejects it (schema_version Literal["1.0"]).
    with pytest.raises(Exception):
        ClientCapabilities.model_validate(v1_1_payload)


# ---- v1.1 response contract via the version-aware Director parser ----

def _v1_1_session_document(card: bool, brief: bool) -> dict[str, Any]:
    return {
        "session_id": "sess_v1_1",
        "status": "collecting_decisions",
        "brief_version": 0,
        "catalog_version": "2026-07-16.1",
        "updated_at": NOW,
        "decision_card_set": json.loads(
            (Path(__file__).parent / "fixtures" / "decision-card-set-v1_1.json").read_text()
        ) if card else None,
        "brief": json.loads(
            (Path(__file__).parent / "fixtures" / "creative-brief-v1_1.json").read_text()
        ) if brief else None,
    }


def test_v1_1_card_parses_under_1_1_rejects_under_1_0() -> None:
    """A real v1.1 presenter card (stage=presenter, condition.all_of, consent
    disclosure, pricing_estimate) must parse under protocol 1.1 and be rejected
    under 1.0 — proving the version-aware parser, not a hardcoded v1.0 model."""
    from lecturecast.director import DirectorClient

    card = json.loads(
        (Path(__file__).parent / "fixtures" / "decision-card-set-v1_1-presenter.json").read_text()
    )
    doc = _v1_1_session_document(card=False, brief=False)
    doc["decision_card_set"] = card
    DirectorClient._session(doc, protocol_version="1.1")  # accepts
    with pytest.raises(Exception):
        DirectorClient._session(doc, protocol_version="1.0")  # rejects


def test_v1_1_brief_parses_under_1_1() -> None:
    from lecturecast.director import DirectorClient

    doc = _v1_1_session_document(card=False, brief=True)
    DirectorClient._session(doc, protocol_version="1.1")
    with pytest.raises(Exception):
        DirectorClient._session(doc, protocol_version="1.0")


def test_v1_1_plan_models_target_v1_1_bundle() -> None:
    """PresenterPlan / OrchestrationPlan are v1.1-only documents — they load
    the v1.1 schema bundle (full plan fixtures are built when the client
    consumes M2/M3 in §5.5d/e; this locks the model wiring now)."""
    from lecturecast.protocol.models import OrchestrationPlanV1_1, PresenterPlanV1_1

    assert PresenterPlanV1_1.schema_filename == "presenter-plan.schema.json"
    assert PresenterPlanV1_1.schema_dir.name == "v1.1"
    assert OrchestrationPlanV1_1.schema_filename == "orchestration-plan.schema.json"
    assert OrchestrationPlanV1_1.schema_dir.name == "v1.1"
    assert (PresenterPlanV1_1.schema_dir / PresenterPlanV1_1.schema_filename).is_file()
    assert (OrchestrationPlanV1_1.schema_dir / OrchestrationPlanV1_1.schema_filename).is_file()


# ---- ClientCapabilitiesV1_1 semantic regression (the r3 fix) ----

def _v1_1_caps() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parent / "fixtures" / "client-capabilities-v1_1.json").read_text()
    )


@pytest.mark.parametrize(
    "mutation, label",
    [
        (lambda c: c["supported_artifact_versions"].update({"creative_brief": ["1.1", "1.1"]}), "version"),
        (lambda c: c["third_party_processors"].append(dict(c["third_party_processors"][0])), "provider"),
        (lambda c: c["third_party_processors"][0].__setitem__("operations", ["photo_avatar", "photo_avatar"]), "operations"),
        (lambda c: c["third_party_processors"][0].__setitem__("features", ["idempotency_24h", "idempotency_24h"]), "features"),
    ],
)
def test_v1_1_capabilities_reject_duplicates(mutation, label) -> None:
    from lecturecast.protocol.models import ClientCapabilitiesV1_1

    caps = _v1_1_caps()
    mutation(caps)
    with pytest.raises(Exception, match=label):
        ClientCapabilitiesV1_1.model_validate(caps)


# ---- v1.1 vendored bundle integrity guard ----

V1_1_BUNDLE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src" / "lecturecast" / "protocol" / "schemas" / "v1.1"
)
# Audited bundle digest (matches lecturecast-server protocol/v1.1/protocol.lock).
# Updated for m2-5 re-vendor: error-envelope.schema.json gained m2_not_ready.
# Updated for m3-5 re-vendor: error-envelope.schema.json gained m3_not_ready.
# Updated for bgm-decouple re-vendor: creative-brief.schema.json bgm now allowed
# with avatar=none (spec §1.2 none+stock+bgm≠none → M1+M3).
V1_1_AUDITED_BUNDLE_DIGEST = "sha256:f20abcc690c0e3f8be06e5521871c8564485b118a9d7bd2931244b7a5a73dda2"


def test_v1_1_bundle_lock_is_intact_and_audited() -> None:
    """The vendored v1.1 bundle must match its lock (per-file digests + bundle
    digest) AND the audited bundle digest — catches lock/schema drift."""
    import hashlib

    lock = json.loads((V1_1_BUNDLE_DIR / "protocol.lock").read_text())
    assert lock["bundle_version"] == "1.1"
    files = lock["files"]
    assert set(files) == {
        "client-capabilities.schema.json",
        "creative-brief.schema.json",
        "decision-card-set.schema.json",
        "error-envelope.schema.json",
        "manifest-generation-out.schema.json",
        "orchestration-plan.schema.json",
        "presenter-plan.schema.json",
        "production-manifest.schema.json",
        "recovery-directive-catalog.schema.json",
    }
    for filename, expected in files.items():
        actual = "sha256:" + hashlib.sha256(
            (V1_1_BUNDLE_DIR / filename).read_bytes()
        ).hexdigest()
        assert actual == expected, f"{filename} digest drift"
    # bundle_digest = canonical digest of the files map.
    canonical = "sha256:" + hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert canonical == lock["bundle_digest"] == V1_1_AUDITED_BUNDLE_DIGEST


# ---- create_session sends protocol_version over the wire ----

def _capturing_client() -> tuple["DirectorClient", list[dict[str, Any]]]:
    from lecturecast.director import DirectorClient

    captured: list[dict[str, Any]] = []

    class _Capture:
        def request(self, *, method, url, headers, payload, timeout):
            captured.append({"method": method, "url": url, "payload": payload})
            # Minimal valid session document for DirectorClient._session.
            return 201, {
                "session_id": "sess_demo_001",
                "status": "collecting_decisions",
                "brief_version": 0,
                "catalog_version": "2026-07-16.1",
                "updated_at": NOW,
            }

    client = DirectorClient(
        server_url="https://api.lecturecast.agentmesh360.com",
        api_key="k",
        transport=_Capture(),  # type: ignore[arg-type]
    )
    return client, captured


def test_create_session_default_sends_protocol_version_1_0() -> None:
    client, captured = _capturing_client()
    client.create_session({"source_type": "topic", "title": "t", "summary": "s", "language": "zh-CN"})
    assert len(captured) == 1
    assert captured[0]["payload"]["protocol_version"] == "1.0"


def test_create_session_passes_protocol_version_1_1() -> None:
    client, captured = _capturing_client()
    client.create_session(
        {"source_type": "topic", "title": "t", "summary": "s", "language": "zh-CN"},
        protocol_version="1.1",
    )
    assert captured[0]["payload"]["protocol_version"] == "1.1"
