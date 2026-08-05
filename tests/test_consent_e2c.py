"""ConsentService withdraw + validate_submit_consent guard tests (§5.5e2c)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentConflictError,
    ConsentIntegrityError,
    ConsentService,
    ConsentStateError,
    DisclosedAsset,
    HeyGenOperationIdentity,
    PreparedOperation,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.protocol import (
    CreativeBriefV1_1,
    OrchestrationPlanV1_1,
    PresenterPlanV1_1,
    ProductionManifest,
)
from lecturecast.protocol.canonical import canonical_digest

D = "sha256:" + "a" * 64
GEN = "gen_1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"
FIXTURE_DIR = Path(__file__).parent / "fixtures"
import hashlib

def Z(seed) -> str:
    """A valid, distinct sha256 digest from any seed (for non-artifact identities)."""
    return "sha256:" + hashlib.sha256(str(seed).encode()).hexdigest()


def _identity(digests, gen=GEN) -> HeyGenOperationIdentity:
    return HeyGenOperationIdentity(
        operation_kind="video", generation_id=gen,
        manifest_digest=digests["manifest_digest"],
        request_digest=digests["request_digest"],
        credential_profile_id="heygen_env_default",
        orchestration_plan_digest=digests["orch_digest"], endpoint="/v3/videos",
    )


def _disclosure() -> ThirdPartyTransferDisclosure:
    return ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    )


def _grant_only(svc, gen=GEN):
    """Grant an operation using arbitrary (non-artifact) digests — enough for
    withdraw tests, which don't verify the artifact chain."""
    digests = {"brief_digest": Z("b"), "manifest_digest": Z("m"),
               "orch_digest": Z("o"), "request_digest": Z("r")}
    prepared = prepare_operation(_identity(digests, gen))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=digests["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    return prepared


def _real_chain(gen=GEN, avatar="photo", consent_status="granted",
                consented_at="2026-07-29T00:00:00Z"):
    """Build real schema-valid Brief/Manifest/Presenter/Orchestration with a
    chained digest set, for the guard (which isinstance-checks each)."""
    brief_p = json.loads((FIXTURE_DIR / "creative-brief-v1_1.json").read_text())
    brief_p["presenter"] = {
        "avatar": avatar, "voice_mode": "own_voice", "presenter_mode": "three_segment",
        "bgm": "none",
        "third_party_processing": {
            "provider": "heygen", "credential_mode": "byo_local",
            "consent_status": consent_status,
            "disclosure_version": "heygen-transfer-2026-07-27",
            "consented_at": consented_at,
        },
    }
    brief = CreativeBriefV1_1.model_validate(brief_p)
    brief_digest = canonical_digest(brief)
    manifest_p = json.loads((FIXTURE_DIR / "production-manifest-v1.json").read_text())
    manifest_p["generation_id"] = gen
    manifest_p["brief_digest"] = brief_digest
    manifest = ProductionManifest.model_validate(manifest_p)
    manifest_digest = canonical_digest(manifest)
    cap_d = manifest.payload["capability_digest"]
    comp_d = manifest.payload["component_catalog_digest"]
    presenter_p = {
        "schema_version": "1.1", "presenter_plan_id": "pp_1", "generation_id": gen,
        "production_manifest_digest": manifest_digest, "brief_digest": brief_digest,
        "capability_digest": cap_d, "component_catalog_digest": comp_d,
        "avatar": "photo", "presenter_mode": "three_segment",
        "pip_style": {"size_px": 320, "corner_radius_px": 16, "position": "bottom-right",
                      "margin_right_px": 48, "margin_bottom_px": 48},
        "heygen": {"base_url": "https://api.heygen.com", "auth_header": "X-Api-Key",
                   "key_source": "user_provided_local", "key_env_var": "HEYGEN_API_KEY",
                   "assets_endpoint": "/v3/assets", "videos_endpoint": "/v3/videos",
                   "asset_id_field": "data.asset_id", "status_field": "data.status",
                   "url_field": "data.video_url", "poll_interval_s": 5, "poll_max_attempts": 60},
        "segments": [{"segment_id": "seg.opening", "script_chunk_ids": [0], "label": "opening"}],
        "signature": {"algorithm": "Ed25519", "key_id": "lec.signing.v1", "value": "A" * 86 + "=="},
        "created_at": "2026-07-29T00:00:00Z", "content_expires_at": "2026-07-29T00:00:00Z",
    }
    presenter = PresenterPlanV1_1.model_validate(presenter_p)
    presenter_digest = canonical_digest(presenter)
    orch_p = {
        "schema_version": "1.1", "orchestration_plan_id": "orch_1", "generation_id": gen,
        "production_manifest_digest": manifest_digest, "brief_digest": brief_digest,
        "capability_digest": cap_d, "component_catalog_digest": comp_d,
        "presenter_plan_digest": presenter_digest,
        "bgm_enabled": False, "ffmpeg_overlay_template_id": "lec.overlay.v1",
        "timing_placeholder_contract": "{{placeholder}}", "voice_orchestration": None,
        "speed": 1.25, "bgm_genre": "none",
        "signature": {"algorithm": "Ed25519", "key_id": "lec.signing.v1", "value": "A" * 86 + "=="},
        "created_at": "2026-07-29T00:00:00Z", "content_expires_at": "2026-07-29T00:00:00Z",
    }
    orch = OrchestrationPlanV1_1.model_validate(orch_p)
    orch_digest = canonical_digest(orch)
    request_descriptor = {"video_inputs": {"avatar": "photo"}, "title": "lecturecast:t"}
    request_digest = canonical_digest(request_descriptor)
    digests = {"brief_digest": brief_digest, "manifest_digest": manifest_digest,
               "presenter_digest": presenter_digest, "orch_digest": orch_digest,
               "request_digest": request_digest}
    return brief, manifest, presenter, orch, request_descriptor, digests


def _manifest_variant(manifest, **changes):
    p = manifest.model_dump()
    p.update(changes)
    return ProductionManifest.model_validate(p)


def _orch_variant(orch, **changes):
    p = orch.model_dump()
    p.update(changes)
    return OrchestrationPlanV1_1.model_validate(p)


def _presenter_variant(presenter, **changes):
    p = presenter.model_dump()
    p.update(changes)
    return PresenterPlanV1_1.model_validate(p)


def _grant_real(svc, digests):
    prepared = prepare_operation(_identity(digests))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=digests["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    return prepared


def _fk_off(project: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(project / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    return db


def _row(project, sql, params=()):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute(sql, params).fetchone()
    db.close()
    return row


# ---- withdraw ----------------------------------------------------------

def test_withdraw_pristine_cancels_and_no_cleanup(tmp_path: Path):
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    res = svc.withdraw(prepared.operation_id)
    assert res.cleanup_required is False
    assert res.idempotent is False
    op = _row(tmp_path, "SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
              (prepared.operation_id,))
    rc = _row(tmp_path, "SELECT status, withdrawn_at FROM heygen_consent_receipts WHERE operation_id = ?",
              (prepared.operation_id,))
    assert op["status"] == "cancelled"
    assert op["consent_receipt_digest"] is None
    assert rc["status"] == "withdrawn"


@pytest.mark.parametrize("progressed", ["submitted", "failed", "submit_attempts", "remote_ref"])
def test_withdraw_non_pristine_requires_cleanup(tmp_path: Path, progressed: str):
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    db = _fk_off(tmp_path)
    if progressed == "submit_attempts":
        db.execute("UPDATE heygen_operations SET submit_attempts = 1 WHERE operation_id = ?",
                   (prepared.operation_id,))
    elif progressed == "remote_ref":
        db.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, remote_id, "
            "created_by_operation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("heygen_env_default", "video", "rem_1", prepared.operation_id,
             "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    else:
        db.execute("UPDATE heygen_operations SET status = ? WHERE operation_id = ?",
                   (progressed, prepared.operation_id))
    db.commit()
    db.close()
    res = svc.withdraw(prepared.operation_id)
    assert res.cleanup_required is True
    op = _row(tmp_path, "SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
              (prepared.operation_id,))
    assert op["consent_receipt_digest"] is None
    if progressed not in ("submitted", "failed"):
        assert op["status"] != "cancelled"


def test_withdraw_replay_keeps_cleanup_topology(tmp_path: Path):
    svc = ConsentService(tmp_path)
    # pristine: both calls cleanup_required=False
    prepared = _grant_only(svc)
    first = svc.withdraw(prepared.operation_id)
    second = svc.withdraw(prepared.operation_id)
    assert first.cleanup_required is False
    assert second.cleanup_required is False
    assert second.idempotent is True
    assert second.withdrawn_at == first.withdrawn_at

    # engaged: both calls cleanup_required=True
    engaged = _grant_only(svc, gen="gen_2")
    db = _fk_off(tmp_path)
    db.execute("UPDATE heygen_operations SET status = 'submitted' WHERE operation_id = ?",
               (engaged.operation_id,))
    db.commit()
    db.close()
    e1 = svc.withdraw(engaged.operation_id)
    e2 = svc.withdraw(engaged.operation_id)
    assert e1.cleanup_required is True and e2.cleanup_required is True


def test_withdraw_declined_or_missing_rejected(tmp_path: Path):
    from lecturecast.consent import ConsentStateError as _CSE  # noqa: F401
    svc = ConsentService(tmp_path)
    digests = {"brief_digest": Z("b"), "manifest_digest": Z("m"),
               "orch_digest": Z("o"), "request_digest": Z("r")}
    prepared = prepare_operation(_identity(digests))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="declined",
                        creative_brief_digest=digests["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    with pytest.raises(ConsentStateError):
        svc.withdraw(prepared.operation_id)
    with pytest.raises(ConsentStateError):
        svc.withdraw("lc_hg_nonexistent")


def test_withdraw_fail_closed_on_tampered_receipt(tmp_path: Path):
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    db = _fk_off(tmp_path)
    db.execute("UPDATE heygen_consent_receipts SET disclosed_assets_json = '[]' WHERE operation_id = ?",
               (prepared.operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.withdraw(prepared.operation_id)
    rc = _row(tmp_path, "SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
              (prepared.operation_id,))
    assert rc["status"] == "granted"


def test_concurrent_withdraw_transitions_once(tmp_path: Path):
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        results.append(svc.withdraw(prepared.operation_id))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if not r.idempotent) == 1
    assert sum(1 for r in results if r.idempotent) == 1
    rc = _row(tmp_path, "SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
              (prepared.operation_id,))
    assert rc["status"] == "withdrawn"


def test_withdraw_rolls_back_when_operation_update_fails(tmp_path: Path):
    """Fault injection: if the operation UPDATE fails after the receipt is
    flipped to withdrawn, the whole transaction rolls back — receipt still
    granted, consent pointer still set."""
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("CREATE TRIGGER stop_op_update BEFORE UPDATE ON heygen_operations "
               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    db.commit()
    db.close()
    with pytest.raises(sqlite3.IntegrityError):
        svc.withdraw(prepared.operation_id)
    rc = _row(tmp_path, "SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
              (prepared.operation_id,))
    op = _row(tmp_path, "SELECT consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
              (prepared.operation_id,))
    assert rc["status"] == "granted"
    assert op["consent_receipt_digest"] is not None


# ---- record_decision fault injection ----------------------------------

def test_record_decision_rolls_back_when_receipt_insert_fails(tmp_path: Path):
    from lecturecast.heygen_journal import init_database
    init_database(tmp_path).close()
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("CREATE TRIGGER stop_receipt_insert BEFORE INSERT ON heygen_consent_receipts "
               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    db.commit()
    db.close()
    svc = ConsentService(tmp_path)
    digests = {"brief_digest": Z("b"), "manifest_digest": Z("m"),
               "orch_digest": Z("o"), "request_digest": Z("r")}
    prepared = prepare_operation(_identity(digests))
    with pytest.raises(sqlite3.IntegrityError):
        svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=digests["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    n = sqlite3.connect(str(tmp_path / DB_REL)).execute(
        "SELECT COUNT(*) FROM heygen_operations WHERE operation_id = ?", (prepared.operation_id,)
    ).fetchone()[0]
    assert n == 0  # operation row must not be residual


# ---- forged PreparedOperation -----------------------------------------

def test_record_decision_rejects_forged_prepared(tmp_path: Path):
    svc = ConsentService(tmp_path)
    prepared = _grant_only(svc)
    forged = PreparedOperation(
        operation_id="lc_hg_forged", idempotency_key=prepared.idempotency_key,
        heygen_title=prepared.heygen_title, identity=prepared.identity,
    )
    with pytest.raises(ConsentConflictError):
        svc.record_decision(prepared=forged, disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=Z("b"), decision_at="2026-07-29T00:00:00Z")


# ---- validate_submit_consent guard ------------------------------------

def _guard(svc, prepared, brief, manifest, presenter, orch, req, *, in_tx=False):
    if in_tx:
        from lecturecast.heygen_journal import init_database
        conn = init_database(svc._project_dir)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            res = svc.validate_submit_consent_in_tx(
                conn, prepared=prepared, brief=brief, manifest=manifest,
                presenter_plan=presenter, orchestration_plan=orch, request_descriptor=req)
            conn.execute("COMMIT")
            return res
        finally:
            conn.close()
    return svc.validate_submit_consent(
        prepared=prepared, brief=brief, manifest=manifest, presenter_plan=presenter,
        orchestration_plan=orch, request_descriptor=req)


def test_guard_authorizes_a_properly_chained_submit(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    result = _guard(svc, prepared, brief, manifest, presenter, orch, req)
    assert result.operation_id == prepared.operation_id
    assert result.manifest_digest == dig["manifest_digest"]
    assert result.orchestration_plan_digest == dig["orch_digest"]
    assert result.request_digest == dig["request_digest"]
    assert result.generation_id == GEN


def test_guard_runs_in_caller_transaction(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    result = _guard(svc, prepared, brief, manifest, presenter, orch, req, in_tx=True)
    assert result.receipt_digest.startswith("sha256:")


def test_guard_rejects_non_protocol_objects(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)

    class _FakeDoc:
        def __init__(self, payload):
            self._payload = payload

        def model_dump(self):
            return dict(self._payload)

        @property
        def payload(self):
            return self._payload

    fake_brief = _FakeDoc(brief.model_dump())
    with pytest.raises(ConsentConflictError):
        svc.validate_submit_consent(prepared=prepared, brief=fake_brief, manifest=manifest,
                                    presenter_plan=presenter, orchestration_plan=orch, request_descriptor=req)


def test_guard_rejects_brief_not_granted(tmp_path: Path):
    svc = ConsentService(tmp_path)
    for status in ("declined", "not_applicable"):
        brief, manifest, presenter, orch, req, dig = _real_chain(consent_status=status)
        prepared = _grant_real(svc, dig)
        with pytest.raises(ConsentStateError):
            _guard(svc, prepared, brief, manifest, presenter, orch, req)


def test_guard_rejects_brief_avatar_not_photo(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain(avatar="none", consent_status="not_applicable")
    prepared = _grant_real(svc, dig)
    with pytest.raises(ConsentStateError):
        _guard(svc, prepared, brief, manifest, presenter, orch, req)


@pytest.mark.parametrize("break_", ["brief_digest", "manifest_digest", "orch_digest",
                                    "request_digest", "manifest_generation", "orch_generation",
                                    "presenter_digest", "presenter_avatar", "orch_capability"])
def test_guard_rejects_broken_chain(tmp_path: Path, break_: str):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    if break_ == "brief_digest":
        manifest = _manifest_variant(manifest, brief_digest=Z(0))
    elif break_ == "manifest_digest":
        manifest = _manifest_variant(manifest, total_frames=manifest.payload["total_frames"] + 1000)
    elif break_ == "orch_digest":
        orch = _orch_variant(orch, created_at="2026-07-28T00:00:00Z")
    elif break_ == "request_digest":
        req = dict(req, extra="tampered")
    elif break_ == "manifest_generation":
        manifest = _manifest_variant(manifest, generation_id="gen_other")
    elif break_ == "orch_generation":
        orch = _orch_variant(orch, generation_id="gen_other")
    elif break_ == "presenter_digest":
        # tamper presenter content so its digest no longer matches orch.presenter_plan_digest
        presenter = _presenter_variant(presenter, created_at="2026-07-28T00:00:00Z")
    elif break_ == "presenter_avatar":
        # avatar is const 'photo' in schema; emulate a non-photo by tampering the
        # validated payload post-hoc via a one-off subclass instance.
        presenter = _presenter_variant(presenter)  # keep valid; avatar break tested via brief path
        object.__setattr__(presenter, "_payload", {**presenter.payload, "avatar": "none"})
    elif break_ == "orch_capability":
        orch = _orch_variant(orch, capability_digest=Z(99))
    with pytest.raises(ConsentConflictError):
        _guard(svc, prepared, brief, manifest, presenter, orch, req)


def test_guard_rejects_forged_prepared(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    forged = PreparedOperation(
        operation_id="lc_hg_forged", idempotency_key=prepared.idempotency_key,
        heygen_title=prepared.heygen_title, identity=prepared.identity,
    )
    with pytest.raises(ConsentConflictError):
        _guard(svc, forged, brief, manifest, presenter, orch, req)


def test_guard_rejects_after_withdraw(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    svc.withdraw(prepared.operation_id)
    with pytest.raises(ConsentStateError):
        _guard(svc, prepared, brief, manifest, presenter, orch, req)


def test_guard_rejects_unauthorized_operation(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = prepare_operation(_identity(dig))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="declined",
                        creative_brief_digest=dig["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    with pytest.raises(ConsentStateError):
        _guard(svc, prepared, brief, manifest, presenter, orch, req)


def test_guard_vs_claim_race_is_deterministic(tmp_path: Path):
    """If e3 has already claimed (submit_attempts>0) and committed, a subsequent
    withdraw reports cleanup_required=True (remote engaged)."""
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    # Simulate the claim tx having committed an attempt.
    db = _fk_off(tmp_path)
    db.execute("UPDATE heygen_operations SET submit_attempts = 1, attempt_started_at = ? "
               "WHERE operation_id = ?", ("2026-07-29T00:00:00Z", prepared.operation_id))
    db.commit()
    db.close()
    res = svc.withdraw(prepared.operation_id)
    assert res.cleanup_required is True


def test_guard_in_tx_requires_active_transaction(tmp_path: Path):
    """validate_submit_consent_in_tx must refuse a connection with no open
    transaction, so e3 can't accidentally lose claim/guard linearization."""
    from lecturecast.heygen_journal import init_database
    svc = ConsentService(tmp_path)
    brief, manifest, presenter, orch, req, dig = _real_chain()
    prepared = _grant_real(svc, dig)
    conn = init_database(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(ConsentStateError):
            svc.validate_submit_consent_in_tx(
                conn, prepared=prepared, brief=brief, manifest=manifest,
                presenter_plan=presenter, orchestration_plan=orch, request_descriptor=req,
            )
    finally:
        conn.close()
