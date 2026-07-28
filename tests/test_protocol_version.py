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


def test_v1_1_state_requires_protocol_version_1_1() -> None:
    payload = dict(_v1_0_payload()) | {"schema_version": "1.1", "protocol_version": "1.0"}
    with pytest.raises(ValueError, match="requires protocol_version=1.1"):
        DirectorStateStore._validate(payload)


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


# ---- v1.1 vendored bundle integrity guard ----

V1_1_BUNDLE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src" / "lecturecast" / "protocol" / "schemas" / "v1.1"
)
# Audited bundle digest (matches lecturecast-server protocol/v1.1/protocol.lock).
V1_1_AUDITED_BUNDLE_DIGEST = "sha256:5c8e15d1514fce97445ccd0540401eef6bafd54405b0960d4003ee819707f2fc"


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
        "orchestration-plan.schema.json",
        "presenter-plan.schema.json",
        "production-manifest.schema.json",
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
