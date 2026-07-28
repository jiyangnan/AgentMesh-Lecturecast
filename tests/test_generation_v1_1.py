"""Version-aware generation response parsing (§5.5d1)."""

from __future__ import annotations

from typing import Any

import pytest

from lecturecast.director import DirectorClient
from lecturecast.errors import LectureCastError
from lecturecast.protocol import ManifestGenerationOutV1_1


_NOW = "2026-07-28T12:00:00Z"


def _v1_1_generation(billing_state: str = "charged", resume_available: bool = False) -> dict[str, Any]:
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
        "created_at": _NOW,
        "updated_at": _NOW,
        "completed_at": _NOW,
        "milestone_charges": [
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": _NOW},
            {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": _NOW},
            {"milestone": "orchestration", "artifact_type": "orchestration_plan", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "c" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": _NOW},
        ],
        "billing_state": billing_state,
        "resume_available": resume_available,
    }


class _Capture:
    def __init__(self, response: dict[str, Any]):
        self.response = response

    def request(self, *, method, url, headers, payload, timeout):
        return 200, self.response


def test_v1_1_get_generation_parses_milestone_charges():
    client = DirectorClient(
        server_url="https://api.test", api_key="k",
        transport=_Capture(_v1_1_generation()),  # type: ignore[arg-type]
    )
    gen = client.get_generation("gen_1", protocol_version="1.1")
    assert gen["billing_state"] == "charged"
    assert len(gen["milestone_charges"]) == 3
    assert gen["resume_available"] is False


def test_v1_1_resume_generation_calls_resume_endpoint():
    captured: list[dict[str, Any]] = []

    class _ResumeCapture:
        def request(self, *, method, url, headers, payload, timeout):
            captured.append({"method": method, "url": url})
            return 200, _v1_1_generation(billing_state="charged")

    client = DirectorClient(
        server_url="https://api.test", api_key="k",
        transport=_ResumeCapture(),  # type: ignore[arg-type]
    )
    gen = client.resume_generation("gen_1", protocol_version="1.1")
    assert captured[0]["method"] == "POST"
    assert "/resume" in captured[0]["url"]
    assert gen["billing_state"] == "charged"


def test_v1_1_generation_rejects_sensitive_fields():
    """The parser must reject responses containing ledger_id etc."""
    bad = _v1_1_generation()
    bad["ledger_id"] = 42
    client = DirectorClient(
        server_url="https://api.test", api_key="k",
        transport=_Capture(bad),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError) as exc_info:
        client.get_generation("gen_1", protocol_version="1.1")
    assert exc_info.value.code == "manifest_incompatible"


def test_v1_1_generation_rejects_duplicate_milestones():
    bad = _v1_1_generation()
    dup = dict(bad["milestone_charges"][0])
    bad["milestone_charges"].append(dup)
    client = DirectorClient(
        server_url="https://api.test", api_key="k",
        transport=_Capture(bad),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError) as exc_info:
        client.get_generation("gen_1", protocol_version="1.1")
    assert exc_info.value.code == "manifest_incompatible"


def test_v1_1_generation_rejects_milestone_wrong_order():
    """Milestone charges must be an ordered subset of manifest →
    presenter_plan → orchestration. Swapping two must fail."""
    bad = _v1_1_generation()
    # Swap presenter_plan and orchestration.
    charges = bad["milestone_charges"]
    charges[1], charges[2] = charges[2], charges[1]
    client = DirectorClient(
        server_url="https://api.test", api_key="k",
        transport=_Capture(bad),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError) as exc_info:
        client.get_generation("gen_1", protocol_version="1.1")
    assert exc_info.value.code == "manifest_incompatible"


def test_v1_1_bundle_has_generation_schema():
    from pathlib import Path
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "lecturecast" / "protocol" / "schemas" / "v1.1"
        / "manifest-generation-out.schema.json"
    )
    assert schema_path.is_file()
