"""Version-aware generation response parsing (§5.5d1)."""

from __future__ import annotations

import json
from pathlib import Path
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


# ---- d3 workflow regression (§5.5d3) ----

def test_status_workflow_billing_before_ready():
    """When billing_state=awaiting_credits + resume_available=true, the workflow
    must return director.generation.resume EVEN IF status=ready. Billing
    priority > legacy ready."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState

    state = DirectorState({
        "schema_version": "1.2", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.test", "session_id": "s1",
        "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
        "adapter_kind": "codex", "adapter_version": "1.0.0",
        "protocol_version": "1.1", "generation_id": "g1", "generation_status": "queued",
        "billing_state": "awaiting_credits", "resume_available": True,
        "billing_updated_at": "2026-07-28T12:00:00Z",
        "updated_at": "2026-07-28T12:00:00Z",
    })
    gen = {
        "generation_id": "g1", "status": "ready", "updated_at": "2026-07-28T12:00:00Z",
        "billing_state": "awaiting_credits", "resume_available": True,
    }
    wf = _status_workflow(state, gen, "/tmp")
    assert wf["phase"] == "credit_resume_required"
    assert wf["next_action"]["id"] == "director.generation.resume"
    assert wf["next_action"]["requires_user_approval"] is True


def test_status_workflow_v1_0_ready_still_manifest_review():
    """v1.0 (no billing fields) + status=ready → manifest.review unchanged."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState

    state = DirectorState({
        "schema_version": "1.0", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.test", "session_id": "s1",
        "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
        "adapter_kind": "codex", "adapter_version": "1.0.0",
        "generation_id": "g1", "generation_status": "ready",
        "updated_at": "2026-07-28T12:00:00Z",
    })
    gen = {"generation_id": "g1", "status": "ready", "updated_at": "2026-07-28T12:00:00Z"}
    wf = _status_workflow(state, gen, "/tmp")
    assert wf["phase"] == "script_review_required"
    assert wf["next_action"]["id"] == "manifest.review"


def test_state_workflow_cached_awaiting_returns_billing_refresh():
    """Cached billing_state=awaiting_credits + resume_available →
    billing_refresh_required + director.status (NOT resume)."""
    from lecturecast.commands.director import _state_workflow
    from lecturecast.director import DirectorState

    state = DirectorState({
        "schema_version": "1.2", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.test", "session_id": "s1",
        "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
        "adapter_kind": "codex", "adapter_version": "1.0.0",
        "protocol_version": "1.1", "generation_id": "g1", "generation_status": "queued",
        "billing_state": "awaiting_credits", "resume_available": True,
        "billing_updated_at": "2026-07-28T12:00:00Z",
        "updated_at": "2026-07-28T12:00:00Z",
    })
    wf = _state_workflow(Path("/tmp"), state)
    assert wf["phase"] == "billing_refresh_required"
    assert wf["next_action"]["id"] == "director.status"


# ---- d3 r2: Manifest release guard + v1.0 isolation + CLI ----

def test_can_release_manifest_v1_0_ready():
    from lecturecast.commands.director import _can_release_manifest
    assert _can_release_manifest({"status": "ready"}, protocol_version="1.0") is True


def test_can_release_manifest_v1_1_requires_charged_milestone():
    from lecturecast.commands.director import _can_release_manifest
    # ready but manifest milestone not charged → False
    assert _can_release_manifest(
        {"status": "ready", "manifest_digest": "sha256:x", "milestone_charges": [{"milestone": "manifest", "status": "charge_pending", "artifact_digest": "sha256:x"}]},
        protocol_version="1.1",
    ) is False
    # ready + manifest charged + digest match → True
    assert _can_release_manifest(
        {"status": "ready", "manifest_digest": "sha256:abc", "milestone_charges": [{"milestone": "manifest", "status": "charged", "artifact_digest": "sha256:abc"}]},
        protocol_version="1.1",
    ) is True
    # ready but no milestone_charges → False
    assert _can_release_manifest({"status": "ready"}, protocol_version="1.1") is False
    # digest mismatch → False
    assert _can_release_manifest(
        {"status": "ready", "manifest_digest": "sha256:x", "milestone_charges": [{"milestone": "manifest", "status": "charged", "artifact_digest": "sha256:y"}]},
        protocol_version="1.1",
    ) is False
    # either digest missing → False (strict)
    assert _can_release_manifest(
        {"status": "ready", "milestone_charges": [{"milestone": "manifest", "status": "charged", "artifact_digest": "sha256:a"}]},
        protocol_version="1.1",
    ) is False
    assert _can_release_manifest(
        {"status": "ready", "manifest_digest": "sha256:a", "milestone_charges": [{"milestone": "manifest", "status": "charged"}]},
        protocol_version="1.1",
    ) is False


def test_generation_resume_command_v1_1_charged_saves_manifest(tmp_path: Path, monkeypatch):
    """CLI happy-path: generation-resume on v1.1 → state updated + Manifest saved."""
    import json
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

    runner = CliRunner()
    # Set up a v1.1 project with a generation.
    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": _NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")
    state = store.update(state, generation={
        "generation_id": "gen_1", "status": "queued", "updated_at": _NOW,
        "billing_state": "awaiting_credits", "resume_available": True,
    })

    # Fake resume response: charged + ready + real manifest.
    charged_gen = _v1_1_generation(billing_state="charged")
    manifest_fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures", "production-manifest-v1.json").read_text()
    )
    from lecturecast.protocol import ProductionManifest, canonical_digest
    manifest_obj = ProductionManifest.model_validate(manifest_fixture)
    manifest_digest = canonical_digest(manifest_obj.model_dump())
    charged_gen["manifest"] = manifest_obj.model_dump()
    charged_gen["manifest_digest"] = manifest_digest
    charged_gen["milestone_charges"][0]["artifact_digest"] = manifest_digest

    class _ResumeCapture:
        def __init__(self):
            self.urls = []
        def request(self, *, method, url, headers, payload, timeout):
            self.urls.append(url)
            return 200, charged_gen

    import lecturecast.commands.director as d
    from lecturecast.director import DirectorClient
    capture = _ResumeCapture()
    d._make_client = lambda _url: DirectorClient(_url, transport=capture)

    result = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    # Resume request hit the correct endpoint with the original generation_id.
    assert any("/director/generations/gen_1/resume" in u for u in capture.urls)

    result = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["generation"]["billing_state"] == "charged"
    # State upgraded to 1.2 with billing snapshot.
    reloaded = store.load()
    assert reloaded.payload["schema_version"] == "1.2"
    assert reloaded.billing_state == "charged"
    # Manifest saved to ProjectStore with matching digest.
    project = store.project.load()
    assert project.payload["production_manifest_digest"] == manifest_digest


def test_generation_resume_command_v1_0_fails_closed(tmp_path: Path, monkeypatch):
    """generation-resume on a v1.0 project must fail-closed."""
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

    runner = CliRunner()
    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": _NOW},
        adapter_kind="codex", adapter_version="1.0.0",
    )
    result = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    assert result.exit_code == 1  # LectureCastError → SystemExit(1)
    body = json.loads(result.output)
    assert body["code"] == "manifest_incompatible"
