"""Title reconciliation verdicts + cancellation/withdrawal topology (§5.5e3d2)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lecturecast.consent import ConsentService, ConsentStateError
from lecturecast.heygen_adapter import (
    TitleCandidate, TitleQuery, TitleQueryAdapterError, TitleQueryResult,
)
from lecturecast.operation_repository import OperationRepository, ReconcileProcessor

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-reconcile-w1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


def Z(seed) -> str:
    return "sha256:" + hashlib.sha256(str(seed).encode()).hexdigest()


def _seed_reconcile(tmp_path: Path, *, receipt_status="granted", attempt_age_seconds=0,
                    gen="gen_1"):
    """A maybe-sent operation in reconciliation_required with an expired lease,
    no video resource. attempt_age_seconds moves attempt_started_at into the past."""
    from lecturecast.consent import (
        CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE, CANONICAL_PROVIDER_COST_DISCLOSURE,
        DisclosedAsset, HeyGenOperationIdentity, ThirdPartyTransferDisclosure, prepare_operation,
    )
    svc = ConsentService(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id=gen, manifest_digest=dig["manifest_digest"],
        request_digest=dig["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=dig["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared,
        disclosure=ThirdPartyTransferDisclosure(
            provider="heygen", operation_kind="video",
            disclosure_version="heygen-transfer-2026-07-27",
            disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
            data_categories=["portrait_image", "facial_biometric_template"],
            provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
            agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE),
        decision="granted", creative_brief_digest=dig["brief_digest"], decision_at=NOW)
    attempt = (datetime.fromisoformat(NOW.replace("Z", "+00:00"))
               - timedelta(seconds=attempt_age_seconds)).isoformat()
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET status='reconciliation_required', "
               "attempt_started_at=?, lease_owner='maintenance-submit-dead', "
               "lease_expires_at='2026-07-28T00:01:00+00:00', lease_fence=1 "
               "WHERE operation_id=?", (attempt, prepared.operation_id))
    if receipt_status == "withdrawn":
        db.execute("UPDATE heygen_consent_receipts SET status='withdrawn', withdrawn_at=? "
                   "WHERE operation_id=?", (NOW, prepared.operation_id))
        db.execute("UPDATE heygen_operations SET consent_receipt_digest=NULL WHERE operation_id=?",
                   (prepared.operation_id,))
    db.commit()
    db.close()
    return prepared, attempt


def _op(project, op_id):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT status, consent_receipt_digest, last_error_code, completed_at "
                     "FROM heygen_operations WHERE operation_id=?", (op_id,)).fetchone()
    db.close()
    return row


def _video_resources(project, op_id):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT r.remote_id, r.deletion_status FROM heygen_remote_resources r "
                      "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
                      "WHERE ref.operation_id=?", (op_id,)).fetchall()
    db.close()
    return rows


class _Adapter:
    def __init__(self, result): self._result = result; self.queried = []
    def query_videos_by_title(self, query):
        self.queried.append(query); 
        if isinstance(self._result, TitleQueryAdapterError): raise self._result
        return self._result
    def submit_video(self, c): ...
    def poll_video(self, r): ...


def test_exact_found_writes_resource_and_maps_processing(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_x", title=title, created_at=attempt,
                           provider_status="processing"),))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "exact_found"
    assert res.outcome.target_status == "processing"
    assert res.outcome.written_remote_ids == ("hg_x",)
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "processing"
    res_rows = _video_resources(tmp_path, prepared.operation_id)
    assert len(res_rows) == 1 and res_rows[0]["remote_id"] == "hg_x"
    assert res_rows[0]["deletion_status"] == "not_started"


def test_exact_found_completed_lands_submitted_for_poll(tmp_path: Path):
    """A completed candidate is NOT finalized here (no video_url on a title
    search); it lands submitted so e3c poll re-fetches the required URL."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_c", title=title, created_at=attempt,
                           provider_status="completed"),))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.target_status == "submitted"


def test_definitive_no_match_cancels_with_carve_out(tmp_path: Path):
    """Window fully elapsed + zero matches → cancelled, pointer NULL,
    last_error_code=reconciliation_no_match. Granted receipt kept as history."""
    # attempt 25h ago → past the 24h+5m window
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=25 * 3600)
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=())),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "definitive_no_match"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "cancelled"
    assert op["consent_receipt_digest"] is None
    assert op["last_error_code"] == "reconciliation_no_match"
    assert _video_resources(tmp_path, prepared.operation_id) == []


def test_no_match_before_window_close_is_indeterminate(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)  # window still open
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=())),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "indeterminate"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "reconciliation_required"
    assert op["last_error_code"] == "search_window_open"


def test_incomplete_query_is_indeterminate(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=25 * 3600)
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=False, candidates=())),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "indeterminate"
    assert res.outcome.last_error_code == "title_query_incomplete"


def test_multiple_matches_indeterminate(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_a", title=title, created_at=attempt, provider_status="processing"),
            TitleCandidate(remote_id="hg_b", title=title, created_at=attempt, provider_status="processing")))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "indeterminate"
    assert res.outcome.last_error_code == "multiple_matches"


def test_withdrawn_after_submit_cleanup_required(tmp_path: Path):
    """Exact-found but receipt withdrawn → resources recorded as deletion_pending,
    operation cancelled, no delivery."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600, receipt_status="withdrawn")
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_w", title=title, created_at=attempt, provider_status="completed"),))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "cleanup_required"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "cancelled"
    assert op["last_error_code"] == "consent_withdrawn_cleanup_required"
    rows = _video_resources(tmp_path, prepared.operation_id)
    assert len(rows) == 1
    assert rows[0]["deletion_status"] == "deletion_pending"


def test_title_query_adapter_error_retryable_indeterminate(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryAdapterError(code="connection_error", retryable=True)),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "indeterminate"
    assert res.outcome.last_error_code == "connection_error"
    assert res.outcome.next_retry_at is not None


def test_title_non_title_match_excluded(tmp_path: Path):
    """A candidate whose title merely contains the query but isn't exact is not a match."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=25 * 3600)
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_partial", title=f"lecturecast:{prepared.operation_id}x",
                           created_at=attempt, provider_status="processing"),))),
        now_iso=NOW, lease_seconds=60)
    # No exact match + window closed → definitive_no_match (partial title ignored)
    assert res.outcome.verdict == "definitive_no_match"


def test_record_decision_refuses_reconcile_cancelled_operation(tmp_path: Path):
    """The granted+cancelled+no-match topology is terminal history: record_decision
    must refuse to re-attach a consent pointer or re-submit."""
    from lecturecast.consent import (
        CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE, CANONICAL_PROVIDER_COST_DISCLOSURE,
        DisclosedAsset, ThirdPartyTransferDisclosure,
    )
    prepared, _ = _seed_reconcile(tmp_path, attempt_age_seconds=25 * 3600)
    proc = ReconcileProcessor(tmp_path)
    proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=())),
        now_iso=NOW, lease_seconds=60)
    svc = ConsentService(tmp_path)
    with pytest.raises(ConsentStateError):
        svc.record_decision(
            prepared=__import__("lecturecast.consent", fromlist=["prepare_operation"]).prepare_operation(
                __import__("lecturecast.consent", fromlist=["HeyGenOperationIdentity"]).HeyGenOperationIdentity(
                    operation_kind="video", generation_id="gen_1", manifest_digest=Z(2),
                    request_digest=Z(4), credential_profile_id="heygen_env_default",
                    orchestration_plan_digest=Z(3), endpoint="/v3/videos")),
            disclosure=ThirdPartyTransferDisclosure(
                provider="heygen", operation_kind="video",
                disclosure_version="heygen-transfer-2026-07-27",
                disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
                data_categories=["portrait_image", "facial_biometric_template"],
                provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
                agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE),
            decision="granted", creative_brief_digest=Z(1), decision_at=NOW)


def test_reconcile_fence_conflict_writes_nothing(tmp_path: Path):
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    with repo.begin_immediate() as conn:
        outcome = repo.apply_reconcile_outcome_in_tx(
            conn, prepared.operation_id, OWNER, claim.fence + 99, NOW,
            TitleQueryResult(query_complete=True, candidates=()),
        )
    assert outcome.verdict == "fence_conflict"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "reconciliation_required"  # unchanged


def test_claim_refuses_declined_or_missing_receipt(tmp_path: Path):
    """A reconciliation candidate must have a coherent receipt (granted or
    withdrawn). declined/missing → not_ready (no query, no delivery)."""
    from lecturecast.operation_repository import OperationRepository
    prepared, _ = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    # flip receipt to declined — not a valid reconcile candidate
    db.execute("UPDATE heygen_consent_receipts SET status='declined' WHERE operation_id=?",
               (prepared.operation_id,))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "not_ready"


def test_claim_fail_closed_on_granted_pointer_mismatch(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError
    from lecturecast.operation_repository import OperationRepository
    prepared, _ = _seed_reconcile(tmp_path, attempt_age_seconds=3600)  # granted, pointer==digest
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET consent_receipt_digest=? WHERE operation_id=?",
               ("sha256:" + "f" * 64, prepared.operation_id))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        with pytest.raises(ConsentIntegrityError):
            repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)


def test_withdrawn_incomplete_query_registers_found_resources(tmp_path: Path):
    """withdrawn + incomplete query: register discovered precise candidates as
    deletion_pending now, keep reconciling for more (do not wait for completeness)."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600, receipt_status="withdrawn")
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=False, candidates=(
            TitleCandidate(remote_id="hg_w1", title=title, created_at=attempt, provider_status="processing"),))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "indeterminate"  # keep reconciling
    assert res.outcome.written_remote_ids == ("hg_w1",)  # but registered now
    rows = _video_resources(tmp_path, prepared.operation_id)
    assert len(rows) == 1 and rows[0]["deletion_status"] == "deletion_pending"


def test_permanent_title_error_parks_for_manual_recovery(tmp_path: Path):
    prepared, _ = _seed_reconcile(tmp_path, attempt_age_seconds=3600)
    proc = ReconcileProcessor(tmp_path)
    proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryAdapterError(code="auth_failed", retryable=False)),
        now_iso=NOW, lease_seconds=60)
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "reconciliation_required"
    assert op["last_error_code"] == "manual_reconciliation_required"
    # find + claim now exclude it (no hot-loop).
    from lecturecast.operation_repository import OperationRepository
    repo = OperationRepository(tmp_path)
    assert prepared.operation_id not in [c.operation_id for c in repo.find_reconciliation_candidates(NOW)]
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "not_ready"


def test_not_found_candidate_is_not_a_match(tmp_path: Path):
    """A candidate with a remote id but provider_status 'not_found' is contradictory
    and must not count as a precise match."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=25 * 3600)
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    res = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=True, candidates=(
            TitleCandidate(remote_id="hg_nf", title=title, created_at=attempt, provider_status="not_found"),))),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.verdict == "definitive_no_match"  # not_found candidate ignored


def test_claim_fail_closed_on_tampered_receipt_content(tmp_path: Path):
    """Tampering receipt content while keeping digest/pointer must fail closed
    (the full validator recomputes the digest); the adapter is never called."""
    from lecturecast.consent import ConsentIntegrityError
    prepared, _ = _seed_reconcile(tmp_path, attempt_age_seconds=3600)  # granted, pointer==digest
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    # tamper the disclosed assets but keep receipt_digest/pointer unchanged
    db.execute("UPDATE heygen_consent_receipts SET disclosed_assets_json='[\"tampered\"]' "
               "WHERE operation_id=?", (prepared.operation_id,))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        with pytest.raises(ConsentIntegrityError):
            repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)


def test_withdrawn_multiround_cleanup_keeps_reconciling(tmp_path: Path):
    """withdrawn + incomplete: round 1 registers r1 as deletion_pending; after
    backoff, round 2 is still claimable and registers r2 (cleanup resources do
    not exclude a withdrawn op from further reconciliation)."""
    prepared, attempt = _seed_reconcile(tmp_path, attempt_age_seconds=3600, receipt_status="withdrawn")
    title = f"lecturecast:{prepared.operation_id}"
    proc = ReconcileProcessor(tmp_path)
    r1 = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=False, candidates=(
            TitleCandidate(remote_id="hg_r1", title=title, created_at=attempt, provider_status="processing"),))),
        now_iso=NOW, lease_seconds=60)
    assert r1.outcome.verdict == "indeterminate"
    assert r1.outcome.written_remote_ids == ("hg_r1",)
    # after backoff (RECONCILE_BACKOFF_SECONDS=300), a second round finds r2
    later = (datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(seconds=400)).isoformat()
    r2 = proc.reconcile_once(operation_id=prepared.operation_id, lease_owner=OWNER,
        adapter=_Adapter(TitleQueryResult(query_complete=False, candidates=(
            TitleCandidate(remote_id="hg_r2", title=title, created_at=attempt, provider_status="processing"),))),
        now_iso=later, lease_seconds=60)
    assert r2.outcome is not None  # still claimable
    assert r2.outcome.written_remote_ids == ("hg_r2",)
    rows = _video_resources(tmp_path, prepared.operation_id)
    assert {row["remote_id"] for row in rows} == {"hg_r1", "hg_r2"}
    assert all(row["deletion_status"] == "deletion_pending" for row in rows)
