"""PollProcessor known-id poll + outcome mapping (§5.5e3c)."""

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
from lecturecast.heygen_adapter import HeyGenAdapterError, PollResult, SubmitAccepted
from lecturecast.operation_repository import (
    OperationRepository,
    PollProcessor,
    SubmitProcessor,
)

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-poll-worker-1"
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


def _submitted(tmp_path: Path, *, remote_id="hg_vid_1"):
    """Grant + claim + submit-accept an operation so it is in 'submitted' with a
    known remote video id."""
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
        claim = repo.claim_submit_in_tx(conn, prepared.operation_id,
                                        "maintenance-submit-w1", NOW, 120)
    SubmitProcessor(tmp_path).record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner="maintenance-submit-w1",
        fence=claim.fence, now_iso=NOW,
        outcome=SubmitAccepted(remote_id=remote_id, provider_status="processing"))
    return prepared


def _op(project: Path, op_id: str) -> sqlite3.Row:
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT status, lease_owner, lease_fence, next_retry_at, last_error_code, "
        "completed_at, provider_status FROM heygen_operations WHERE operation_id = ?",
        (op_id,),
    ).fetchone()
    db.close()
    return row


class _Adapter:
    def __init__(self, result_or_error):
        self._result = result_or_error
        self.polled = []

    def poll_video(self, remote_id):
        self.polled.append(remote_id)
        if isinstance(self._result, HeyGenAdapterError):
            raise self._result
        return self._result

    def submit_video(self, command): ...   # noqa
    def query_videos_by_title(self, query): ...  # noqa


# ---- claim ------------------------------------------------------------

def test_poll_claim_on_submitted_returns_remote_id(tmp_path: Path):
    prepared = _submitted(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_poll_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "claimed"
    assert claim.remote_id == "hg_vid_1"
    assert claim.fence >= 2


def test_poll_claim_not_ready_for_non_pollable(tmp_path: Path):
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
        claim = repo.claim_poll_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "not_ready"  # still submit_pending


def test_poll_claim_busy_while_lease_held(tmp_path: Path):
    prepared = _submitted(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        first = repo.claim_poll_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
        second = repo.claim_poll_in_tx(conn, prepared.operation_id, "maintenance-poll-other", NOW, 60)
    assert first.status == "claimed"
    assert second.status == "busy"  # anti-hotloop


def test_poll_claim_retry_wait_during_backoff(tmp_path: Path):
    prepared = _submitted(tmp_path)
    repo = OperationRepository(tmp_path)
    proc = PollProcessor(tmp_path)
    # A transient poll error sets next_retry_at.
    proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                   adapter=_Adapter(HeyGenAdapterError(code="connection_error", retryable=True,
                                                       submission_certainty="not_sent")),
                   now_iso=NOW, lease_seconds=60)
    # Re-poll immediately → retry_wait (backoff not elapsed).
    res = proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                         adapter=_Adapter(PollResult(provider_status="processing")),
                         now_iso=NOW, lease_seconds=60)
    assert res.claim.status == "retry_wait"


# ---- outcome mapping --------------------------------------------------

@pytest.mark.parametrize("provider_status,expected", [
    ("queued", "submitted"),
    ("submitted", "submitted"),
    ("processing", "processing"),
    ("completed", "completed"),
    ("failed", "failed"),
    ("not_found", "reconciliation_required"),
    ("bogus", "reconciliation_required"),
])
def test_poll_outcome_mapping(tmp_path: Path, provider_status: str, expected: str):
    prepared = _submitted(tmp_path)
    proc = PollProcessor(tmp_path)
    res = proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                         adapter=_Adapter(PollResult(provider_status=provider_status)),
                         now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == expected
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == expected
    assert op["lease_owner"] is None  # poll lease cleared


def test_poll_transient_error_keeps_status_and_sets_retry(tmp_path: Path):
    prepared = _submitted(tmp_path)
    proc = PollProcessor(tmp_path)
    res = proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                         adapter=_Adapter(HeyGenAdapterError(code="connection_error", retryable=True,
                                                             submission_certainty="not_sent")),
                         now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "keep"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submitted"   # unchanged
    assert op["next_retry_at"] is not None
    assert op["last_error_code"] == "connection_error"


def test_poll_maybe_sent_error_is_reconciliation(tmp_path: Path):
    prepared = _submitted(tmp_path)
    proc = PollProcessor(tmp_path)
    res = proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                         adapter=_Adapter(HeyGenAdapterError(code="network_timeout", retryable=True,
                                                             submission_certainty="maybe_sent")),
                         now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "reconciliation_required"


def test_poll_fence_conflict_writes_nothing(tmp_path: Path):
    prepared = _submitted(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_poll_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    # Apply with a wrong fence.
    with repo.begin_immediate() as conn:
        outcome = repo.apply_poll_outcome_in_tx(
            conn, prepared.operation_id, OWNER, claim.fence + 99, NOW,
            PollResult(provider_status="completed"),
        )
    assert outcome.status == "fence_conflict"
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "submitted"   # unchanged
    assert op["lease_owner"] == OWNER    # lease still held


def test_poll_completed_clears_retry_fields(tmp_path: Path):
    prepared = _submitted(tmp_path)
    proc = PollProcessor(tmp_path)
    # First a transient error leaves next_retry_at/last_error_code.
    proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                   adapter=_Adapter(HeyGenAdapterError(code="connection_error", retryable=True,
                                                       submission_certainty="not_sent")),
                   now_iso=NOW, lease_seconds=60)
    # Wait out the backoff, then completed must clear them.
    proc.poll_once(operation_id=prepared.operation_id, lease_owner=OWNER,
                   adapter=_Adapter(PollResult(provider_status="completed")),
                   now_iso="2026-07-29T00:01:00Z", lease_seconds=60)
    op = _op(tmp_path, prepared.operation_id)
    assert op["status"] == "completed"
    assert op["next_retry_at"] is None
    assert op["last_error_code"] is None
