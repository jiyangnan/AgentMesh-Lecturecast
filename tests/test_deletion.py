"""DeleteProcessor per-resource deletion lifecycle (§5.5e4b)."""

from __future__ import annotations
import hashlib, sqlite3
from pathlib import Path
import pytest

from lecturecast.consent import (CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE, ConsentService, DisclosedAsset,
    HeyGenOperationIdentity, ThirdPartyTransferDisclosure, prepare_operation)
from lecturecast.heygen_adapter import (DeleteAdapterError, DeleteResult, PollResult,
    SubmitAccepted)
from lecturecast.operation_repository import (DeleteProcessor, DownloadProcessor,
    MediaProbeResult, OperationRepository, OperationIntegrityError, PreparedDownload,
    SubmitProcessor)

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-delete-w1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"
def Z(s): return "sha256:" + hashlib.sha256(str(s).encode()).hexdigest()


def _verified(tmp_path, *, retention="ephemeral"):
    """Full pipeline: grant → submit → poll completed → download verified."""
    svc = ConsentService(tmp_path); repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id="gen_1", manifest_digest=dig["manifest_digest"],
        request_digest=dig["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=dig["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared,
        disclosure=ThirdPartyTransferDisclosure(
            provider="heygen", operation_kind="video", disclosure_version="heygen-transfer-2026-07-27",
            disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
            data_categories=["portrait_image", "facial_biometric_template"],
            provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
            agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE),
        decision="granted", creative_brief_digest=dig["brief_digest"], decision_at=NOW)
    with repo.begin_immediate() as conn:
        claim = repo.claim_submit_in_tx(conn, prepared.operation_id, "maintenance-submit-w1", NOW, 120)
    SubmitProcessor(tmp_path).record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner="maintenance-submit-w1",
        fence=claim.fence, now_iso=NOW, outcome=SubmitAccepted(remote_id="hg_v1", provider_status="processing"))
    PollStub = type("A", (), {"poll_video": lambda s, r: PollResult("completed", "https://x/v.mp4"),
                              "submit_video": lambda *a: None, "query_videos_by_title": lambda *a: None})
    from lecturecast.operation_repository import PollProcessor
    PollProcessor(tmp_path).poll_once(operation_id=prepared.operation_id, lease_owner="maintenance-poll-w1",
        adapter=PollStub(), now_iso="2026-07-29T00:00:31Z", lease_seconds=60)
    # download + verify
    class _DL:
        def download_and_verify(self, url, runtime_dir, local_ref, max_bytes, probe):
            content = b"video"; tmp = Path(runtime_dir) / (local_ref + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True); tmp.write_bytes(content)
            return PreparedDownload(temp_path_str=str(tmp), local_output_ref=local_ref,
                digest="sha256:" + hashlib.sha256(content).hexdigest(),
                size_bytes=len(content), media=MediaProbeResult(10.0, "h264", 1280, 720))
    DownloadProcessor(tmp_path).download_once(
        operation_id=prepared.operation_id, lease_owner="maintenance-download-w1", adapter=PollStub(),
        downloader=_DL(), now_iso="2026-07-29T00:01:00Z", lease_seconds=60)
    if retention == "reusable_avatar":
        db = sqlite3.connect(str(tmp_path / DB_REL))
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("UPDATE heygen_remote_resources SET retention_mode='reusable_avatar' WHERE resource_kind='video'")
        db.commit(); db.close()
    # get resource_id
    db = sqlite3.connect(str(tmp_path / DB_REL)); db.row_factory = sqlite3.Row
    rid = db.execute("SELECT resource_id FROM heygen_remote_resources WHERE resource_kind='video'").fetchone()["resource_id"]
    db.close()
    return prepared, rid


class _StubDeleter:
    def __init__(self, result): self._r = result
    def delete_video(self, rid):
        if isinstance(self._r, Exception): raise self._r
        return self._r


def test_normal_deletion_verified_ephemeral(tmp_path):
    prepared, rid = _verified(tmp_path)
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteResult("deleted")), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "deleted"
    db = sqlite3.connect(str(tmp_path / DB_REL)); db.row_factory = sqlite3.Row
    row = db.execute("SELECT deletion_status, deleted_at FROM heygen_remote_resources WHERE resource_id=?", (rid,)).fetchone()
    db.close()
    assert row["deletion_status"] == "deleted"
    assert row["deleted_at"] is not None


def test_reusable_avatar_not_auto_deletable(tmp_path):
    prepared, rid = _verified(tmp_path, retention="reusable_avatar")
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteResult("deleted")), now_iso=NOW, lease_seconds=60)
    assert res.claim.status == "not_ready"


def test_already_absent_is_idempotent_deleted(tmp_path):
    prepared, rid = _verified(tmp_path)
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteResult("already_absent")), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "deleted"


def test_retryable_error_backoff(tmp_path):
    prepared, rid = _verified(tmp_path)
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteAdapterError(code="connection_error", retryable=True)),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error == "connection_error"
    assert res.outcome.next_retry_at is not None


def test_max_attempts_exhausted_parks(tmp_path):
    prepared, rid = _verified(tmp_path)
    proc = DeleteProcessor(tmp_path)
    times = [NOW, "2026-07-29T00:03:00Z", "2026-07-29T00:06:00Z"]  # advance past backoff each retry
    res = None
    for t in times:
        res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
            lease_owner=OWNER, deleter=_StubDeleter(DeleteAdapterError(code="connection_error", retryable=True)),
            now_iso=t, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error == "deletion_retry_exhausted"
    assert res.outcome.next_retry_at is None
    # Parked → not re-claimable
    res2 = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteResult("deleted")),
        now_iso="2026-07-29T01:00:00Z", lease_seconds=60)
    assert res2.claim.status == "not_ready"


def test_permanent_error_parks(tmp_path):
    prepared, rid = _verified(tmp_path)
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteAdapterError(code="auth_failed", retryable=False)),
        now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error == "deletion_reconciliation_required"


def test_consent_cleanup_deletes_regardless_of_retention(tmp_path):
    """A withdrawal cleanup resource (deletion_pending + consent_withdrawal) is
    deletable regardless of retention_mode or download status."""
    prepared, rid = _verified(tmp_path, retention="reusable_avatar")
    # manually set to deletion_pending + consent_withdrawal
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_remote_resources SET deletion_status='deletion_pending', "
               "deletion_reason='consent_withdrawal' WHERE resource_id=?", (rid,))
    db.commit(); db.close()
    proc = DeleteProcessor(tmp_path)
    res = proc.delete_once(operation_id=prepared.operation_id, resource_id=rid,
        lease_owner=OWNER, deleter=_StubDeleter(DeleteResult("deleted")), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "deleted"


def test_stale_fence_does_not_delete(tmp_path):
    prepared, rid = _verified(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_deletion_in_tx(conn, prepared.operation_id, rid, OWNER, NOW, 60)
    with repo.begin_immediate() as conn:
        outcome = repo.apply_deletion_outcome_in_tx(
            conn, prepared.operation_id, rid, OWNER, claim.fence + 99, NOW,
            DeleteResult("deleted"), expected_remote_id=claim.remote_id)
    assert outcome.status == "fence_conflict"
    db = sqlite3.connect(str(tmp_path / DB_REL))
    ds = db.execute("SELECT deletion_status FROM heygen_remote_resources WHERE resource_id=?", (rid,)).fetchone()[0]
    db.close()
    assert ds == "deletion_pending"  # not deleted
