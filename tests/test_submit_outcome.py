"""SubmitProcessor outcome state machine + remote resource write (§5.5e3b)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService,
    DisclosedAsset,
    HeyGenOperationIdentity,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.heygen_adapter import HeyGenAdapterError, SubmitAccepted
from lecturecast.operation_repository import (
    OperationRepository,
    SubmitProcessor,
)

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-submit-worker-1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


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


def _claim(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id="gen_1", manifest_digest=dig["manifest_digest"],
        request_digest=dig["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=dig["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=dig["brief_digest"], decision_at=NOW)
    with repo.begin_immediate() as conn:
        claim = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    assert claim.status == "claimed"
    return prepared, claim


def _op(project: Path, op_id: str) -> sqlite3.Row:
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT status, lease_owner, lease_expires_at, lease_fence, submit_attempts, "
        "attempt_started_at, submitted_at, completed_at, next_retry_at, last_error_code, "
        "provider_status FROM heygen_operations WHERE operation_id = ?", (op_id,)
    ).fetchone()
    db.close()
    return row


def _video_resource(project: Path, op_id: str):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT r.resource_id, r.remote_id, r.resource_kind, r.retention_mode, "
        "ref.operation_id AS ref_op FROM heygen_remote_resources r "
        "JOIN heygen_resource_operation_refs ref ON ref.resource_id = r.resource_id "
        "WHERE ref.operation_id = ?", (op_id,)
    ).fetchone()
    db.close()
    return row


def test_accepted_with_remote_id_submits_and_writes_resource(tmp_path: Path):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
        now_iso=NOW, outcome=SubmitAccepted(remote_id="hg_vid_123", provider_status="processing"),
    )
    assert outcome.status == "submitted"
    assert outcome.remote_resource_id is not None
    assert outcome.last_error_code is None
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submitted"
    assert op["lease_owner"] is None           # lease cleared
    assert op["lease_fence"] == claim.fence     # fence retained
    assert op["attempt_started_at"] is not None  # the attempt that succeeded
    assert op["submitted_at"] is not None
    res = _video_resource(tmp_path, prepared.operation_id)
    assert res is not None
    assert res["remote_id"] == "hg_vid_123"
    assert res["resource_kind"] == "video"
    assert res["retention_mode"] == "ephemeral"
    assert res["ref_op"] == prepared.operation_id


def test_accepted_without_remote_id_is_reconciliation(tmp_path: Path):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
        now_iso=NOW, outcome=SubmitAccepted(remote_id=""),
    )
    assert outcome.status == "reconciliation_required"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "reconciliation_required"
    assert op["last_error_code"] == "unknown"
    assert _video_resource(tmp_path, prepared.operation_id) is None


def test_maybe_sent_error_is_reconciliation(tmp_path: Path):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    err = HeyGenAdapterError(code="network_timeout", retryable=True, submission_certainty="maybe_sent")
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
        now_iso=NOW, outcome=err,
    )
    assert outcome.status == "reconciliation_required"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "reconciliation_required"
    assert op["last_error_code"] == "network_timeout"
    assert op["attempt_started_at"] is not None  # kept (maybe-sent)


@pytest.mark.parametrize("code,retryable", [
    ("rate_limited", True), ("validation_error", True),
])
def test_not_sent_retryable_resets_to_claimable(tmp_path: Path, code: str, retryable: bool):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    err = HeyGenAdapterError(code=code, retryable=retryable, submission_certainty="not_sent")
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
        now_iso=NOW, outcome=err,
    )
    assert outcome.status == "submit_pending"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submit_pending"
    assert op["lease_owner"] is None
    assert op["attempt_started_at"] is None   # reset → re-claimable
    assert op["next_retry_at"] is not None
    assert op["last_error_code"] == code
    # And it really is re-claimable now.
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        again = repo.claim_submit_in_tx(conn, prepared.operation_id, OWNER, NOW, 120)
    assert again.status == "claimed"
    assert again.submit_attempts == 2          # counted both attempts
    assert again.fence == claim.fence + 1


def test_not_sent_permanent_fails(tmp_path: Path):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    err = HeyGenAdapterError(code="auth_failed", retryable=False, submission_certainty="not_sent")
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
        now_iso=NOW, outcome=err,
    )
    assert outcome.status == "failed"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "failed"
    assert op["last_error_code"] == "auth_failed"
    assert op["completed_at"] is not None


def test_fence_mismatch_writes_nothing(tmp_path: Path):
    prepared, claim = _claim(tmp_path)
    proc = SubmitProcessor(tmp_path)
    outcome = proc.record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence + 99,
        now_iso=NOW, outcome=SubmitAccepted(remote_id="hg_vid_x"),
    )
    assert outcome.status == "fence_conflict"
    assert outcome.fence == claim.fence
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submit_pending"    # unchanged
    assert op["lease_owner"] == OWNER          # lease still held
    assert _video_resource(tmp_path, prepared.operation_id) is None


def test_accepted_but_local_write_fails_leaves_no_submit(tmp_path: Path):
    """Fault injection: HeyGen accepted, but the local resource INSERT aborts.
    The whole tx rolls back — the operation is NOT marked submitted (the
    accepted result is effectively lost, and the lease will expire into
    ambiguous/reconciliation rather than a false 'submitted')."""
    prepared, claim = _claim(tmp_path)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("CREATE TRIGGER stop_resource_insert BEFORE INSERT ON heygen_remote_resources "
               "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    db.commit()
    db.close()
    proc = SubmitProcessor(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        proc.record_submit_outcome(
            operation_id=prepared.operation_id, lease_owner=OWNER, fence=claim.fence,
            now_iso=NOW, outcome=SubmitAccepted(remote_id="hg_vid_crash"),
        )
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submit_pending"    # rolled back, not submitted
    assert op["lease_owner"] == OWNER          # claim still held
    assert op["attempt_started_at"] is not None
    assert _video_resource(tmp_path, prepared.operation_id) is None


def test_adapter_error_rejects_unknown_code_and_certainty():
    with pytest.raises(ValueError):
        HeyGenAdapterError(code="bogus", retryable=True, submission_certainty="not_sent")
    with pytest.raises(ValueError):
        HeyGenAdapterError(code="rate_limited", retryable=True, submission_certainty="definitely")
