"""Protocol-version negotiation (§5.5a)."""

from __future__ import annotations

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
    """A v1.0 state file that omits protocol_version loads as 1.0 (no rejection,
    no silent downgrade — it simply IS 1.0)."""
    payload = _v1_0_payload()
    assert "protocol_version" not in payload
    state = DirectorStateStore._validate(payload)
    assert state.protocol_version == "1.0"


def test_state_pins_protocol_version_1_1() -> None:
    payload = _v1_0_payload() | {"protocol_version": "1.1"}
    state = DirectorStateStore._validate(payload)
    assert state.protocol_version == "1.1"


def test_state_rejects_unknown_protocol_version() -> None:
    payload = _v1_0_payload() | {"protocol_version": "9.9"}
    with pytest.raises(ValueError, match="unsupported protocol_version"):
        DirectorStateStore._validate(payload)


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
