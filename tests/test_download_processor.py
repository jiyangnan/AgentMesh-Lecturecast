"""DownloadProcessor two-phase download + URL re-poll mapping (§5.5e4a2)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lecturecast.consent import ConsentService
from lecturecast.heygen_adapter import PollAdapterError, PollResult
from lecturecast.operation_repository import (
    DownloadProcessor, MediaProbeResult, OperationRepository, PreparedDownload,
    SubmitProcessor,
)
import sqlite3
from lecturecast.heygen_adapter import SubmitAccepted

NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-download-w1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"

def _op(project, op_id):
    db = sqlite3.connect(str(project / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT status, download_status, download_attempts, lease_owner, lease_fence "
                     "FROM heygen_operations WHERE operation_id=?", (op_id,)).fetchone()
    db.close(); return row

# reuse _completed from test_download_claim via importlib (inline copy)
import hashlib as _h
def _Z(seed): return "sha256:" + _h.sha256(str(seed).encode()).hexdigest()

def _completed(tmp_path, *, remote_id="hg_v1", receipt_status="granted"):
    from lecturecast.consent import (CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
        CANONICAL_PROVIDER_COST_DISCLOSURE, ConsentService, DisclosedAsset,
        HeyGenOperationIdentity, ThirdPartyTransferDisclosure, prepare_operation)
    from lecturecast.operation_repository import OperationRepository, SubmitProcessor
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": _Z(1), "manifest_digest": _Z(2), "orch_digest": _Z(3), "request_digest": _Z(4)}
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id="gen_1", manifest_digest=dig["manifest_digest"],
        request_digest=dig["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=dig["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared,
        disclosure=ThirdPartyTransferDisclosure(
            provider="heygen", operation_kind="video",
            disclosure_version="heygen-transfer-2026-07-27",
            disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", "sha256:"+"a"*64)],
            data_categories=["portrait_image", "facial_biometric_template"],
            provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
            agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE),
        decision="granted", creative_brief_digest=dig["brief_digest"], decision_at=NOW)
    with repo.begin_immediate() as conn:
        claim = repo.claim_submit_in_tx(conn, prepared.operation_id, "maintenance-submit-w1", NOW, 120)
    SubmitProcessor(tmp_path).record_submit_outcome(
        operation_id=prepared.operation_id, lease_owner="maintenance-submit-w1",
        fence=claim.fence, now_iso=NOW,
        outcome=SubmitAccepted(remote_id=remote_id, provider_status="processing"))
    from lecturecast.operation_repository import PollProcessor
    PollProcessor(tmp_path).poll_once(
        operation_id=prepared.operation_id, lease_owner="maintenance-poll-w1",
        adapter=type("A", (), {"poll_video": lambda self, rid: PollResult(provider_status="completed", video_url="https://x/v.mp4"),
                               "submit_video": lambda *a: None, "query_videos_by_title": lambda *a: None})(),
        now_iso="2026-07-29T00:00:31Z", lease_seconds=60)
    return prepared


class _StubAdapter:
    def __init__(self, poll_result): self._poll = poll_result
    def poll_video(self, rid): 
        if isinstance(self._poll, Exception): raise self._poll
        return self._poll
    def submit_video(self, c): ...
    def query_videos_by_title(self, q): ...


class _StubDownloader:
    def __init__(self, content=b"fake video bytes"): self._content = content
    def download_and_verify(self, url, runtime_dir, local_ref, max_bytes, probe):
        tmp = Path(runtime_dir) / (local_ref + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(self._content)
        digest = "sha256:" + hashlib.sha256(self._content).hexdigest()
        return PreparedDownload(
            temp_path_str=str(tmp), local_output_ref=local_ref,
            digest=digest, size_bytes=len(self._content),
            media=MediaProbeResult(duration_seconds=10.0, video_codec="h264",
                                   width=1280, height=720))


def test_download_verified_two_phase(tmp_path):
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollResult(provider_status="completed", video_url="https://x/v.mp4")),
        downloader=_StubDownloader(), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "verified"
    op = _op(tmp_path, prepared_op.operation_id)
    # op row doesn't have all download fields — fetch full row
    db = sqlite3.connect(str(tmp_path / DB_REL)); db.row_factory = sqlite3.Row
    row = db.execute("SELECT download_status, local_output_ref, local_output_digest, "
                     "download_verified_at, lease_owner FROM heygen_operations WHERE operation_id=?",
                     (prepared_op.operation_id,)).fetchone()
    db.close()
    assert row["download_status"] == "verified"
    assert row["local_output_ref"] is not None
    assert row["local_output_digest"].startswith("sha256:")
    assert row["lease_owner"] is None
    # final file published
    final = tmp_path / ".lecturecast" / "runtime" / row["local_output_ref"]
    assert final.exists()


def test_download_poll_processing_backoff(tmp_path):
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollResult(provider_status="processing")),
        downloader=_StubDownloader(), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error_code == "provider_output_not_ready"
    assert res.outcome.next_retry_at is not None


def test_download_poll_retryable_error_backoff(tmp_path):
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollAdapterError(code="connection_error", retryable=True)),
        downloader=_StubDownloader(), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error_code == "connection_error"


def test_download_poll_permanent_error_parks(tmp_path):
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollAdapterError(code="auth_failed", retryable=False)),
        downloader=_StubDownloader(), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error_code == "download_reconciliation_required"
    assert res.outcome.next_retry_at is None


def test_download_failure_backoff(tmp_path):
    """Downloader raises → download_failed + backoff."""
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    class _BadDownloader:
        def download_and_verify(self, *a): raise RuntimeError("network down")
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollResult(provider_status="completed", video_url="https://x/v.mp4")),
        downloader=_BadDownloader(), now_iso=NOW, lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error_code == "download_failed"


def test_download_crash_recovery_finalize(tmp_path):
    """Simulate crash after stage (downloaded) but before finalize: a new
    download_once claim returns 'finalize' and publishes from temp."""
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    # First call: download + stage → but simulate crash before finalize by
    # only staging (not finalizing). We'll manually stage.
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared_op.operation_id, OWNER, NOW, 60)
    # write temp file manually
    ref = f"outputs/heygen/{prepared_op.operation_id}.mp4"
    tmp_file = tmp_path / ".lecturecast" / "runtime" / (ref + ".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    content = b"recovered video"
    tmp_file.write_bytes(content)
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    prepared = PreparedDownload(temp_path_str=str(tmp_file), local_output_ref=ref,
                                digest=digest, size_bytes=len(content),
                                media=MediaProbeResult(10.0, "h264", 1280, 720))
    with repo.begin_immediate() as conn:
        repo.stage_download_in_tx(conn, prepared_op.operation_id, OWNER, claim.fence, NOW, prepared)
    # Now simulate crash: lease expired. Recovery call → finalize mode.
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner="maintenance-download-recovery",
        adapter=_StubAdapter(PollResult(provider_status="completed", video_url="https://x/v.mp4")),
        downloader=_StubDownloader(), now_iso="2026-07-29T00:05:00Z", lease_seconds=60)
    assert res.claim.status == "finalize"
    assert res.outcome.status == "verified"
    final = tmp_path / ".lecturecast" / "runtime" / ref
    assert final.exists()
    assert final.read_bytes() == content


# ---- e4a2 Codex fix tests ----

def test_permanent_error_not_reclaimed_second_round(tmp_path):
    """A download parked with a manual-recovery code must NOT be re-claimable."""
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    # First call: non-retryable poll error → parks with download_reconciliation_required.
    proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_StubAdapter(PollAdapterError(code="auth_failed", retryable=False)),
        downloader=_StubDownloader(), now_iso=NOW, lease_seconds=60)
    # Second call: must NOT reach the adapter (claim returns not_ready).
    called = []
    class _TrackingAdapter(_StubAdapter):
        def poll_video(self, rid): called.append(rid); return PollResult("completed", "https://x/v.mp4")
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner=OWNER,
        adapter=_TrackingAdapter(PollAdapterError(code="auth_failed", retryable=False)),
        downloader=_StubDownloader(), now_iso="2026-07-29T00:10:00Z", lease_seconds=60)
    assert res.claim.status == "not_ready"
    assert called == []  # adapter never called


def test_stage_rejects_mismatched_ref(tmp_path):
    """A downloader returning a wrong local_output_ref is rejected at stage."""
    from lecturecast.operation_repository import OperationRepository, OperationIntegrityError
    prepared_op = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared_op.operation_id, OWNER, NOW, 60)
    # Write a real temp file with correct content/digest but WRONG ref.
    content = b"x"
    tmp_file = tmp_path / ".lecturecast" / "runtime" / "outputs/heygen/wrong.mp4.tmp"
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(content)
    bad = PreparedDownload(
        temp_path_str=str(tmp_file), local_output_ref="outputs/heygen/wrong.mp4",
        digest="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=1, media=MediaProbeResult(10.0, "h264", 1280, 720))
    with repo.begin_immediate() as conn:
        with pytest.raises(OperationIntegrityError, match="local_output_ref"):
            repo.stage_download_in_tx(conn, prepared_op.operation_id, OWNER, claim.fence, NOW, bad)


def test_stage_rejects_digest_mismatch(tmp_path):
    """A downloader returning a digest that doesn't match the file is rejected."""
    from lecturecast.operation_repository import OperationRepository, OperationIntegrityError
    prepared_op = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared_op.operation_id, OWNER, NOW, 60)
    ref = f"outputs/heygen/{prepared_op.operation_id}.mp4"
    tmp_file = tmp_path / ".lecturecast" / "runtime" / (ref + ".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(b"real content")
    lying = PreparedDownload(
        temp_path_str=str(tmp_file), local_output_ref=ref,
        digest="sha256:" + "0" * 64,  # wrong digest
        size_bytes=len(b"real content"), media=MediaProbeResult(10.0, "h264", 1280, 720))
    with repo.begin_immediate() as conn:
        with pytest.raises(OperationIntegrityError, match="digest"):
            repo.stage_download_in_tx(conn, prepared_op.operation_id, OWNER, claim.fence, NOW, lying)


def test_finalize_missing_file_parks_for_manual_recovery(tmp_path):
    """Neither temp nor final exists → fenced manual-recovery park (not a loop)."""
    prepared_op = _completed(tmp_path)
    proc = DownloadProcessor(tmp_path)
    repo = OperationRepository(tmp_path)
    # Manually stage without writing any file.
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared_op.operation_id, OWNER, NOW, 60)
    ref = f"outputs/heygen/{prepared_op.operation_id}.mp4"
    content = b"staged"
    tmp_file = tmp_path / ".lecturecast" / "runtime" / (ref + ".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(content)
    prepared = PreparedDownload(temp_path_str=str(tmp_file), local_output_ref=ref,
        digest="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=len(content), media=MediaProbeResult(10.0, "h264", 1280, 720))
    with repo.begin_immediate() as conn:
        repo.stage_download_in_tx(conn, prepared_op.operation_id, OWNER, claim.fence, NOW, prepared)
    # Delete the temp file so finalize finds nothing.
    tmp_file.unlink()
    res = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner="maintenance-download-recovery",
        adapter=_StubAdapter(PollResult(provider_status="completed", video_url="https://x/v.mp4")),
        downloader=_StubDownloader(), now_iso="2026-07-29T00:05:00Z", lease_seconds=60)
    assert res.outcome.status == "failed"
    assert res.outcome.last_error_code == "download_file_missing"
    # Parked → not re-claimable.
    res2 = proc.download_once(
        operation_id=prepared_op.operation_id, lease_owner="maintenance-download-recovery2",
        adapter=_StubAdapter(PollResult(provider_status="completed", video_url="https://x/v.mp4")),
        downloader=_StubDownloader(), now_iso="2026-07-29T00:10:00Z", lease_seconds=60)
    assert res2.claim.status == "not_ready"


def test_stale_fence_does_not_publish(tmp_path):
    """A fence mismatch in finalize writes nothing and does not publish a file."""
    prepared_op = _completed(tmp_path)
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_download_in_tx(conn, prepared_op.operation_id, OWNER, NOW, 60)
    ref = f"outputs/heygen/{prepared_op.operation_id}.mp4"
    content = b"staged"
    tmp_file = tmp_path / ".lecturecast" / "runtime" / (ref + ".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(content)
    prepared = PreparedDownload(temp_path_str=str(tmp_file), local_output_ref=ref,
        digest="sha256:" + hashlib.sha256(content).hexdigest(),
        size_bytes=len(content), media=MediaProbeResult(10.0, "h264", 1280, 720))
    with repo.begin_immediate() as conn:
        repo.stage_download_in_tx(conn, prepared_op.operation_id, OWNER, claim.fence, NOW, prepared)
    # Finalize with WRONG fence.
    with repo.begin_immediate() as conn:
        outcome = repo.finalize_download_in_tx(
            conn, prepared_op.operation_id, OWNER, claim.fence + 99, NOW,
            str(tmp_path))
    assert outcome.status == "fence_conflict"
    final = tmp_path / ".lecturecast" / "runtime" / ref
    assert not final.exists()  # not published
