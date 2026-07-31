"""AssetUploadProcessor contract tests (§5.5e5b0c2).

Covers the crash-safe orchestration: guard+claim in one tx, adapter call
outside tx, fenced outcome apply — plus the concurrency/crash contracts
(stale fence, dual worker, crash-after-send terminal promotion).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService, DisclosedAsset, HeyGenOperationIdentity,
    ThirdPartyTransferDisclosure, prepare_operation,
)
from lecturecast.heygen_asset_adapter import (
    prepare_asset_upload, AssetUploadError, AssetUploadAmbiguousError,
    AssetUploadResult,
)
from lecturecast.heygen_adapter import HeyGenAdapterError
from lecturecast.operation_repository import AssetUploadProcessor

LEASE = "worker-1"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png(size=128):
    return PNG_MAGIC + b"\x00" * (size - len(PNG_MAGIC))


def _setup_parent(td, *, asset_role, digest):
    """Create a real parent video op + granted receipt disclosing (role, digest).
    Returns the derived operation_id."""
    svc = ConsentService(Path(td))
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id="gen_1",
        manifest_digest="sha256:" + "1" * 64, request_digest="sha256:" + "2" * 64,
        credential_profile_id="heygen_env_default",
        orchestration_plan_digest="sha256:" + "3" * 64, endpoint="/v3/videos"))
    cats = {"portrait_photo": ["portrait_image", "facial_biometric_template"],
            "synthetic_narration_audio": ["synthetic_narration_audio"]}[asset_role]
    disclosure = ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset(asset_role, f"{asset_role}.bin", digest)],
        data_categories=cats,
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE)
    svc.record_decision(prepared=prepared, disclosure=disclosure, decision="granted",
                        creative_brief_digest="sha256:" + "b" * 64,
                        decision_at="2026-07-29T00:00:00Z")
    return prepared.operation_id


def _prepare(td, *, asset_role="portrait_photo", local_ref="r/portrait.png",
             ext=".png", content=None):
    """Write an asset file, grant a receipt disclosing its real digest, and
    prepare the upload command. Returns (command, op_id)."""
    runtime = Path(td) / "runtime"
    (runtime / "r").mkdir(parents=True, exist_ok=True)
    data = content if content is not None else _png()
    (runtime / local_ref).write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    op_id = _setup_parent(td, asset_role=asset_role, digest=digest)
    command = prepare_asset_upload(
        operation_id=op_id, asset_role=asset_role,
        runtime_root=runtime, local_output_ref=local_ref)
    return command, op_id, runtime


class _FakeAdapter:
    def __init__(self, *, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def upload_asset(self, command, *, runtime_root):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


# === happy path ============================================================

def test_upload_once_success():
    with tempfile.TemporaryDirectory() as td:
        command, _op, runtime = _prepare(td)
        proc = AssetUploadProcessor(td)
        adapter = _FakeAdapter(result=AssetUploadResult(
            asset_id="asset_xyz", remote_url="https://x/y", mime_type="image/png",
            size_bytes=128))
        r = proc.upload_once(command=command, adapter=adapter, runtime_root=runtime,
                             lease_owner=LEASE, now_iso="2026-07-30T00:00:00Z",
                             lease_seconds=60)
        assert r.status == "uploaded"
        assert r.resource_id is not None
        assert adapter.calls == 1


def test_upload_once_ambiguous_routes_to_reconciliation():
    with tempfile.TemporaryDirectory() as td:
        command, _op, runtime = _prepare(td)
        proc = AssetUploadProcessor(td)
        adapter = _FakeAdapter(error=AssetUploadAmbiguousError(
            code="network_timeout", message="lost response"))
        r = proc.upload_once(command=command, adapter=adapter, runtime_root=runtime,
                             lease_owner=LEASE, now_iso="2026-07-30T00:00:00Z",
                             lease_seconds=60)
        assert r.status == "reconciliation_required"
        assert r.error_code == "network_timeout"


def test_upload_once_not_sent_permanent_is_failed():
    with tempfile.TemporaryDirectory() as td:
        command, _op, runtime = _prepare(td)
        proc = AssetUploadProcessor(td)
        adapter = _FakeAdapter(error=AssetUploadError(
            code="validation_error", message="bad"))
        r = proc.upload_once(command=command, adapter=adapter, runtime_root=runtime,
                             lease_owner=LEASE, now_iso="2026-07-30T00:00:00Z",
                             lease_seconds=60)
        assert r.status == "failed"


def test_upload_once_idempotent_on_replay():
    # A second upload_once for an already-uploaded row → terminal (done), no
    # second provider call.
    with tempfile.TemporaryDirectory() as td:
        command, _op, runtime = _prepare(td)
        proc = AssetUploadProcessor(td)
        adapter = _FakeAdapter(result=AssetUploadResult(
            asset_id="asset_xyz", remote_url="u", mime_type="image/png", size_bytes=128))
        first = proc.upload_once(command=command, adapter=adapter, runtime_root=runtime,
                                 lease_owner=LEASE, now_iso="2026-07-30T00:00:00Z",
                                 lease_seconds=60)
        assert first.status == "uploaded"
        second = proc.upload_once(command=command, adapter=adapter, runtime_root=runtime,
                                  lease_owner=LEASE, now_iso="2026-07-30T00:01:00Z",
                                  lease_seconds=60)
        assert second.status == "terminal"
        assert adapter.calls == 1  # not re-sent


# === concurrency / crash contracts ========================================

def test_stale_fence_apply_is_rejected():
    # Worker 1 claims; worker 2 reclaims after the lease expires (fence bumps);
    # worker 1's outcome apply with the stale fence must fail (no overwrite).
    with tempfile.TemporaryDirectory() as td:
        command, _op, runtime = _prepare(td)
        proc = AssetUploadProcessor(td)
        claim = proc.claim_for_upload(
            command=command, lease_owner=LEASE, now_iso="2026-07-30T00:00:00Z",
            lease_seconds=60)
        # worker 2 reclaims 2 minutes later (lease expired) → fence bumps to 2.
        proc.claim_for_upload(
            command=command, lease_owner="worker-2",
            now_iso="2026-07-30T00:02:00Z", lease_seconds=60)
        from lecturecast.operation_repository import OperationIntegrityError
        with pytest.raises(OperationIntegrityError):
            proc.apply_outcome(
                claim=claim,
                asset_result=AssetUploadResult(
                    asset_id="ax", remote_url="u", mime_type="image/png",
                    size_bytes=128),
                lease_owner=LEASE, now_iso="2026-07-30T00:02:30Z")


def test_claim_for_upload_runs_guard_and_claim_atomically():
    # If consent is missing, claim_for_upload must NOT leave a claimed row.
    with tempfile.TemporaryDirectory() as td:
        runtime = Path(td) / "runtime"
        (runtime / "r").mkdir(parents=True)
        (runtime / "r" / "portrait.png").write_bytes(_png())
        # parent op WITHOUT a receipt → guard fails
        from lecturecast.operation_repository import OperationRepository
        from lecturecast.heygen_journal import init_database
        conn = init_database(Path(td))
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, "
            "generation_id, manifest_digest, request_digest, idempotency_key, "
            "heygen_title, credential_profile_id, created_at, updated_at) "
            "VALUES ('op_norc','video','/v3/videos','g','sha256:m','sha256:r',"
            "'i','lc:op_norc','heygen_env_default','t','t')")
        conn.commit(); conn.close()
        digest = "sha256:" + hashlib.sha256(_png()).hexdigest()
        command = prepare_asset_upload(
            operation_id="op_norc", asset_role="portrait_photo",
            runtime_root=runtime, local_output_ref="r/portrait.png")
        proc = AssetUploadProcessor(td)
        from lecturecast.consent import ConsentStateError
        with pytest.raises(ConsentStateError):
            proc.claim_for_upload(command=command, lease_owner=LEASE,
                                  now_iso="2026-07-30T00:00:00Z", lease_seconds=60)
        # No asset upload row was written.
        conn = init_database(Path(td))
        n = conn.execute("SELECT count(*) FROM heygen_asset_uploads").fetchone()[0]
        conn.close()
        assert n == 0
