"""Download claim/lease/fence + guards (§5.5e4a1)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE, CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService, DisclosedAsset, HeyGenOperationIdentity, ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.heygen_adapter import SubmitAccepted
from lecturecast.operation_repository import (
    OperationIntegrityError, OperationRepository, OperationStateError, SubmitProcessor,
)

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-download-w1"
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
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE)


def _completed(tmp_path: Path, *, remote_id="hg_v1", receipt_status="granted"):
    """Grant → submit-accept → poll-complete so the operation is 'completed' with
    one exclusive video resource."""
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
        claim = repo.claim_submit_in_tx(conn, prepared.operation_id, "maintenance-submit-w1", NOW, 120)
    SubmitProcessor(tmp_path).record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner="maintenance-submit-w1",
        fence=claim.fence, now_iso=NOW,
        outcome=SubmitAccepted(remote_id=remote_id, provider_status="processing"))
    # poll → completed
    from lecturecast.operation_repository import PollProcessor
    from lecturecast.heygen_adapter import PollResult
    PollProcessor(tmp_path).poll_once(
        operation_id=prepared.operation_id, lease_owner="maintenance-poll-w1",
        adapter=type("A", (), {"poll_video": lambda self, rid: PollResult(provider_status="completed", video_url="https://x/v.mp4"),
                               "submit_video": lambda *a: None, "query_videos_by_title": lambda *a: None})(),
        now_iso="2026-07-29T00:00:31Z", lease_seconds=60)
    if receipt_status == "withdrawn":
        db = sqlite3.connect(str(tmp_path / DB_REL))
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("UPDATE heygen_consent_receipts SET status='withdrawn', withdrawn_at=? WHERE operation_id=?",
                   (NOW, prepared.operation_id))
        db.execute("UPDATE heygen_operations SET consent_receipt_digest=NULL WHERE operation_id=?",
                   (prepared.operation_id,))
        db.commit(); db.close()
    return prepared


def _op(project, op_id):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT status, download_status, download_attempts, lease_owner, lease_fence "
                     "FROM heygen_operations WHERE operation_id=?", (op_id,)).fetchone()
    db.close(); return row


def _resource(project, op_id):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT deletion_status, deletion_reason FROM heygen_remote_resources r "
                     "JOIN heygen_resource_operation_refs ref ON ref.resource_id=r.resource_id "
                     "WHERE ref.operation_id=?", (op_id,)).fetchone()
    db.close(); return row


def test_download_claim_on_completed(tmp_path: Path):
    prepared = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "claimed"
    assert claim.fence >= 2
    assert claim.remote_id == "hg_v1"
    op = _op(tmp_path, prepared.operation_id)
    assert op["download_status"] == "downloading"
    assert op["download_attempts"] == 1


def test_download_claim_not_ready_for_non_completed(tmp_path: Path):
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
        claim = repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "not_ready"  # submit_pending


def test_download_claim_busy_while_lease_held(tmp_path: Path):
    prepared = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        first = repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
        second = repo.claim_download_in_tx(conn, prepared.operation_id, "maintenance-download-other", NOW, 60)
    assert first.status == "claimed"
    assert second.status == "busy"


def test_download_claim_reclaims_after_lease_expiry(tmp_path: Path):
    """Crash recovery: a worker claimed download (downloading) then crashed; the
    expired lease is reclaimed by another worker (download_attempts already 1)."""
    prepared = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        repo.claim_download_in_tx(conn, prepared.operation_id, "maintenance-download-dead", NOW, 60)
    # lease expired (NOW is past 00:01:00 expiry); a new worker reclaims
    with repo.begin_immediate() as conn:
        reclaim = repo.claim_download_in_tx(conn, prepared.operation_id, OWNER,
                                            "2026-07-29T00:02:00Z", 60)
    assert reclaim.status == "claimed"
    op = _op(tmp_path, prepared.operation_id)
    assert op["download_attempts"] == 2  # counted both


def test_download_claim_consent_withdrawn_flips_resource_to_cleanup(tmp_path: Path):
    """User withdrew after completion, before download → download refused, resource
    flipped to deletion_pending (consent_withdrawal) for the cleanup path."""
    prepared = _completed(tmp_path, receipt_status="withdrawn")
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "consent_withdrawn"
    res = _resource(tmp_path, prepared.operation_id)
    assert res["deletion_status"] == "deletion_pending"
    assert res["deletion_reason"] == "consent_withdrawal"


def test_download_claim_fail_closed_on_half_lease(tmp_path: Path):
    prepared = _completed(tmp_path)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET lease_owner=?, lease_expires_at=NULL WHERE operation_id=?",
               (OWNER, prepared.operation_id))
    db.commit(); db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        with pytest.raises(OperationIntegrityError):
            repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)


def test_download_claim_fail_closed_on_resource_owned_by_another(tmp_path: Path):
    prepared = _completed(tmp_path)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_remote_resources SET created_by_operation_id='lc_hg_other' "
               "WHERE resource_kind='video'")
    db.commit(); db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        with pytest.raises(OperationIntegrityError):
            repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)


def test_download_claim_requires_active_transaction(tmp_path: Path):
    from lecturecast.heygen_journal import init_database
    prepared = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    conn = init_database(tmp_path)
    with pytest.raises(OperationStateError):
        repo.claim_download_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    conn.close()
