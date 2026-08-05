"""M3 OrchestrationPlan client consumption (§2.6 m3-6): create_orchestration_plan
HTTP method (no approval payload), save_orchestration_plan digest binding +
verify_orchestration_plan_signature, and the _status_workflow M3 branch."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lecturecast.errors import LectureCastError
from lecturecast.manifest import PublicKeyRing, SigningKey
from lecturecast.protocol import manifest_signing_bytes


NOW = "2026-08-04T12:00:00Z"
NOW_DT = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
PLACEHOLDER = "A" * 86 + "=="


def _orchestration_plan(*, key_id: str = "fixture_key_v1") -> dict:
    """A schema-valid OrchestrationPlanV1_1 with a placeholder signature (the
    test helper signs it with the matching private key where required)."""
    return {
        "schema_version": "1.1",
        "orchestration_plan_id": "op_abc123",
        "generation_id": "generation_op_001",
        "production_manifest_digest": "sha256:" + "b" * 64,
        "brief_digest": "sha256:" + "c" * 64,
        "capability_digest": "sha256:" + "d" * 64,
        "component_catalog_digest": "sha256:" + "e" * 64,
        "presenter_plan_digest": None,
        "bgm_enabled": False,
        "bgm_genre": "none",
        "speed": 1.25,
        "ffmpeg_overlay_template_id": "overlay.template.v1",
        "timing_placeholder_contract": "{scenes[0].total_frames}",
        "voice_orchestration": {
            "ref_audio_uri": "asset://voice/ref.wav",
            "ref_text": "示例参考文本",
            "device": "mps",
            "seed": 42,
            "nfe_step": 16,
            "chunk_split_regex": "[。！？]",
            "gap_between_chunks_s": 0.1,
            "sample_rate": 24000,
        },
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
        "created_at": NOW,
        "content_expires_at": (NOW_DT + timedelta(days=30)).isoformat(),
    }


def _keyring_for(key_id: str, *, not_before: str = "2026-07-01T00:00:00Z", not_after: str = "2030-01-01T00:00:00Z"):
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    ring = PublicKeyRing(
        [SigningKey(
            key_id=key_id, algorithm="Ed25519", public_key=public_key,
            status="current", not_before=not_before, not_after=not_after,
        )]
    )
    return ring, private_key


def _sign_plan(plan: dict, private_key: Ed25519PrivateKey) -> dict:
    signed = copy.deepcopy(plan)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "key_id": signed["signature"]["key_id"],
        "value": "",
    }
    signature = private_key.sign(manifest_signing_bytes(signed))
    signed["signature"]["value"] = base64.b64encode(signature).decode()
    return signed


# --------------------------------------------------------------------------- #
# verify_orchestration_plan_signature
# --------------------------------------------------------------------------- #


def test_orchestration_plan_signature_verifies_with_matching_keyring() -> None:
    from lecturecast.manifest import verify_orchestration_plan_signature

    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_orchestration_plan(key_id=key_id), private_key)

    result = verify_orchestration_plan_signature(plan, keyring=ring)

    assert result.valid is True
    assert result.key_id == key_id
    assert result.key_status == "current"


def test_orchestration_plan_tampering_is_rejected() -> None:
    from lecturecast.manifest import verify_orchestration_plan_signature

    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_orchestration_plan(key_id=key_id), private_key)
    plan["timing_placeholder_contract"] = "篡改"

    with pytest.raises(LectureCastError) as captured:
        verify_orchestration_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


def test_orchestration_plan_signature_rejects_out_of_window_created_at() -> None:
    """OrchestrationPlan HAS created_at, so the key-window check must apply."""
    from lecturecast.manifest import verify_orchestration_plan_signature

    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(
        key_id, not_before="2026-01-01T00:00:00Z", not_after="2026-06-01T00:00:00Z"
    )
    plan = _sign_plan(_orchestration_plan(key_id=key_id), private_key)

    with pytest.raises(LectureCastError) as captured:
        verify_orchestration_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


def test_orchestration_plan_signature_rejects_unknown_key() -> None:
    from lecturecast.manifest import verify_orchestration_plan_signature

    ring, _ = _keyring_for("m3_other_v1")
    plan = _orchestration_plan(key_id="m3_unknown_v1")

    with pytest.raises(LectureCastError) as captured:
        verify_orchestration_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


def test_orchestration_plan_schema_invalid_fails_closed_as_signature_invalid() -> None:
    """A schema-invalid plan (ProtocolValidationError during model_validate) must
    fail closed as signature-invalid — never escape as a raw validation error."""
    from lecturecast.manifest import verify_orchestration_plan_signature

    key_id = "m3_key_v1"
    ring, _ = _keyring_for(key_id)
    plan = _orchestration_plan(key_id=key_id)
    plan["schema_version"] = "1.0"  # schema const mismatch

    with pytest.raises(LectureCastError) as captured:
        verify_orchestration_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


# --------------------------------------------------------------------------- #
# DirectorClient.create_orchestration_plan
# --------------------------------------------------------------------------- #


def _capturing_client() -> tuple[object, list[dict]]:
    from lecturecast.director import DirectorClient

    captured: list[dict] = []

    class _Capture:
        def request(self, *, method, url, headers, payload, timeout):
            captured.append({"method": method, "url": url, "payload": payload, "headers": headers})
            return 200, {
                "orchestration_plan": _orchestration_plan(),
                "billing": [
                    {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
                     "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
                     "deducted_credits": 10, "last_error_code": None,
                     "completed_at": NOW},
                    {"milestone": "orchestration", "artifact_type": "orchestration_plan",
                     "cost": 10, "status": "charged",
                     "artifact_digest": "sha256:" + "b" * 64,
                     "deducted_credits": 10, "last_error_code": None,
                     "completed_at": NOW},
                ],
                "recovery_catalog": None,
            }

    client = DirectorClient(
        server_url="https://api.lecturecast.agentmesh360.com",
        api_key="k", transport=_Capture(),  # type: ignore[arg-type]
    )
    return client, captured


def test_create_orchestration_plan_posts_without_approval() -> None:
    """M3 (裁决 B) carries NO approval credential: the payload must be exactly
    {capabilities}, unlike M2's {capabilities, approval}."""

    client, captured = _capturing_client()
    capabilities = {"schema_version": "1.1", "adapter_kind": "codex"}
    result = client.create_orchestration_plan(
        "generation_op_001",
        capabilities=capabilities,
        protocol_version="1.1",
    )

    assert len(captured) == 1
    assert captured[0]["method"] == "POST"
    assert captured[0]["url"].endswith("/director/generations/generation_op_001/orchestration-plan")
    payload = captured[0]["payload"]
    assert payload == {"capabilities": capabilities}  # no approval key
    assert "approval" not in payload
    assert result["orchestration_plan"]["orchestration_plan_id"] == "op_abc123"
    assert result["billing"][1]["milestone"] == "orchestration"


def test_create_orchestration_plan_rejects_v1_0_response() -> None:
    """The M3 response envelope is v1.1-only; parsing must reject a malformed
    envelope (fail-closed, never act on unvalidated fields)."""
    from lecturecast.director import DirectorClient

    class _Bad:
        def request(self, *, method, url, headers, payload, timeout):
            return 200, {"orchestration_plan": _orchestration_plan()}  # missing billing

    client = DirectorClient(
        server_url="https://api.test", api_key="k", transport=_Bad(),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError):
        client.create_orchestration_plan(
            "generation_op_001", capabilities={}, protocol_version="1.1",
        )


def test_create_orchestration_plan_http_error_uses_v1_1_envelope() -> None:
    """A non-2xx create must be parsed through the v1.1 error envelope (m3 codes
    like m3_not_ready arrive over it), not swallowed."""
    from lecturecast.director import DirectorClient

    class _Err:
        def request(self, *, method, url, headers, payload, timeout):
            return 409, {
                "detail": {
                    "code": "m3_not_ready",
                    "message": "M3 前置不满足",
                    "next_action": "先完成前置。",
                    "retryable": False,
                },
            }

    client = DirectorClient(
        server_url="https://api.test", api_key="k", transport=_Err(),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError) as captured:
        client.create_orchestration_plan(
            "generation_op_001", capabilities={}, protocol_version="1.1",
        )

    assert captured.value.code == "m3_not_ready"


# --------------------------------------------------------------------------- #
# _status_workflow M3 branch
# --------------------------------------------------------------------------- #


def _v1_2_state(**overrides) -> dict:
    state = {
        "schema_version": "1.2", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.test", "session_id": "s1",
        "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
        "adapter_kind": "codex", "adapter_version": "1.0.0",
        "protocol_version": "1.1", "generation_id": "g1", "generation_status": "queued",
        "billing_state": "charged", "resume_available": False,
        "billing_updated_at": NOW, "updated_at": NOW,
    }
    state.update(overrides)
    return state


def _gen_ready(**overrides) -> dict:
    gen = {
        "generation_id": "g1", "status": "ready", "updated_at": NOW,
        "billing_state": "charged", "resume_available": False,
        "manifest_digest": "sha256:" + "a" * 64,
        "milestone_charges": [
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
            {"milestone": "orchestration", "artifact_type": "orchestration_plan", "cost": 10,
             "status": "awaiting_credits", "artifact_digest": None,
             "deducted_credits": 0, "last_error_code": "insufficient_credits",
             "completed_at": None},
        ],
    }
    gen.update(overrides)
    return gen


def _brief_m3_applicable(monkeypatch: pytest.MonkeyPatch, *, avatar="none", voice_mode="own_voice", bgm="none"):
    """Install a _brief_m3_applicable that returns True only when the mounted
    signal matches (so the M3 workflow branch fires without a real brief)."""
    def _applicable(project_store) -> bool:
        try:
            brief = project_store.load_brief_dict()
            if not isinstance(brief, dict):
                return False
            presenter = brief.get("presenter") or {}
            if presenter.get("avatar") == "photo":
                return True
            if presenter.get("voice_mode") == "own_voice":
                return True
            if presenter.get("bgm") not in (None, "none"):
                return True
            return False
        except Exception:
            return False

    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable", _applicable
    )
    return lambda store: _applicable(store)


def test_status_workflow_photo_m2_charged_offers_orchestration_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 released + avatar=photo + M2 charged → the workflow must offer the M3
    orchestration-plan create (NOT re-offer M2 or manifest.review)."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: "photo",
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda project_store: True,
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready(
        milestone_charges=[
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
            {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
            {"milestone": "orchestration", "artifact_type": "orchestration_plan", "cost": 10,
             "status": "awaiting_credits", "artifact_digest": None,
             "deducted_credits": 0, "last_error_code": "insufficient_credits",
             "completed_at": None},
        ],
    )

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "orchestration_plan_create_required"
    assert wf["next_action"]["id"] == "director.orchestration.plan.create"
    assert wf["next_action"]["argv"][2] == "generation-orchestration-plan"


def test_status_workflow_own_voice_offers_orchestration_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 released + own_voice (no digital human) → M3 must trigger even though
    there is no M2 row (avatar=none skips the M2 branch)."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: None,  # own_voice → no M2
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda project_store: True,  # own_voice/bgm → M3
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready()

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "orchestration_plan_create_required"
    assert wf["next_action"]["id"] == "director.orchestration.plan.create"


def test_status_workflow_orchestration_charged_stays_script_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3 already charged (idempotent re-run) → must NOT re-offer create."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: None,
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda project_store: True,
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready(
        milestone_charges=[
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
            {"milestone": "orchestration", "artifact_type": "orchestration_plan", "cost": 10,
             "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
             "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
        ],
    )

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "script_review_required"
    assert wf["next_action"]["id"] == "manifest.review"


def test_status_workflow_pure_m1_project_never_offers_m3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """avatar=none + stock voice + no bgm → _brief_m3_applicable is False, so the
    workflow must stay on manifest.review even after M1 release."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: None,
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda project_store: False,
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready()

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "script_review_required"
    assert wf["next_action"]["id"] == "manifest.review"


def test_m2_charges_from_project_includes_orchestration_when_m3_done(
    tmp_path: Path,
) -> None:
    """UAT Path D idempotent re-run regression: after M3 the project carries an
    orchestration_plan_digest, so the M2 re-run projection (`_m2_charges_from_project`)
    must append the orchestration charge. Without it `_orchestration_plan_charged`
    is always False and the post-M2 workflow wrongly re-offers a completed M3
    (`orchestration_plan_create_required`) instead of `script_review_required`."""
    from lecturecast.commands.director import _m2_charges_from_project

    store, state = _release_manifest_project(tmp_path)
    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)
    state = store.save_orchestration_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )

    charges = _m2_charges_from_project(store.load())
    milestones = [c["milestone"] for c in charges]
    assert "orchestration" in milestones
    orchestration = next(c for c in charges if c["milestone"] == "orchestration")
    assert orchestration["status"] == "charged"


def test_brief_m3_applicable_bgm_triggers_without_avatar(
    tmp_path: Path,
) -> None:
    """Path C applicability (tech spec §1.2): bgm≠none must trigger M3 even for
    a pure-M1 brief (none avatar + stock voice). Guards `_brief_m3_applicable`
    so the M3 branch stays reachable if a future card makes the combination
    constructible. UAT note: the current card set cannot produce this brief —
    brief_compiler hard-codes bgm="none" for avatar≠photo."""
    from lecturecast.commands.director import _brief_m3_applicable
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.brief_path.parent.mkdir(parents=True, exist_ok=True)
    store.brief_path.write_text(json.dumps({
        "presenter": {"avatar": "none", "voice_mode": "stock", "bgm": "light_tech"},
    }))
    assert _brief_m3_applicable(store) is True


def test_brief_m3_applicable_bgm_none_avatar_none_is_false(
    tmp_path: Path,
) -> None:
    """Negative control: a pure-M1 brief (none avatar + stock voice + bgm=none)
    must NOT trigger M3 — the workflow stays on manifest.review."""
    from lecturecast.commands.director import _brief_m3_applicable
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.brief_path.parent.mkdir(parents=True, exist_ok=True)
    store.brief_path.write_text(json.dumps({
        "presenter": {"avatar": "none", "voice_mode": "stock", "bgm": "none"},
    }))
    assert _brief_m3_applicable(store) is False


def test_status_workflow_awaiting_credits_still_beats_m3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Billing priority is inviolable: awaiting_credits + resume_available must
    still beat the M3 branch."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: None,
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda project_store: True,
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready(
        billing_state="awaiting_credits", resume_available=True,
        milestone_charges=[
            {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
             "status": "awaiting_credits", "artifact_digest": None,
             "deducted_credits": 0, "last_error_code": "insufficient_credits",
             "completed_at": None},
        ],
    )

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "credit_resume_required"
    assert wf["next_action"]["id"] == "director.generation.resume"


# --------------------------------------------------------------------------- #
# ProjectStore.save_orchestration_plan (digest binding)
# --------------------------------------------------------------------------- #


FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Digest chain in tests/fixtures/*-v1.json (canonical bytes, compact JSON).
FIXTURE_CAPS_DIGEST = "sha256:7c9a59784ea31fdc37c087c9b29eeee8257dcf461619d5503f7fcebfbfca4755"
FIXTURE_MANIFEST_DIGEST = "sha256:c4d3b972066c7b107bfdb7870c11eeaf03d6528af16d80c5e1a8cba0f543115d"
FIXTURE_BRIEF_DIGEST = "sha256:8ab66ff5434cf525d908e46eae5ff6c01f7ae5739f2659103dd6640ff68b6e14"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _release_manifest_project(tmp_path: Path):
    """init → save_capabilities → save_brief → save_manifest, matching the real
    fixture digest chain (manifest binds caps 7c9a… / brief 8ab6…)."""
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    state = store.init(name="M3")
    state = store.save_capabilities(
        _fixture("client-capabilities-v1.json"), expected_revision=state.revision
    )
    state = store.save_brief(
        _fixture("creative-brief-v1.json"), expected_revision=state.revision
    )
    state = store.save_manifest(
        _fixture("production-manifest-v1.json"), expected_revision=state.revision
    )
    return store, state


def _plan_bound_to_fixture(*, key_id: str = "fixture_key_v1", **overrides) -> dict:
    plan = _orchestration_plan(key_id=key_id)
    plan["production_manifest_digest"] = FIXTURE_MANIFEST_DIGEST
    plan["capability_digest"] = FIXTURE_CAPS_DIGEST
    plan["brief_digest"] = FIXTURE_BRIEF_DIGEST
    plan.update(overrides)
    return plan


def test_save_orchestration_plan_rejects_mismatched_manifest_digest(tmp_path: Path) -> None:
    """save_orchestration_plan must bind to the stored production_manifest_digest —
    a plan whose production_manifest_digest differs must be rejected."""

    store, state = _release_manifest_project(tmp_path)
    plan = _plan_bound_to_fixture(
        production_manifest_digest="sha256:" + "9" * 64,
    )

    with pytest.raises(LectureCastError) as captured:
        store.save_orchestration_plan(plan, expected_revision=state.revision)

    assert captured.value.code == "manifest_incompatible"


def test_save_orchestration_plan_rejects_mismatched_capability_digest(tmp_path: Path) -> None:
    """A plan not bound to the stored ClientCapabilities digest must be rejected."""

    store, state = _release_manifest_project(tmp_path)
    plan = _plan_bound_to_fixture(
        capability_digest="sha256:" + "8" * 64,
    )

    with pytest.raises(LectureCastError) as captured:
        store.save_orchestration_plan(plan, expected_revision=state.revision)

    assert captured.value.code == "manifest_incompatible"


def test_save_orchestration_plan_accepts_none_presenter_plan_digest(tmp_path: Path) -> None:
    """none + own_voice path has no M2: presenter_plan_digest=None is LEGAL and
    must not be asserted (unlike a mismatched value)."""

    store, state = _release_manifest_project(tmp_path)
    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(
        _plan_bound_to_fixture(key_id=key_id, presenter_plan_digest=None), private_key
    )

    saved = store.save_orchestration_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )

    assert saved.payload["status"] == "orchestration_plan_ready"
    assert saved.payload["orchestration_plan_digest"] is not None


def test_save_orchestration_plan_writes_read_only_and_advances(tmp_path: Path) -> None:
    """A digest-bound, signature-valid plan is persisted read-only (0o444) and
    advances the project to orchestration_plan_ready with a stored digest."""
    import os


    store, state = _release_manifest_project(tmp_path)
    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)

    saved = store.save_orchestration_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )

    assert saved.payload["status"] == "orchestration_plan_ready"
    assert store.orchestration_plan_path.is_file()
    assert os.stat(store.orchestration_plan_path).st_mode & 0o222 == 0
    from lecturecast.protocol import canonical_digest
    persisted = json.loads(store.orchestration_plan_path.read_text(encoding="utf-8"))
    assert saved.payload["orchestration_plan_digest"] == canonical_digest(persisted)

    # load() must re-verify the persisted plan digest.
    reloaded = store.load()
    assert reloaded.payload["orchestration_plan_digest"] == saved.payload["orchestration_plan_digest"]


def test_save_orchestration_plan_is_idempotent_on_same_bytes(tmp_path: Path) -> None:
    """Re-saving the identical plan must not fail (idempotent) — no double
    charge at the CLI layer on a re-run."""

    store, state = _release_manifest_project(tmp_path)
    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)

    first = store.save_orchestration_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )
    second = store.save_orchestration_plan(
        plan, expected_revision=first.revision, keyring=ring,
    )

    assert second == first


def test_save_orchestration_plan_rejects_tampered_signature(tmp_path: Path) -> None:
    """A plan whose signature no longer matches its content must be rejected at
    save time (fail-closed — never persist an unverified plan)."""

    store, state = _release_manifest_project(tmp_path)
    key_id = "m3_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)
    plan["timing_placeholder_contract"] = "篡改"

    with pytest.raises(LectureCastError) as captured:
        store.save_orchestration_plan(plan, expected_revision=state.revision, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"
