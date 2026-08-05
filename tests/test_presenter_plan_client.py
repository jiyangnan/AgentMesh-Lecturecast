"""M2 PresenterPlan client consumption (§2.6 m2-6): create_presenter_plan HTTP
method, save_presenter_plan digest binding, verify_presenter_plan_signature,
_status_workflow M2 branch, and M2-context recovery suppression."""

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
from lecturecast.protocol import PresenterPlanV1_1, manifest_signing_bytes


NOW = "2026-08-04T12:00:00Z"
NOW_DT = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
PLACEHOLDER = "A" * 86 + "=="


def _presenter_plan(*, key_id: str = "fixture_key_v1") -> dict:
    """A schema-valid PresenterPlanV1_1 with a placeholder signature (the
    test helper signs it with the matching private key where required)."""
    return {
        "schema_version": "1.1",
        "presenter_plan_id": "pp_abc123",
        "generation_id": "generation_pp_001",
        "production_manifest_digest": "sha256:" + "b" * 64,
        "brief_digest": "sha256:" + "c" * 64,
        "capability_digest": "sha256:" + "d" * 64,
        "component_catalog_digest": "sha256:" + "e" * 64,
        "avatar": "photo",
        "presenter_mode": "three_segment",
        "segments": [
            {"segment_id": "seg_open", "script_chunk_ids": [0], "label": "开场"},
        ],
        "pip_style": {
            "size_px": 320, "corner_radius_px": 16,
            "position": "bottom-right", "margin_right_px": 24, "margin_bottom_px": 24,
        },
        "heygen": {
            "base_url": "https://api.heygen.com",
            "auth_header": "X-Api-Key",
            "key_source": "user_provided_local",
            "key_env_var": "HEYGEN_API_KEY",
            "assets_endpoint": "/v3/assets",
            "videos_endpoint": "/v3/videos",
            "asset_id_field": "data.asset_id",
            "status_field": "data.status",
            "url_field": "data.video_url",
            "poll_interval_s": 15,
            "poll_max_attempts": 24,
        },
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
        "created_at": NOW,
        "content_expires_at": (NOW_DT + timedelta(days=30)).isoformat(),
    }


def _keyring_for(key_id: str, *, not_before: str = "2026-07-01T00:00:00Z", not_after: str = "2030-01-01T00:00:00Z"):
    from lecturecast.manifest import PublicKeyRing, SigningKey

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
# verify_presenter_plan_signature
# --------------------------------------------------------------------------- #


def test_presenter_plan_signature_verifies_with_matching_keyring() -> None:
    from lecturecast.manifest import verify_presenter_plan_signature

    key_id = "m2_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_presenter_plan(key_id=key_id), private_key)

    result = verify_presenter_plan_signature(plan, keyring=ring)

    assert result.valid is True
    assert result.key_id == key_id
    assert result.key_status == "current"


def test_presenter_plan_tampering_is_rejected() -> None:
    from lecturecast.manifest import verify_presenter_plan_signature

    key_id = "m2_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_presenter_plan(key_id=key_id), private_key)
    plan["segments"][0]["label"] = "篡改"

    with pytest.raises(LectureCastError) as captured:
        verify_presenter_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


def test_presenter_plan_signature_rejects_out_of_window_created_at() -> None:
    """PresenterPlan HAS created_at (unlike the recovery catalog), so the
    key-window check must apply: a plan signed by a key whose validity window
    excludes created_at must be rejected."""
    from lecturecast.manifest import verify_presenter_plan_signature

    key_id = "m2_key_v1"
    # Key window does NOT cover the plan's created_at (2026-08-04).
    ring, private_key = _keyring_for(
        key_id, not_before="2026-01-01T00:00:00Z", not_after="2026-06-01T00:00:00Z"
    )
    plan = _sign_plan(_presenter_plan(key_id=key_id), private_key)

    with pytest.raises(LectureCastError) as captured:
        verify_presenter_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


def test_presenter_plan_signature_rejects_unknown_key() -> None:
    from lecturecast.manifest import verify_presenter_plan_signature

    ring, _ = _keyring_for("m2_other_v1")
    plan = _presenter_plan(key_id="m2_unknown_v1")

    with pytest.raises(LectureCastError) as captured:
        verify_presenter_plan_signature(plan, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"


# --------------------------------------------------------------------------- #
# DirectorClient.create_presenter_plan
# --------------------------------------------------------------------------- #


def _capturing_client() -> tuple[object, list[dict]]:
    from lecturecast.director import DirectorClient

    captured: list[dict] = []

    class _Capture:
        def request(self, *, method, url, headers, payload, timeout):
            captured.append({"method": method, "url": url, "payload": payload, "headers": headers})
            return 200, {
                "presenter_plan": _presenter_plan(),
                "billing": [
                    {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
                     "status": "charged", "artifact_digest": "sha256:" + "a" * 64,
                     "deducted_credits": 10, "last_error_code": None,
                     "completed_at": NOW},
                    {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "cost": 10,
                     "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
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


def test_create_presenter_plan_posts_with_approval() -> None:
    """create_presenter_plan must POST to the server route with capabilities +
    approval {approved, disclosure_version} in the payload."""
    from lecturecast.director import DirectorClient

    client, captured = _capturing_client()
    capabilities = {"schema_version": "1.1", "adapter_kind": "codex"}
    result = client.create_presenter_plan(
        "generation_pp_001",
        capabilities=capabilities,
        approved=True,
        protocol_version="1.1",
    )

    assert len(captured) == 1
    assert captured[0]["method"] == "POST"
    assert captured[0]["url"].endswith("/director/generations/generation_pp_001/presenter-plan")
    payload = captured[0]["payload"]
    assert payload["capabilities"] == capabilities
    assert payload["approval"] == {
        "approved": True,
        "disclosure_version": "heygen-transfer-2026-07-27",
    }
    assert result["presenter_plan"]["presenter_plan_id"] == "pp_abc123"
    assert result["billing"][1]["milestone"] == "presenter_plan"


def test_create_presenter_plan_disapproved_payload() -> None:
    client, captured = _capturing_client()
    client.create_presenter_plan(
        "generation_pp_001", capabilities={}, approved=False, protocol_version="1.1",
    )
    assert captured[0]["payload"]["approval"]["approved"] is False


def test_create_presenter_plan_rejects_v1_0_response() -> None:
    """The M2 response envelope is v1.1-only; parsing must reject a malformed
    envelope (fail-closed, never act on unvalidated fields)."""
    from lecturecast.director import DirectorClient

    class _Bad:
        def request(self, *, method, url, headers, payload, timeout):
            return 200, {"presenter_plan": _presenter_plan()}  # missing billing

    client = DirectorClient(
        server_url="https://api.test", api_key="k", transport=_Bad(),  # type: ignore[arg-type]
    )
    with pytest.raises(LectureCastError):
        client.create_presenter_plan(
            "generation_pp_001", capabilities={}, approved=True, protocol_version="1.1",
        )


# --------------------------------------------------------------------------- #
# _status_workflow M2 branch
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
            {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "cost": 10,
             "status": "awaiting_credits", "artifact_digest": None,
             "deducted_credits": 0, "last_error_code": "insufficient_credits",
             "completed_at": None},
        ],
    }
    gen.update(overrides)
    return gen


def test_status_workflow_manifest_ready_photo_avatar_offers_presenter_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 released (manifest charge charged) + brief avatar=photo → the workflow
    must offer director.presenter-plan create (NOT re-offer manifest.review)."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState
    from lecturecast.project import ProjectStore

    store = ProjectStore("/tmp/m2-status-wf")
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: "photo",
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready()

    wf = _status_workflow(state, gen, "/tmp", store)

    assert wf["phase"] == "presenter_plan_create_required"
    assert wf["next_action"]["id"] == "director.presenter.plan.create"
    assert wf["next_action"]["requires_user_approval"] is True


def test_status_workflow_manifest_ready_none_avatar_stays_manifest_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1 released + avatar=none (M1 own_voice path) → manifest.review unchanged
    (M2 must NOT trigger)."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState

    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: None,
    )
    state = DirectorState(_v1_2_state())
    gen = _gen_ready()

    wf = _status_workflow(state, gen, "/tmp")

    assert wf["phase"] == "script_review_required"
    assert wf["next_action"]["id"] == "manifest.review"


def test_status_workflow_awaiting_credits_still_beats_m2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Billing priority is inviolable: awaiting_credits + resume_available must
    still beat the M2 branch even when avatar=photo."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState

    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar",
        lambda project_store: "photo",
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

    wf = _status_workflow(state, gen, "/tmp")

    assert wf["phase"] == "credit_resume_required"
    assert wf["next_action"]["id"] == "director.generation.resume"


# --------------------------------------------------------------------------- #
# ProjectStore.save_presenter_plan (digest binding)
# --------------------------------------------------------------------------- #


FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Digest chain in tests/fixtures/*-v1.json (canonical bytes, compact JSON).
FIXTURE_CAPS_DIGEST = "sha256:7c9a59784ea31fdc37c087c9b29eeee8257dcf461619d5503f7fcebfbfca4755"
FIXTURE_MANIFEST_DIGEST = "sha256:c4d3b972066c7b107bfdb7870c11eeaf03d6528af16d80c5e1a8cba0f543115d"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _release_manifest_project(tmp_path: Path):
    """init → save_capabilities → save_brief → save_manifest, matching the real
    fixture digest chain (manifest binds caps 7c9a… / brief 8ab6…)."""
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    state = store.init(name="M2")
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
    plan = _presenter_plan(key_id=key_id)
    plan["production_manifest_digest"] = FIXTURE_MANIFEST_DIGEST
    plan["capability_digest"] = FIXTURE_CAPS_DIGEST
    plan.update(overrides)
    return plan


def test_save_presenter_plan_rejects_mismatched_manifest_digest(tmp_path: Path) -> None:
    """save_presenter_plan must bind to the stored production_manifest_digest —
    a plan whose production_manifest_digest differs must be rejected."""
    from lecturecast.project import ProjectStore

    store, state = _release_manifest_project(tmp_path)
    plan = _plan_bound_to_fixture(
        production_manifest_digest="sha256:" + "9" * 64,
    )

    with pytest.raises(LectureCastError) as captured:
        store.save_presenter_plan(plan, expected_revision=state.revision)

    assert captured.value.code == "manifest_incompatible"


def test_save_presenter_plan_rejects_mismatched_capability_digest(tmp_path: Path) -> None:
    """A plan not bound to the stored ClientCapabilities digest must be rejected."""
    from lecturecast.project import ProjectStore

    store, state = _release_manifest_project(tmp_path)
    plan = _plan_bound_to_fixture(
        capability_digest="sha256:" + "8" * 64,
    )

    with pytest.raises(LectureCastError) as captured:
        store.save_presenter_plan(plan, expected_revision=state.revision)

    assert captured.value.code == "manifest_incompatible"


def test_save_presenter_plan_writes_read_only_and_advances(tmp_path: Path) -> None:
    """A digest-bound, signature-valid plan is persisted read-only (0o444) and
    advances the project to presenter_plan_ready with a stored digest."""
    import os

    from lecturecast.project import ProjectStore

    store, state = _release_manifest_project(tmp_path)
    key_id = "m2_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)

    saved = store.save_presenter_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )

    assert saved.payload["status"] == "presenter_plan_ready"
    assert store.presenter_plan_path.is_file()
    assert os.stat(store.presenter_plan_path).st_mode & 0o222 == 0
    from lecturecast.protocol import canonical_digest
    persisted = json.loads(store.presenter_plan_path.read_text(encoding="utf-8"))
    assert saved.payload["presenter_plan_digest"] == canonical_digest(persisted)

    # load() must re-verify the persisted plan digest.
    reloaded = store.load()
    assert reloaded.payload["presenter_plan_digest"] == saved.payload["presenter_plan_digest"]


def test_save_presenter_plan_is_idempotent_on_same_bytes(tmp_path: Path) -> None:
    """Re-saving the identical plan must not fail (idempotent) — no double
    charge at the CLI layer on a re-run."""
    from lecturecast.project import ProjectStore

    store, state = _release_manifest_project(tmp_path)
    key_id = "m2_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)

    first = store.save_presenter_plan(
        plan, expected_revision=state.revision, keyring=ring,
    )
    second = store.save_presenter_plan(
        plan, expected_revision=first.revision, keyring=ring,
    )

    assert second == first


def test_save_presenter_plan_rejects_tampered_signature(tmp_path: Path) -> None:
    """A plan whose signature no longer matches its content must be rejected at
    save time (fail-closed — never persist an unverified plan)."""
    from lecturecast.project import ProjectStore

    store, state = _release_manifest_project(tmp_path)
    key_id = "m2_key_v1"
    ring, private_key = _keyring_for(key_id)
    plan = _sign_plan(_plan_bound_to_fixture(key_id=key_id), private_key)
    plan["segments"][0]["label"] = "篡改"

    with pytest.raises(LectureCastError) as captured:
        store.save_presenter_plan(plan, expected_revision=state.revision, keyring=ring)

    assert captured.value.code == "manifest_signature_invalid"

