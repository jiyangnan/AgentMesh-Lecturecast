"""Asset upload journal + consent guard tests (§5.5e5b0c1).

Covers the asset consent guard (validate_asset_upload_consent_in_tx) and the
three OperationRepository asset primitives (claim/apply_outcome/apply_failure)
on the v5 heygen_asset_uploads table.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from lecturecast.heygen_journal import init_database
from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService, ConsentConflictError, ConsentStateError,
    DisclosedAsset, HeyGenOperationIdentity, ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.operation_repository import OperationRepository


D_PORT = "sha256:" + "a" * 64
D_AUDIO = "sha256:" + "b" * 64
D_OTHER = "sha256:" + "c" * 64
LEASE = "worker-1"
NOW = "2026-07-30T00:00:00Z"

_ASSET_CATEGORIES = {
    "portrait_photo": ("portrait_image", "facial_biometric_template"),
    "synthetic_narration_audio": ("synthetic_narration_audio",),
}


def _db():
    td = tempfile.mkdtemp()
    conn = init_database(Path(td))
    conn.row_factory = sqlite3.Row
    return conn, td


def _add_parent_op(conn, op_id="op1"):
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id,"
        " manifest_digest, request_digest, idempotency_key, heygen_title,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (op_id, "video", "/v3/videos", "gen", "sha256:m", "sha256:r",
         f"idem-{op_id}", f"lc:{op_id}", "t", "t"),
    )


def _grant_parent(td, assets=(("portrait_photo", D_PORT),), decision="granted"):
    """Create a real parent video operation + a receipt with a valid digest via
    record_decision (the canonical path), so the integrity check passes.
    Returns the derived operation_id."""
    svc = ConsentService(Path(td))
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id="gen_1",
        manifest_digest="sha256:" + "1" * 64, request_digest="sha256:" + "2" * 64,
        credential_profile_id="heygen_env_default",
        orchestration_plan_digest="sha256:" + "3" * 64, endpoint="/v3/videos"))
    disclosed = [DisclosedAsset(kind, f"{kind}.bin", dig) for kind, dig in assets]
    cats = sorted({c for kind, _ in assets for c in _ASSET_CATEGORIES[kind]})
    disclosure = ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=disclosed, data_categories=cats,
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE)
    svc.record_decision(prepared=prepared, disclosure=disclosure, decision=decision,
                        creative_brief_digest="sha256:" + "b" * 64,
                        decision_at="2026-07-29T00:00:00Z")
    return prepared.operation_id


# === consent guard =========================================================

class TestAssetConsentGuard:
    def _svc(self, td):
        return ConsentService(Path(td))

    def test_grants_when_disclosed(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            r = self._svc(td).validate_asset_upload_consent_in_tx(
                conn, parent_operation_id=op_id,
                asset_role="portrait_photo", content_digest=D_PORT)
            assert r.asset_role == "portrait_photo"
            assert r.content_digest == D_PORT
            assert r.receipt_digest.startswith("sha256:")
        finally:
            conn.close()

    def test_rejects_digest_not_disclosed(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            with pytest.raises(ConsentConflictError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id=op_id,
                    asset_role="portrait_photo", content_digest=D_OTHER)
        finally:
            conn.close()

    def test_rejects_role_digest_mismatch(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT),
                                              ("synthetic_narration_audio", D_AUDIO)])
            with pytest.raises(ConsentConflictError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id=op_id,
                    asset_role="portrait_photo", content_digest=D_AUDIO)
        finally:
            conn.close()

    def test_rejects_withdrawn_receipt(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            self._svc(td).withdraw(operation_id=op_id)
            with pytest.raises(ConsentStateError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id=op_id,
                    asset_role="portrait_photo", content_digest=D_PORT)
        finally:
            conn.close()

    def test_rejects_no_receipt(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn, "op_bare")  # op with no receipt
            with pytest.raises(ConsentStateError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op_bare",
                    asset_role="portrait_photo", content_digest=D_PORT)
        finally:
            conn.close()

    def test_rejects_tampered_receipt_digest(self):
        # Flip a disclosure byte but leave the stored digest → integrity check
        # must fail closed (blocker #1: not a status-only peek).
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            conn.execute(
                "UPDATE heygen_consent_receipts SET creative_brief_digest=? "
                "WHERE operation_id=?",
                ("sha256:" + "z" * 64, op_id))
            with pytest.raises(Exception):  # ConsentIntegrityError
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id=op_id,
                    asset_role="portrait_photo", content_digest=D_PORT)
        finally:
            conn.close()

    def test_requires_transaction(self):
        conn, td = _db()
        try:
            with pytest.raises(ConsentStateError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op1",
                    asset_role="portrait_photo", content_digest=D_PORT)
        finally:
            conn.close()


# === repository claim / apply / failure ====================================

def _do_claim(repo, conn, upload_id="u1", parent="op1", role="portrait_photo",
              digest=D_PORT, idem="k1", now=NOW, size=1024,
              ctype="image/png", fname="portrait.png", lref="r/p.png"):
    return repo.claim_asset_upload_in_tx(
        conn, upload_id=upload_id, parent_operation_id=parent, asset_role=role,
        content_digest=digest, local_ref=lref, content_type=ctype,
        size_bytes=size, provider_filename=fname, idempotency_key=idem,
        lease_owner=LEASE, now_iso=now, lease_seconds=60)


class TestAssetClaim:
    def test_first_claim_creates_row(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            r = _do_claim(repo, conn)
            assert r.status == "claimed"
            assert r.fence == 1 and r.attempts == 1
            row = conn.execute(
                "SELECT status, lease_fence, attempts FROM heygen_asset_uploads "
                "WHERE upload_id=?", ("u1",)).fetchone()
            assert row["status"] == "uploading"
            assert row["lease_fence"] == 1
        finally:
            conn.close()

    def test_reclaim_after_done_is_idempotent(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id="u1", asset_id="ax",
                retention_mode="ephemeral", credential_profile_id="prof",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            again = _do_claim(repo, conn, now="2026-07-30T00:00:02Z")
            assert again.status == "done"
            assert again.remote_resource_id is not None
        finally:
            conn.close()

    def test_busy_when_lease_active(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")  # lease 60s
            # Another worker within the lease window → busy.
            again = _do_claim(repo, conn, now="2026-07-30T00:00:30Z")
            assert again.status == "busy"
        finally:
            conn.close()

    def test_reclaim_after_lease_expired(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")  # lease 60s
            again = _do_claim(repo, conn, now="2026-07-30T00:02:00Z")  # expired
            assert again.status == "claimed"
            assert again.attempts == 2
        finally:
            conn.close()

    def test_crash_after_send_past_window_is_manual(self):
        # Worker claimed, called the provider, then died before apply_failure.
        # The row sits in 'uploading' with an expired lease and no expires_at.
        # Reclaiming 25h later MUST NOT re-upload (duplicate risk) — promote to
        # manual_reconciliation_required.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")
            reclaim = _do_claim(repo, conn, now="2026-07-31T01:00:00Z")  # 25h
            assert reclaim.status == "terminal"
            st = conn.execute("SELECT status, maybe_sent_at, idempotency_expires_at "
                              "FROM heygen_asset_uploads WHERE upload_id=?",
                              ("u1",)).fetchone()
            assert st["status"] == "manual_reconciliation_required"
            assert st["maybe_sent_at"] is not None
            assert st["idempotency_expires_at"] is not None
        finally:
            conn.close()

    def test_idempotency_key_mismatch_on_replay_raises(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, idem="k1")
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                _do_claim(repo, conn, idem="different-key")
        finally:
            conn.close()


class TestAssetApplyOutcome:
    def test_inserts_resource_and_backfills(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            up = conn.execute("SELECT status, remote_resource_id FROM "
                              "heygen_asset_uploads WHERE upload_id=?",
                              ("u1",)).fetchone()
            assert up["status"] == "uploaded"
            assert up["remote_resource_id"] == rid
            res = conn.execute("SELECT resource_kind, remote_id, "
                               "created_by_operation_id, retention_mode FROM "
                               "heygen_remote_resources WHERE resource_id=?",
                               (rid,)).fetchone()
            assert res["resource_kind"] == "portrait_asset"
            assert res["remote_id"] == "ax"
            assert res["created_by_operation_id"] == "op1"
            assert res["retention_mode"] == "ephemeral"
            # parent-op ref inserted
            ref = conn.execute("SELECT count(*) c FROM "
                               "heygen_resource_operation_refs WHERE "
                               "resource_id=?", (rid,)).fetchone()
            assert ref["c"] == 1
        finally:
            conn.close()

    def test_audio_role_maps_to_audio_asset(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn, role="synthetic_narration_audio",
                      digest=D_AUDIO, ctype="audio/wav", fname="narration.wav",
                      lref="r/n.wav")
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="aud", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            kind = conn.execute("SELECT resource_kind FROM heygen_remote_resources "
                                "WHERE resource_id=?", (rid,)).fetchone()[0]
            assert kind == "audio_asset"
        finally:
            conn.close()

    def test_reapply_after_uploaded_is_noop(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            rid1 = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            rid2 = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:02Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert rid1 == rid2  # no duplicate resource
            n = conn.execute("SELECT count(*) c FROM heygen_remote_resources "
                             "WHERE remote_id='ax'").fetchone()["c"]
            assert n == 1
        finally:
            conn.close()

    def test_empty_asset_id_rejected(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            with pytest.raises(ValueError):
                repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                    asset_id="  ", retention_mode="ephemeral",
                    credential_profile_id="prof", now_iso=NOW,
                    lease_owner=LEASE, expected_fence=c.fence)
        finally:
            conn.close()

    def test_reapply_with_different_asset_id_is_integrity_error(self):
        # Same upload row bound to a second remote asset → reject (blocker #5/C).
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id="u1", asset_id="ax",
                retention_mode="ephemeral", credential_profile_id="prof",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                    asset_id="different", retention_mode="ephemeral",
                    credential_profile_id="prof", now_iso="2026-07-30T00:00:02Z",
                    lease_owner=LEASE, expected_fence=c.fence)
        finally:
            conn.close()


class TestAssetFailure:
    def test_maybe_sent_sets_reconciliation_and_expiry(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-30T00:00:30Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert status == "reconciliation_required"
            row = conn.execute("SELECT maybe_sent_at, idempotency_expires_at, "
                               "last_error_code FROM heygen_asset_uploads "
                               "WHERE upload_id=?", ("u1",)).fetchone()
            assert row["maybe_sent_at"] is not None
            assert row["idempotency_expires_at"] is not None
            assert row["last_error_code"] == "network_timeout"
        finally:
            conn.close()

    def test_not_sent_permanent_is_failed(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="auth_failed",
                submission_certainty="not_sent", retryable=False, now_iso=NOW,
                lease_owner=LEASE, expected_fence=c.fence)
            assert status == "failed"
        finally:
            conn.close()

    def test_not_sent_retryable_returns_to_pending(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="connection_error",
                submission_certainty="not_sent", retryable=True, now_iso=NOW,
                lease_owner=LEASE, expected_fence=c.fence)
            assert status == "upload_pending"
            nr = conn.execute("SELECT next_retry_at FROM heygen_asset_uploads "
                              "WHERE upload_id=?", ("u1",)).fetchone()[0]
            assert nr is not None
        finally:
            conn.close()

    def test_past_idempotency_window_goes_manual_at_reclaim(self):
        # Per Codex #3: past the 24h replay window, the upload must be promoted
        # to manual BEFORE any provider call — i.e. at reclaim time, not at a
        # second failure.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn, now="2026-07-30T00:00:00Z")
            repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-30T00:00:30Z",
                lease_owner=LEASE, expected_fence=c.fence)
            # reclaim 25h later: past the 24h window → terminal (manual).
            reclaim = _do_claim(repo, conn, now="2026-07-31T01:00:30Z")
            assert reclaim.status == "terminal"
            st = conn.execute("SELECT status FROM heygen_asset_uploads "
                              "WHERE upload_id=?", ("u1",)).fetchone()[0]
            assert st == "manual_reconciliation_required"
        finally:
            conn.close()

    def test_stale_fence_apply_is_rejected(self):
        # A second worker claiming the same upload bumps the fence; the first
        # worker's apply with the stale fence must fail (no overwrite).
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c1 = _do_claim(repo, conn, now="2026-07-30T00:00:00Z")
            # worker 2 reclaims after lease expiry → fence bumps.
            _do_claim(repo, conn, now="2026-07-30T00:02:00Z")
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(
                    conn, upload_id="u1", asset_id="ax",
                    retention_mode="ephemeral", credential_profile_id="prof",
                    now_iso="2026-07-30T00:02:30Z",
                    lease_owner=LEASE, expected_fence=c1.fence)
        finally:
            conn.close()
