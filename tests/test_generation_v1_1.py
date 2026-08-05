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


def _signed_catalog_keyring(key_id: str):
    """Return (PublicKeyRing, Ed25519PrivateKey) for a fresh key."""
    import base64 as _b64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from lecturecast.manifest import PublicKeyRing, SigningKey

    private_key = Ed25519PrivateKey.generate()
    public_key = _b64.b64encode(
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
                status="current",
                not_before="2026-01-01T00:00:00Z",
                not_after="2027-01-01T00:00:00Z",
            )
        ]
    )
    return ring, private_key


def _sign_bytes(payload: dict, private_key) -> dict:
    """Blank signature.value, sign canonical bytes, fill base64."""
    import base64 as _b64

    from lecturecast.protocol import manifest_signing_bytes

    signed = json.loads(json.dumps(payload))
    signed["signature"] = {
        "algorithm": "Ed25519",
        "key_id": signed["signature"]["key_id"],
        "value": "",
    }
    value = private_key.sign(manifest_signing_bytes(signed))
    signed["signature"]["value"] = _b64.b64encode(value).decode()
    return signed


def _signed_catalog_for(failure_kind: str, key_id: str, private_key) -> dict:
    """A signed catalog with a single directive for the given failure_kind."""
    directive = {
        "failure_kind": failure_kind,
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
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": "A" * 86 + "=="},
    }
    signed_directive = _sign_bytes(directive, private_key)
    catalog = {
        "catalog_version": "recovery_base_v1",
        "directives": {failure_kind: signed_directive},
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": "A" * 86 + "=="},
    }
    return _sign_bytes(catalog, private_key)


def _verify_with(key_id: str, ring, catalog) -> Any:
    """Verify a signed catalog against the injected keyring (test helper)."""
    from lecturecast.manifest import verify_recovery_catalog_signature

    return verify_recovery_catalog_signature(catalog, keyring=ring)


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
    # legacy single-charge M1 fallback (裁决 A: no manifest charge row; the
    # worker keeps M1 on generation.deducted_credits): ready + charged +
    # delivered manifest_digest → True.
    assert _can_release_manifest(
        {"status": "ready", "deducted_credits": 10, "manifest_digest": "sha256:abc"},
        protocol_version="1.1",
    ) is True
    # fallback fails when the generation was not charged (0 credits)
    assert _can_release_manifest(
        {"status": "ready", "deducted_credits": 0, "manifest_digest": "sha256:abc"},
        protocol_version="1.1",
    ) is False
    # fallback fails when the manifest digest is absent
    assert _can_release_manifest(
        {"status": "ready", "deducted_credits": 10}, protocol_version="1.1",
    ) is False
    # the milestone-charge path still takes precedence over the fallback:
    # an explicit uncharged manifest milestone is NOT released even though the
    # generation reports deducted credits.
    assert _can_release_manifest(
        {"status": "ready", "deducted_credits": 10, "manifest_digest": "sha256:abc",
         "milestone_charges": [{"milestone": "manifest", "status": "charge_pending", "artifact_digest": "sha256:abc"}]},
        protocol_version="1.1",
    ) is False
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


# ---- d4: resume error mapping (§5.5d4) ----

def _setup_resume_project(tmp_path: Path, monkeypatch, status_code: int, error_body: dict):
    """Set up a v1.1 project with a generation and a fake transport that returns
    an error for the resume call."""
    import json as _json
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

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
    before_revision = state.revision

    class _ErrorCapture:
        def request(self, *, method, url, headers, payload, timeout):
            return status_code, error_body

    import lecturecast.commands.director as d
    from lecturecast.director import DirectorClient
    capture = _ErrorCapture()
    d._make_client = lambda _url: DirectorClient(_url, transport=capture)

    runner = CliRunner()
    result = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    return result, store, before_revision


def test_resume_402_keeps_state_and_returns_top_up_action(tmp_path: Path, monkeypatch):
    result, store, before_rev = _setup_resume_project(tmp_path, monkeypatch, 402, {
        "detail": {"code": "insufficient_credits", "message": "余额不足。",
                   "next_action": "充值后重试。", "retryable": False},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["error"]["code"] == "insufficient_credits"
    assert body["workflow"]["phase"] == "credit_top_up_required"
    assert body["workflow"]["next_action"]["id"] == "director.generation.resume"
    # State unchanged.
    reloaded = store.load()
    assert reloaded.revision == before_rev


def test_resume_409_in_progress_returns_status(tmp_path: Path, monkeypatch):
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 409, {
        "detail": {"code": "generation_in_progress", "message": "正在处理中。",
                   "next_action": "稍后查看状态。", "retryable": True},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "billing_refresh_required"
    assert body["workflow"]["next_action"]["id"] == "director.status"


def test_resume_409_blocked_stops(tmp_path: Path, monkeypatch):
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 409, {
        "detail": {"code": "idempotency_conflict", "message": "计费冲突。",
                   "next_action": "联系支持。", "retryable": False},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "generation_blocked"
    assert body["workflow"]["next_action"]["id"] == "workflow.stop"


def test_resume_503_retryable_returns_status(tmp_path: Path, monkeypatch):
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 503, {
        "detail": {"code": "core_unavailable", "message": "暂时不可用。",
                   "next_action": "稍后重试。", "retryable": True},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "generation_recovery_required"
    assert body["workflow"]["next_action"]["id"] == "director.status"


def test_resume_503_non_retryable_stops(tmp_path: Path, monkeypatch):
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 503, {
        "detail": {"code": "core_deduct_mapping_error", "message": "扣费异常。",
                   "next_action": "联系支持。", "retryable": False},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "generation_blocked"
    assert body["workflow"]["next_action"]["id"] == "workflow.stop"


def test_resume_404_stops_without_guessing(tmp_path: Path, monkeypatch):
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 404, {
        "detail": {"code": "generation_not_found", "message": "未找到。",
                   "next_action": "检查 generation_id。", "retryable": False},
    })
    assert result.exit_code == 1  # error → exit 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "generation_unavailable"
    assert body["workflow"]["next_action"]["id"] == "workflow.stop"


def test_resume_error_retryable_string_false_not_coerced(tmp_path: Path, monkeypatch):
    """retryable='false' (string) must not be coerced to True."""
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 503, {
        "detail": {"code": "core_unavailable", "message": "err",
                   "next_action": "x", "retryable": "false"},
    })
    assert result.exit_code == 1  # error → exit 1 (malformed envelope)
    body = json.loads(result.output)
    # v1.1 schema rejects retryable="false" (string) → manifest_incompatible via generic fail().
    assert body["code"] == "manifest_incompatible"


# ---- §5.5e6 #121: recovery-directive wiring on the resume error path ----

def _setup_resume_project_with_catalog(tmp_path: Path, monkeypatch, status_code: int, error_body: dict, catalog: dict):
    """Like _setup_resume_project but the v1.3 state persists a recovery_catalog,
    and the resume call returns an error. The command's _recovery_workflow is
    patched to accept the test-signed catalog via the injected keyring."""
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

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
        "recovery_catalog": catalog,
    })
    assert state.payload["schema_version"] == "1.3"

    class _ErrorCapture:
        def request(self, *, method, url, headers, payload, timeout):
            return status_code, error_body

    import lecturecast.commands.director as d
    from lecturecast.director import DirectorClient
    d._make_client = lambda _url: DirectorClient(_url, transport=_ErrorCapture())

    runner = CliRunner()
    result = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    return result


def test_resume_error_emits_recovery_workflow_when_catalog_matches(tmp_path: Path, monkeypatch):
    """F-C12: on the generation-resume error path, a persisted recovery catalog
    whose directive matches the mapped failure_kind yields a recovery phase
    (recovery_directive_required) instead of the generic _resume_error_workflow."""
    import lecturecast.commands.director as d
    from lecturecast.manifest import VerificationResult

    key_id = "recovery_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)
    # Accept the test-signed catalog (the command path uses the real keyring).
    monkeypatch.setattr(
        d,
        "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )

    result = _setup_resume_project_with_catalog(tmp_path, monkeypatch, 402, {
        "detail": {"code": "insufficient_credits", "message": "余额不足。",
                   "next_action": "充值后重试。", "retryable": False},
    }, catalog)

    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "main_blocker_recovery_required"
    assert body["workflow"]["next_action"]["id"] == "director.recovery.decide"
    assert body["workflow"]["next_action"]["kind"] == "host_choice"
    assert body["workflow"]["next_action"]["requires_user_approval"] is True


def test_resume_error_falls_back_when_catalog_missing(tmp_path: Path, monkeypatch):
    """F-C13: without a persisted catalog the resume error path must fall back
    to the existing _resume_error_workflow (no regression)."""
    result, store, _ = _setup_resume_project(tmp_path, monkeypatch, 402, {
        "detail": {"code": "insufficient_credits", "message": "余额不足。",
                   "next_action": "充值后重试。", "retryable": False},
    })
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "credit_top_up_required"
    assert body["workflow"]["next_action"]["id"] == "director.generation.resume"


def test_resume_error_falls_back_when_catalog_unverified(tmp_path: Path, monkeypatch):
    """F-C13: a persisted but UNVERIFIED catalog (signed with a key the real
    keyring does not trust) must fall through to _resume_error_workflow —
    fail-closed: never present a directive for an unverified catalog."""
    import lecturecast.commands.director as d
    from lecturecast.errors import LectureCastError

    key_id = "untrusted_key_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)
    # verify_recovery_catalog_signature uses the REAL keyring (no such key) → raise.
    def _real_verify_fails(c, keyring=None):
        raise LectureCastError(
            code="manifest_signature_invalid", message="签名无效。",
            next_action="不要根据未验签内容继续。",
        )

    monkeypatch.setattr(d, "_verify_catalog_signature", _real_verify_fails)

    result = _setup_resume_project_with_catalog(tmp_path, monkeypatch, 402, {
        "detail": {"code": "insufficient_credits", "message": "余额不足。",
                   "next_action": "充值后重试。", "retryable": False},
    }, catalog)

    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["workflow"]["phase"] == "credit_top_up_required"  # fallback


def test_full_recovery_chain_status_resume_charged_review(tmp_path: Path, monkeypatch):
    """Complete recovery chain: status → awaiting_credits → resume → charged →
    manifest.review. Same project, sequential transport responses, resume called
    exactly once, state upgrades, Manifest only saved after charged+digest."""
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore
    from lecturecast.protocol import ProductionManifest, canonical_digest

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

    store = DirectorStateStore(tmp_path)
    store.project.init(name="T")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": _NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")

    # Build a real Manifest fixture.
    manifest_fixture = json.loads(
        Path(__file__).parent.joinpath("fixtures", "production-manifest-v1.json").read_text()
    )
    manifest_obj = ProductionManifest.model_validate(manifest_fixture)
    manifest_digest = canonical_digest(manifest_obj.model_dump())

    awaiting_gen = _v1_1_generation(billing_state="awaiting_credits", resume_available=True)
    charged_gen = _v1_1_generation(billing_state="charged")
    charged_gen["manifest"] = manifest_obj.model_dump()
    charged_gen["manifest_digest"] = manifest_digest
    charged_gen["milestone_charges"][0]["artifact_digest"] = manifest_digest

    call_count = {"status": 0, "resume": 0}

    class _ChainCapture:
        def request(self, *, method, url, headers, payload, timeout):
            if "/resume" in url:
                call_count["resume"] += 1
                return 200, charged_gen
            # status / get_generation
            call_count["status"] += 1
            if call_count["status"] == 1:
                return 200, awaiting_gen
            return 200, charged_gen

    import lecturecast.commands.director as d
    from lecturecast.director import DirectorClient
    d._make_client = lambda _url: DirectorClient(_url, transport=_ChainCapture())

    runner = CliRunner()

    # Step 1: status → awaiting_credits → workflow offers resume.
    r1 = runner.invoke(app, ["director", "status", str(tmp_path), "--json"])
    assert r1.exit_code == 0, r1.output
    body1 = json.loads(r1.output)
    assert body1["workflow"]["phase"] == "credit_resume_required"
    assert body1["workflow"]["next_action"]["id"] == "director.generation.resume"

    # Step 2: generation-resume → charged → Manifest saved → manifest.review.
    r2 = runner.invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])
    assert r2.exit_code == 0, r2.output
    body2 = json.loads(r2.output)
    assert body2["generation"]["billing_state"] == "charged"
    assert body2["workflow"]["phase"] == "script_review_required"
    assert body2["workflow"]["next_action"]["id"] == "manifest.review"

    # Resume called exactly once.
    assert call_count["resume"] == 1, f"resume called {call_count['resume']} times"

    # State upgraded to 1.2 + charged.
    reloaded = store.load()
    assert reloaded.payload["schema_version"] == "1.2"
    assert reloaded.billing_state == "charged"

    # Manifest saved with matching digest.
    project = store.project.load()
    assert project.payload["production_manifest_digest"] == manifest_digest

    # Generation ID never changed.
    assert reloaded.generation_id == "gen_1"


@pytest.mark.parametrize("bad_body", [
    {},
    {"detail": None},
    {"detail": "string error"},
    {"detail": [1, 2, 3]},
])
def test_resume_v1_1_malformed_error_detail_fails_closed(tmp_path: Path, monkeypatch, bad_body):
    """v1.1: missing/null/string/list detail → manifest_incompatible, no workflow,
    exit 1, state unchanged."""
    result, store, before_rev = _setup_resume_project(tmp_path, monkeypatch, 503, bad_body)
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["code"] == "manifest_incompatible"
    assert "workflow" not in body
    reloaded = store.load()
    assert reloaded.revision == before_rev


# ---- §2.6 m2-6d: M2-context recovery suppression ----------------------------
#
# In the M2 presenter_plan phase, a resume-402 (insufficient_credits) must NOT
# present the M1 base-catalog directive (m1_insufficient_credits — that 话术
# belongs to the M1 manifest phase). The provider catalog delivered with the
# M2 create response has no m1 directive, so the lookup would naturally miss —
# but a persisted BASE catalog would wrongly match. `_recovery_workflow` takes
# m2_context and suppresses the m1 mapping, falling through to the generic
# credit_top_up_required.

def _insufficient_credits_error() -> LectureCastError:
    return LectureCastError(
        code="insufficient_credits",
        message="余额不足。",
        next_action="充值后重试。",
        http_status=402,
        retryable=False,
    )


def test_recovery_workflow_m2_context_suppresses_m1_directive(monkeypatch, tmp_path):
    """m2-6d: with m2_context=True, a base catalog carrying the m1 directive must
    NOT present it — the resume-402 falls through (None → generic 话术)."""
    import lecturecast.commands.director as d

    key_id = "recovery_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)
    monkeypatch.setattr(
        d,
        "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )

    result = d._recovery_workflow(
        _insufficient_credits_error(), catalog, str(tmp_path), m2_context=True,
    )

    assert result is None  # falls through to _resume_error_workflow → credit_top_up_required


def test_recovery_workflow_m1_context_still_presents_directive(monkeypatch, tmp_path):
    """m2-6d: WITHOUT m2_context (M1 phase), the same catalog + error must still
    present the m1 directive — no regression to the M1 recovery path."""
    import lecturecast.commands.director as d

    key_id = "recovery_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)
    monkeypatch.setattr(
        d,
        "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )

    result = d._recovery_workflow(
        _insufficient_credits_error(), catalog, str(tmp_path), m2_context=False,
    )

    assert result is not None
    assert result["phase"] == "main_blocker_recovery_required"
    assert result["next_action"]["id"] == "director.recovery.decide"


def test_recovery_workflow_m2_context_does_not_suppress_heygen_directive(monkeypatch, tmp_path):
    """m2-6d: m2_context only suppresses the M1 insufficient_credits mapping —
    a heygen_* directive (provider catalog) must still present normally."""
    import lecturecast.commands.director as d

    key_id = "recovery_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("heygen_key_invalid", key_id, private_key)
    monkeypatch.setattr(
        d,
        "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )
    error = LectureCastError(
        code="heygen_key_invalid", message="HeyGen key 无效。",
        next_action="更新 key。", http_status=502, retryable=False,
    )

    result = d._recovery_workflow(error, catalog, str(tmp_path), m2_context=True)

    assert result is not None
    assert result["next_action"]["id"] == "director.recovery.decide"


def test_project_in_m2_context_true_when_plan_persisted(tmp_path: Path):
    """m2-6d: a project with a persisted presenter-plan.json is in M2 context."""
    import lecturecast.commands.director as d
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.init(name="M2ctx")
    (store.directory / "presenter-plan.json").write_text("{}", encoding="utf-8")

    assert d._project_in_m2_context(tmp_path) is True


def test_project_in_m2_context_false_without_plan(tmp_path: Path):
    """m2-6d: a project without a presenter plan is NOT in M2 context (keeps M1 话术)."""
    import lecturecast.commands.director as d
    from lecturecast.project import ProjectStore

    ProjectStore(tmp_path).init(name="M1only")

    assert d._project_in_m2_context(tmp_path) is False


def test_resume_m2_context_402_uses_generic_top_up(monkeypatch, tmp_path: Path):
    """End-to-end: after an M2 create persisted the plan, a resume-402 must emit
    the generic credit_top_up_required — NOT the M1 m1_insufficient_credits
    directive even when a base catalog is present."""
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

    key_id = "recovery_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)

    import lecturecast.commands.director as d
    monkeypatch.setattr(
        d, "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )

    store = DirectorStateStore(tmp_path)
    store.project.init(name="M2resume")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": _NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")
    # Persist the presenter plan (the M2 create signal) + a base catalog in state.
    from lecturecast.project import ProjectStore
    pstore = ProjectStore(tmp_path)
    (pstore.directory / "presenter-plan.json").write_text("{}", encoding="utf-8")
    state = store.update(state, generation={
        "generation_id": "gen_1", "status": "queued", "updated_at": _NOW,
        "billing_state": "awaiting_credits", "resume_available": True,
        "recovery_catalog": catalog,
    })

    class _ErrorCapture:
        def request(self, *, method, url, headers, payload, timeout):
            return 402, {
                "detail": {"code": "insufficient_credits", "message": "余额不足。",
                           "next_action": "充值后重试。", "retryable": False},
            }

    d._make_client = lambda _url: DirectorClient("https://api.test", transport=_ErrorCapture())

    result = CliRunner().invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])

    assert result.exit_code == 1
    body = json.loads(result.output)
    # The M2 context suppresses the m1 directive → generic top-up 话术.
    assert body["workflow"]["phase"] == "credit_top_up_required"
    assert body["workflow"]["next_action"]["id"] == "director.generation.resume"


# ---- §2.6 m3-6d: M3-context recovery suppression ----------------------------
#
# M3 shares the M2 suppression signal (`_project_in_m2_context` now treats a
# persisted orchestration-plan.json as an M3 context). In the M3 phase a
# resume-402 (insufficient_credits) must NOT present the M1 base-catalog
# directive — it falls through to the generic credit_top_up_required.


def test_project_in_m3_context_true_when_orchestration_plan_persisted(tmp_path: Path):
    """m3-6d: a project with a persisted orchestration-plan.json is in M3 context —
    `_project_in_m2_context` must also fire on the orchestration-plan signal."""
    import lecturecast.commands.director as d
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.init(name="M3ctx")
    (store.directory / "orchestration-plan.json").write_text("{}", encoding="utf-8")

    assert d._project_in_m2_context(tmp_path) is True


def test_resume_m3_context_402_uses_generic_top_up(monkeypatch, tmp_path: Path):
    """End-to-end: after an M3 create persisted the orchestration plan, a
    resume-402 must emit the generic credit_top_up_required — NOT the M1
    m1_insufficient_credits directive even when a base catalog is present."""
    from typer.testing import CliRunner
    from lecturecast.cli import app
    from lecturecast.director import DirectorStateStore

    monkeypatch.setattr("lecturecast.commands.director.require_commercial_access", lambda: None)
    monkeypatch.setattr("lecturecast.commands.director.require_project_host_workflow", lambda *a, **kw: None)
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")

    key_id = "recovery_m3_test_v1"
    ring, private_key = _signed_catalog_keyring(key_id)
    catalog = _signed_catalog_for("m1_insufficient_credits", key_id, private_key)

    import lecturecast.commands.director as d
    monkeypatch.setattr(
        d, "_verify_catalog_signature",
        lambda c, keyring=None: _verify_with(key_id, ring, c),
    )

    store = DirectorStateStore(tmp_path)
    store.project.init(name="M3resume")
    state = store.create(
        server_url="https://api.test",
        session={"session_id": "s1", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": _NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    state = store.update(state, generation_id="gen_1", generation_status="queued")
    # Persist the orchestration plan (the M3 create signal) + a base catalog.
    from lecturecast.project import ProjectStore
    pstore = ProjectStore(tmp_path)
    (pstore.directory / "orchestration-plan.json").write_text("{}", encoding="utf-8")
    state = store.update(state, generation={
        "generation_id": "gen_1", "status": "queued", "updated_at": _NOW,
        "billing_state": "awaiting_credits", "resume_available": True,
        "recovery_catalog": catalog,
    })

    class _ErrorCapture:
        def request(self, *, method, url, headers, payload, timeout):
            return 402, {
                "detail": {"code": "insufficient_credits", "message": "余额不足。",
                           "next_action": "充值后重试。", "retryable": False},
            }

    d._make_client = lambda _url: DirectorClient("https://api.test", transport=_ErrorCapture())

    result = CliRunner().invoke(app, ["director", "generation-resume", str(tmp_path), "--json"])

    assert result.exit_code == 1
    body = json.loads(result.output)
    # The M3 context suppresses the m1 directive → generic top-up 话术.
    assert body["workflow"]["phase"] == "credit_top_up_required"
    assert body["workflow"]["next_action"]["id"] == "director.generation.resume"
