"""OperationRepository claim/lease/fence + SubmitCoordinator tests (§5.5e3a)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentConflictError,
    ConsentService,
    DisclosedAsset,
    HeyGenOperationIdentity,
    PreparedOperation,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.operation_repository import (
    OperationRepository,
    OperationStateError,
    SubmitCoordinator,
)
from lecturecast.protocol import (
    CreativeBriefV1_1,
    OrchestrationPlanV1_1,
    PresenterPlanV1_1,
    ProductionManifest,
)
from lecturecast.protocol.canonical import canonical_digest
from lecturecast.heygen_journal import init_database

D = "sha256:" + "a" * 64
GEN = "gen_1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"
FIXTURE_DIR = Path(__file__).parent / "fixtures"
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-submit-worker-1"


def Z(seed) -> str:
    return "sha256:" + hashlib.sha256(str(seed).encode()).hexdigest()


def _disclosure() -> ThirdPartyTransferDisclosure:
    return ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    )


def _grant(svc, digests, gen=GEN, decision="granted"):
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id=gen, manifest_digest=digests["manifest_digest"],
        request_digest=digests["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=digests["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision=decision,
                        creative_brief_digest=digests["brief_digest"], decision_at=NOW)
    return prepared


def _real_chain(gen=GEN):
    brief_p = json.loads(
        (FIXTURE_DIR / "creative-brief-v1_1.json").read_text(encoding="utf-8")
    )
    brief_p["presenter"] = {
        "avatar": "photo", "voice_mode": "own_voice", "presenter_mode": "three_segment",
        "bgm": "none",
        "third_party_processing": {"provider": "heygen", "credential_mode": "byo_local",
                                   "consent_status": "granted",
                                   "disclosure_version": "heygen-transfer-2026-07-27",
                                   "consented_at": NOW},
    }
    brief = CreativeBriefV1_1.model_validate(brief_p)
    brief_digest = canonical_digest(brief)
    manifest_p = json.loads(
        (FIXTURE_DIR / "production-manifest-v1.json").read_text(encoding="utf-8")
    )
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
        "created_at": NOW, "content_expires_at": NOW,
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
        "created_at": NOW, "content_expires_at": NOW,
    }
    orch = OrchestrationPlanV1_1.model_validate(orch_p)
    orch_digest = canonical_digest(orch)
    request_descriptor = {"video_inputs": {"avatar": "photo"}, "title": "lecturecast:t"}
    request_digest = canonical_digest(request_descriptor)
    dig = {"brief_digest": brief_digest, "manifest_digest": manifest_digest,
           "orch_digest": orch_digest, "request_digest": request_digest}
    return brief, manifest, presenter, orch, request_descriptor, dig


def _open_tx(project: Path) -> sqlite3.Connection:
    conn = init_database(project)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _op_row(project: Path, op_id: str) -> sqlite3.Row:
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT status, lease_owner, lease_expires_at, lease_fence, submit_attempts, "
        "attempt_started_at, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
        (op_id,),
    ).fetchone()
    db.close()
    return row


# ---- claim -------------------------------------------------------------

def test_claim_eligible_leases_and_bumps_fence(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    res = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert res.status == "claimed"
    assert res.fence == 1
    assert res.submit_attempts == 1
    assert res.lease_expires_at is not None
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] == OWNER
    assert row["lease_fence"] == 1
    assert row["submit_attempts"] == 1
    assert row["attempt_started_at"].startswith("2026-07-29T00:00:00")


def test_second_claim_while_lease_active_is_busy(tmp_path: Path):
    """An active lease + an in-flight attempt → 'busy' (another worker is on
    it; wait for the lease, do not reconcile)."""
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    first = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    second = repo.claim_submit_in_tx(conn, prepared.operation_id, "maintenance-submit-other", NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert first.status == "claimed"
    assert second.status == "busy"
    # busy carries the real holder state so the caller can time its wait.
    assert second.fence == first.fence
    assert second.submit_attempts == 1
    assert second.lease_expires_at == first.lease_expires_at


def test_claim_ambiguous_attempt_not_reclaimable(tmp_path: Path):
    """The key safety invariant: an operation with attempt_started_at set (a
    maybe-sent prior attempt) is refused even after the lease expired — it must
    reconcile, never re-submit blindly."""
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("UPDATE heygen_operations SET attempt_started_at = ?, lease_owner = ?, "
               "lease_expires_at = ?, lease_fence = 1 WHERE operation_id = ?",
               ("2026-07-28T00:00:00Z", OWNER, "2026-07-28T00:01:00Z", prepared.operation_id))
    db.commit()
    db.close()
    conn = _open_tx(tmp_path)
    res = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert res.status == "ambiguous"


def test_claim_not_ready_without_consent(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig, decision="declined")  # no consent pointer
    conn = _open_tx(tmp_path)
    res = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert res.status == "not_ready"


def test_claim_requires_active_transaction(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = init_database(tmp_path)
    with pytest.raises(OperationStateError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.close()


def test_claim_rejects_bad_owner_and_lease(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    with pytest.raises(ValueError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, "bad owner!", NOW, 120)
    with pytest.raises(ValueError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 0)
    conn.execute("ROLLBACK")
    conn.close()


def test_concurrent_claim_only_one_wins(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    barrier = threading.Barrier(2)
    results = []

    def run(owner):
        barrier.wait()  # release both BEFORE any lock is held, so neither blocks the barrier
        conn = _open_tx(tmp_path)  # BEGIN IMMEDIATE — one wins, the other briefly blocks then proceeds
        try:
            results.append(repo.claim_submit_in_tx(conn, prepared.operation_id, owner, NOW, 120).status)
        finally:
            conn.execute("COMMIT")
            conn.close()

    threads = [threading.Thread(target=run, args=(f"maintenance-submit-w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("claimed") == 1
    assert results.count("busy") == 1


# ---- renew -------------------------------------------------------------

def test_renew_extends_lease_keep_fence(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    claimed = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    renewed = repo.renew_lease_in_tx(conn, prepared.operation_id, OWNER, claimed.fence,
                                     "2026-07-29T00:01:00Z", 120)
    conn.execute("COMMIT")
    conn.close()
    assert renewed.status == "renewed"
    assert renewed.fence == claimed.fence  # unchanged
    assert renewed.lease_expires_at == "2026-07-29T00:03:00+00:00"


def test_renew_expired_lease_fails(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    claimed = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, "2026-07-29T00:00:00Z", 60)
    # Lease expired at 00:01:00; renew from 00:02:00 must fail.
    renewed = repo.renew_lease_in_tx(conn, prepared.operation_id, OWNER, claimed.fence,
                                     "2026-07-29T00:02:00Z", 60)
    conn.execute("COMMIT")
    conn.close()
    assert renewed.status == "expired"


def test_renew_wrong_fence_not_held(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    renewed = repo.renew_lease_in_tx(conn, prepared.operation_id, OWNER, 999, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert renewed.status == "not_held"


# ---- coordinator -------------------------------------------------------

def test_coordinator_claims_after_guard_in_one_transaction(tmp_path: Path):
    brief, manifest, presenter, orch, req, dig = _real_chain()
    svc = ConsentService(tmp_path)
    prepared = _grant(svc, dig)
    coord = SubmitCoordinator(tmp_path)
    claim = coord.claim_for_submit(
        prepared=prepared, brief=brief, manifest=manifest, presenter_plan=presenter,
        orchestration_plan=orch, request_descriptor=req,
        lease_owner=OWNER, now_iso=NOW, lease_seconds=120,
    )
    assert claim.claim.status == "claimed"
    assert claim.consent.operation_id == prepared.operation_id
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] == OWNER


def test_coordinator_guard_failure_leaves_no_claim(tmp_path: Path):
    brief, manifest, presenter, orch, req, dig = _real_chain()
    svc = ConsentService(tmp_path)
    prepared = _grant(svc, dig)
    forged = PreparedOperation(
        operation_id="lc_hg_forged", idempotency_key=prepared.idempotency_key,
        heygen_title=prepared.heygen_title, identity=prepared.identity,
    )
    coord = SubmitCoordinator(tmp_path)
    with pytest.raises(ConsentConflictError):
        coord.claim_for_submit(
            prepared=forged, brief=brief, manifest=manifest, presenter_plan=presenter,
            orchestration_plan=orch, request_descriptor=req,
            lease_owner=OWNER, now_iso=NOW, lease_seconds=120,
        )
    # Guard failed before claim — no lease written.
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] is None
    assert row["submit_attempts"] == 0


def test_coordinator_not_claimable_raises_and_writes_nothing(tmp_path: Path):
    from lecturecast.consent import ConsentStateError
    brief, manifest, presenter, orch, req, dig = _real_chain()
    svc = ConsentService(tmp_path)
    prepared = _grant(svc, dig, decision="declined")  # no consent → guard rejects before claim
    coord = SubmitCoordinator(tmp_path)
    with pytest.raises(ConsentStateError):
        coord.claim_for_submit(
            prepared=prepared, brief=brief, manifest=manifest, presenter_plan=presenter,
            orchestration_plan=orch, request_descriptor=req,
            lease_owner=OWNER, now_iso=NOW, lease_seconds=120,
        )
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] is None


# ---- e3a round-2: fail-closed topology + tx binding + time hardening ----


def test_claim_rejects_wrong_project_connection(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    # A connection to a DIFFERENT project's journal.
    import tempfile
    other = Path(tempfile.mkdtemp())
    ConsentService(other).record_decision = lambda **k: None  # init schema only
    from lecturecast.heygen_journal import init_database
    other_conn = init_database(other)
    other_conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(OperationStateError):
        repo.claim_submit_in_tx(other_conn, prepared.operation_id, OWNER, NOW, 120)
    other_conn.execute("ROLLBACK")
    other_conn.close()


def test_claim_fail_closed_on_lease_without_attempt(tmp_path: Path):
    """A lease present without an attempt_started_at is an anomalous journal
    state; the repository refuses to overwrite it rather than silently claiming."""
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ? WHERE operation_id = ?",
               (OWNER, "2026-07-29T00:05:00+00:00", prepared.operation_id))
    db.commit()
    db.close()
    conn = _open_tx(tmp_path)
    with pytest.raises(OperationStateError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("ROLLBACK")
    conn.close()


def test_claim_fail_closed_on_pointer_receipt_mismatch(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET consent_receipt_digest = ? WHERE operation_id = ?",
               ("sha256:" + "f" * 64, prepared.operation_id))
    db.commit()
    db.close()
    conn = _open_tx(tmp_path)
    from lecturecast.operation_repository import OperationIntegrityError
    with pytest.raises(OperationIntegrityError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("ROLLBACK")
    conn.close()


def test_claim_timezones_represent_same_instant_consistently(tmp_path: Path):
    """Z, +00:00, and a non-UTC offset naming the same instant must produce the
    same canonical expiry (datetime comparison, not lexical)."""
    repo = OperationRepository(tmp_path)
    svc = ConsentService(tmp_path)
    expiries = []
    for i, now in enumerate([
        "2026-07-29T00:00:00Z",
        "2026-07-29T00:00:00+00:00",
        "2026-07-28T16:00:00-08:00",  # same instant
    ]):
        dig = {"brief_digest": Z(10 + i), "manifest_digest": Z(20 + i), "orch_digest": Z(30 + i), "request_digest": Z(40 + i)}
        prepared = _grant(svc, dig, gen=f"gen_tz_{i}")
        conn = _open_tx(tmp_path)
        res = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, now, 120)
        conn.execute("COMMIT")
        conn.close()
        assert res.status == "claimed"
        expiries.append(res.lease_expires_at)
    assert len(set(expiries)) == 1
    assert expiries[0] == "2026-07-29T00:02:00+00:00"


def test_renew_at_exact_expiry_is_expired(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    claimed = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, "2026-07-29T00:00:00Z", 60)
    # Lease expires exactly at 00:01:00; renewing AT that instant must fail.
    renewed = repo.renew_lease_in_tx(conn, prepared.operation_id, OWNER, claimed.fence,
                                     "2026-07-29T00:01:00Z", 60)
    conn.execute("COMMIT")
    conn.close()
    assert renewed.status == "expired"


def test_renew_after_withdraw_fails(tmp_path: Path):
    """Withdraw clears the consent pointer; a worker must not keep renewing a
    submit lease on a withdrawn operation."""
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    conn = _open_tx(tmp_path)
    claimed = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    svc.withdraw(prepared.operation_id)  # clears pointer (engaged → cleanup_required)
    conn = _open_tx(tmp_path)
    renewed = repo.renew_lease_in_tx(conn, prepared.operation_id, OWNER, claimed.fence, NOW, 120)
    conn.execute("COMMIT")
    conn.close()
    assert renewed.status == "not_held"


def test_begin_immediate_context_manager_commits_and_tightens(tmp_path: Path):
    import os, stat
    repo = OperationRepository(tmp_path)
    svc = ConsentService(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    with repo.begin_immediate() as conn:
        res = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
        assert res.status == "claimed"
    # Committed + permissions tightened.
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] == OWNER
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(tmp_path / DB_REL).st_mode) == 0o600


def test_begin_immediate_rolls_back_on_exception(tmp_path: Path):
    repo = OperationRepository(tmp_path)
    svc = ConsentService(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    with pytest.raises(RuntimeError):
        with repo.begin_immediate() as conn:
            repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
            raise RuntimeError("injected")
    row = _op_row(tmp_path, prepared.operation_id)
    assert row["lease_owner"] is None  # rolled back


def test_claim_fail_closed_on_half_lease_state(tmp_path: Path):
    """attempt_started_at set with only one of lease_owner/lease_expires_at is a
    corrupt topology — fail-closed, not 'ambiguous'."""
    from lecturecast.operation_repository import OperationIntegrityError
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    # attempt + lease_owner but NO lease_expires_at → half lease.
    db.execute("UPDATE heygen_operations SET attempt_started_at = ?, lease_owner = ?, "
               "lease_fence = 1 WHERE operation_id = ?",
               ("2026-07-28T00:00:00+00:00", OWNER, prepared.operation_id))
    db.commit()
    db.close()
    conn = _open_tx(tmp_path)
    with pytest.raises(OperationIntegrityError):
        repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    conn.execute("ROLLBACK")
    conn.close()
