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
    ConsentService, ConsentConflictError, ConsentStateError, sha256_digest,
    DisclosedAsset, HeyGenOperationIdentity, ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.operation_repository import (
    OperationRepository,
    OperationIntegrityError,
    _check_asset_resource_consistency,
    _CONSENT_INTEGRITY_ERROR_CODE,
    AssetDeletionProcessor,
)
from lecturecast.heygen_asset_adapter import AssetDeleteResult, AssetReadError


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
        " credential_profile_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, "video", "/v3/videos", "gen", "sha256:m", "sha256:r",
         f"idem-{op_id}", f"lc:{op_id}", "heygen_env_default", "t", "t"),
    )
    # Default-grant a receipt with a CORRECT digest + set the op's consent
    # pointer so the full receipt-integrity validator passes at fenced apply.
    conn.execute(
        "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id,"
        " disclosure_version, generation_id, request_digest, creative_brief_digest,"
        " provider, operation_kind, disclosed_assets_json, data_categories_json,"
        " provider_cost_disclosure, agentmesh_non_processor_disclosure, status,"
        " consented_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"placeholder-{op_id}", op_id, "heygen-transfer-2026-07-27", "gen",
         "sha256:r", "sha256:b", "heygen", "video", "[]", "[]", "cost",
         "nonproc", "granted", "2026-07-29T00:00:00Z", "t"))
    row = conn.execute(
        "SELECT * FROM heygen_consent_receipts WHERE operation_id=?",
        (op_id,)).fetchone()
    stored = ConsentService._stored_content_no_time(row)
    stored["decision_at"] = row["consented_at"] or ""
    digest = sha256_digest(stored)
    conn.execute(
        "UPDATE heygen_consent_receipts SET receipt_digest=? WHERE operation_id=?",
        (digest, op_id))
    conn.execute(
        "UPDATE heygen_operations SET consent_receipt_digest=? WHERE operation_id=?",
        (digest, op_id))


def _withdraw_op(conn, op_id="op1"):
    """Simulate ConsentService.withdraw's DB effect: receipt → withdrawn (with a
    valid withdrawn_at), op consent pointer cleared (so the enqueue entry + apply
    integrity checks pass)."""
    conn.execute(
        "UPDATE heygen_consent_receipts SET status='withdrawn', "
        "withdrawn_at='2026-07-30T00:00:00Z' WHERE operation_id=?", (op_id,))
    conn.execute(
        "UPDATE heygen_operations SET consent_receipt_digest=NULL "
        "WHERE operation_id=?", (op_id,))


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
            # Bare parent op with NO receipt (_add_parent_op grants one by default).
            conn.execute(
                "INSERT INTO heygen_operations (operation_id, kind, endpoint, "
                "generation_id, manifest_digest, request_digest, idempotency_key, "
                "heygen_title, credential_profile_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("op_bare", "video", "/v3/videos", "gen", "sha256:m", "sha256:r",
                 "i_bare", "lc:op_bare", "heygen_env_default", "t", "t"))
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


    def test_recover_picks_up_crashed_enqueue(self):
        # Simulate: receipt flipped to withdrawn but the enqueue did NOT run
        # (withdraw crashed between commit and enqueue). recover must still
        # enqueue the uploaded asset → cleanup_required.
        td = tempfile.mkdtemp()
        conn = init_database(Path(td))
        conn.row_factory = sqlite3.Row
        try:
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            repo = OperationRepository(Path(td))
            conn.execute("BEGIN")
            c = _do_claim(repo, conn, parent=op_id, digest=D_PORT)
            repo.apply_asset_outcome_in_tx(
                conn, upload_id=c.upload_id, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            conn.execute("COMMIT")
            # crash window: flip the receipt withdrawn WITHOUT enqueue (real
            # withdraw also clears the pointer; simulate that committed)
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='withdrawn', withdrawn_at='2026-07-30T00:00:00Z' "
                "WHERE operation_id=?", (op_id,))
            conn.execute(
                "UPDATE heygen_operations SET consent_receipt_digest=NULL "
                "WHERE operation_id=?", (op_id,))
            conn.commit()
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                (c.upload_id,)).fetchone()[0] == "uploaded"  # not yet enqueued
            tally = repo.recover_withdrawn_asset_cleanups(now_iso="2026-07-30T00:05:00Z")
            assert tally["cleanup_required"] == 1
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                (c.upload_id,)).fetchone()[0] == "cleanup_required"
        finally:
            conn.close()

    def test_recover_is_idempotent_across_runs(self):
        td = tempfile.mkdtemp()
        conn = init_database(Path(td))
        conn.row_factory = sqlite3.Row
        try:
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            repo = OperationRepository(Path(td))
            conn.execute("BEGIN")
            c = _do_claim(repo, conn, parent=op_id, digest=D_PORT)
            repo.apply_asset_outcome_in_tx(
                conn, upload_id=c.upload_id, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            conn.execute("COMMIT")
            ConsentService(Path(td)).withdraw(operation_id=op_id)
            first = repo.recover_withdrawn_asset_cleanups(now_iso="2026-07-30T00:05:00Z")
            second = repo.recover_withdrawn_asset_cleanups(now_iso="2026-07-30T00:06:00Z")
            # withdraw already enqueued; both recover runs see it kept (idempotent)
            assert first["cleanup_required"] == 0
            assert second["cleanup_required"] == 0
            assert first["kept"] >= 1 and second["kept"] >= 1
        finally:
            conn.close()


class TestConsentWithdrawalCleanup:
    """enqueue_consent_withdrawal_cleanup state machine + withdraw wiring."""

    def _insert_upload(self, conn, *, upload_id, parent="op1",
                      role="portrait_photo", status="upload_pending",
                      remote_resource_id=None, maybe_sent_at=None,
                      idempotency_expires_at=None, attempt_started_at=None,
                      lease_owner=None):
        conn.execute(
            "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, "
            "asset_role, content_digest, local_ref, content_type, size_bytes, "
            "provider_filename, idempotency_key, status, remote_resource_id, "
            "maybe_sent_at, idempotency_expires_at, attempt_started_at, lease_owner, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (upload_id, parent, role, D_PORT, "r/p", "image/png", 10,
             "portrait.png", f"k_{upload_id}", status, remote_resource_id,
             maybe_sent_at, idempotency_expires_at, attempt_started_at, lease_owner,
             "t", "t"))

    def test_uploaded_goes_cleanup_required(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z", lease_owner=LEASE,
                expected_fence=c.fence)  # → uploaded + resource
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso="2026-07-30T00:00:10Z")
            assert tally["cleanup_required"] == 1
            row = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                (_U1,)).fetchone()
            assert row["status"] == "cleanup_required"
            res = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE remote_id='ax'").fetchone()
            assert res["deletion_status"] == "deletion_pending"
            assert res["deletion_reason"] == "consent_withdrawal"
        finally:
            conn.close()

    def test_upload_pending_provably_clean_is_cancelled(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            self._insert_upload(conn, upload_id="u_clean", status="upload_pending")
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso=NOW)
            assert tally["cancelled"] == 1
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='u_clean'"
            ).fetchone()[0] == "cancelled"
        finally:
            conn.close()

    def test_upload_pending_with_maybe_sent_trace_is_manual(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            self._insert_upload(conn, upload_id="u_ms", status="upload_pending",
                                maybe_sent_at="2026-07-30T00:00:00Z",
                                idempotency_expires_at="2026-07-31T00:00:00Z")
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso=NOW)
            assert tally["manual"] == 1
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='u_ms'"
            ).fetchone()[0] == "manual_reconciliation_required"
        finally:
            conn.close()

    def test_uploading_is_left_intact(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn)  # status=uploading, lease active
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso=NOW)
            assert tally["left_uploading"] == 1
            # row untouched — fenced apply will catch the withdraw
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                (_U1,)).fetchone()[0] == "uploading"
        finally:
            conn.close()

    def test_cleanup_required_is_idempotent(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            self._insert_upload(conn, upload_id="u_done", status="cleanup_required")
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso=NOW)
            assert tally["kept"] == 1
        finally:
            conn.close()

    def test_reconciliation_without_resource_is_manual(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            self._insert_upload(conn, upload_id="u_rec", status="reconciliation_required")
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso=NOW)
            assert tally["manual"] == 1
        finally:
            conn.close()

    def test_withdraw_wires_enqueue_end_to_end(self):
        # A real granted parent (valid receipt) + uploaded asset; withdraw flips
        # the asset to cleanup_required in the same tx.
        td = tempfile.mkdtemp()
        conn = init_database(Path(td))
        conn.row_factory = sqlite3.Row
        try:
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            repo = OperationRepository(Path(td))
            conn.execute("BEGIN")
            c = _do_claim(repo, conn, parent=op_id, digest=D_PORT)
            repo.apply_asset_outcome_in_tx(
                conn, upload_id=c.upload_id, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            conn.execute("COMMIT")
            ConsentService(Path(td)).withdraw(operation_id=op_id)
            row = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                (c.upload_id,)).fetchone()
            assert row["status"] == "cleanup_required"
        finally:
            conn.close()


class TestAssetApplyConsentRecheck:
    """The fenced-apply consent re-check (e5b0c3b race closure): if consent is
    no longer granted when the adapter returns, the resource is still recorded
    but marked for deletion, and the upload goes cleanup_required."""

    def _claim(self, repo, conn):
        return _do_claim(repo, conn)

    def test_apply_with_granted_receipt_is_uploaded(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)  # granted receipt
            repo = OperationRepository(Path(td))
            c = self._claim(repo, conn)
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert out.status == "uploaded"
            assert out.resource_id > 0
        finally:
            conn.close()

    def test_apply_with_withdrawn_receipt_is_cleanup_consent_withdrawal(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            # Real withdrawn state: status=withdrawn + consent pointer cleared
            # (ConsentService.withdraw clears the pointer).
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='withdrawn', withdrawn_at='2026-07-30T00:00:00Z' "
                "WHERE operation_id='op1'")
            conn.execute(
                "UPDATE heygen_operations SET consent_receipt_digest=NULL "
                "WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = self._claim(repo, conn)
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert out.status == "cleanup_required"
            res = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (out.resource_id,)).fetchone()
            assert res["deletion_status"] == "deletion_pending"
            assert res["deletion_reason"] == "consent_withdrawal"
            # asset row has no integrity error code for a clean withdrawal
            err = conn.execute(
                "SELECT last_error_code FROM heygen_asset_uploads "
                "WHERE upload_id=?", (_U1,)).fetchone()[0]
            assert err is None
        finally:
            conn.close()

    def test_apply_with_declined_receipt_is_cleanup_manual_force(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='declined' "
                "WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = self._claim(repo, conn)
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert out.status == "cleanup_required"
            res = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (out.resource_id,)).fetchone()
            assert res["deletion_status"] == "deletion_pending"
            assert res["deletion_reason"] == "manual_force"
            # integrity failure recorded on the asset, docked for manual cleanup
            err = conn.execute(
                "SELECT last_error_code FROM heygen_asset_uploads "
                "WHERE upload_id=?", (_U1,)).fetchone()[0]
            assert err == "consent_integrity_failure"
        finally:
            conn.close()

    def test_apply_with_missing_receipt_is_cleanup_manual_force(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            conn.execute("DELETE FROM heygen_consent_receipts WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = self._claim(repo, conn)
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert out.status == "cleanup_required"
            err = conn.execute(
                "SELECT last_error_code FROM heygen_asset_uploads "
                "WHERE upload_id=?", (_U1,)).fetchone()[0]
            assert err == "consent_integrity_failure"
        finally:
            conn.close()

    def test_idempotent_replay_reports_real_cleanup_status(self):
        # After a withdrawn-apply, re-apply must report cleanup_required (the
        # real DB state), never re-report uploaded.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='withdrawn', withdrawn_at='2026-07-30T00:00:00Z' "
                "WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = self._claim(repo, conn)
            first = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            second = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax", now_iso="2026-07-30T00:00:02Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert first.status == "cleanup_required"
            assert second.status == "cleanup_required"
            assert first.resource_id == second.resource_id
        finally:
            conn.close()


# === repository claim / apply / failure ====================================

# Canonical derived identity for the default (op1, portrait_photo, D_PORT).
from lecturecast.heygen_asset_adapter import derive_asset_identity
_U1, _K1 = derive_asset_identity("op1", "portrait_photo", D_PORT)


def _do_claim(repo, conn, parent="op1", role="portrait_photo",
              digest=D_PORT, now=NOW, size=1024,
              ctype="image/png", fname="portrait.png", lref="r/p.png"):
    # upload_id + idempotency_key are ALWAYS derived from (parent, role, digest);
    # the caller cannot forge them.
    upload_id, idem = derive_asset_identity(parent, role, digest)
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
                "WHERE upload_id=?", (_U1,)).fetchone()
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
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
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
                              (_U1,)).fetchone()
            assert st["status"] == "manual_reconciliation_required"
            assert st["maybe_sent_at"] is not None
            assert st["idempotency_expires_at"] is not None
        finally:
            conn.close()

    def test_repeated_crash_does_not_extend_24h_window(self):
        # t0 send + crash, t23 reclaim (within window) + crash, t46 reclaim must
        # STILL be past the window — the deadline is frozen at the t0 send, not
        # recomputed from the t23 reclaim's attempt_started_at (blocker #1).
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")        # t0
            mid = _do_claim(repo, conn, now="2026-07-30T23:00:00Z")  # t23 reclaim
            assert mid.status == "claimed"  # within 24h of t0
            # the frozen deadline must be anchored at t0, not t23
            frozen = conn.execute(
                "SELECT idempotency_expires_at FROM heygen_asset_uploads "
                "WHERE upload_id=?", (_U1,)).fetchone()[0]
            assert frozen is not None
            assert frozen < "2026-07-31T00:00:00Z"  # ≈ t0+24h, well before t23+24h
            final = _do_claim(repo, conn, now="2026-07-31T22:00:00Z")  # t46
            assert final.status == "terminal"  # past the t0-anchored deadline
        finally:
            conn.close()

    def test_non_canonical_identity_rejected(self):
        # The repository re-derives upload_id + idempotency_key from
        # (parent, role, digest) and rejects any non-canonical pair — a forged
        # caller cannot write a mismatched identity (blocker #2).
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _, valid_idem = derive_asset_identity("op1", "portrait_photo", D_PORT)
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_upload_in_tx(
                    conn, upload_id=_U1, parent_operation_id="op1",
                    asset_role="portrait_photo", content_digest=D_PORT,
                    local_ref="r/p.png", content_type="image/png", size_bytes=1024,
                    provider_filename="portrait.png",
                    idempotency_key="forged-not-canonical",  # != derived
                    lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            # A mismatched upload_id is likewise rejected.
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_upload_in_tx(
                    conn, upload_id="forged-upload-id",
                    parent_operation_id="op1", asset_role="portrait_photo",
                    content_digest=D_PORT, local_ref="r/p.png",
                    content_type="image/png", size_bytes=1024,
                    provider_filename="portrait.png", idempotency_key=valid_idem,
                    lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
        finally:
            conn.close()


class TestAssetApplyOutcome:
    def test_inserts_resource_and_backfills(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                asset_id="ax", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence).resource_id
            up = conn.execute("SELECT status, remote_resource_id FROM "
                              "heygen_asset_uploads WHERE upload_id=?",
                              (_U1,)).fetchone()
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
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id=c.upload_id,
                asset_id="aud", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence).resource_id
            kind = conn.execute("SELECT resource_kind FROM heygen_remote_resources "
                                "WHERE resource_id=?", (rid,)).fetchone()[0]
            assert kind == "audio_asset"
        finally:
            conn.close()

    def test_credential_and_retention_derived_from_parent_and_role(self):
        # credential_profile_id is read from the PARENT operation, never passed
        # by the caller; retention_mode is derived from the role (ephemeral).
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)  # credential=heygen_env_default
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                asset_id="ax", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence).resource_id
            res = conn.execute("SELECT credential_profile_id, retention_mode "
                               "FROM heygen_remote_resources WHERE resource_id=?",
                               (rid,)).fetchone()
            assert res["credential_profile_id"] == "heygen_env_default"
            assert res["retention_mode"] == "ephemeral"
        finally:
            conn.close()

    def test_remote_id_collision_is_structured_error(self):
        # The same remote id under the same profile+kind (different upload)
        # must surface as OperationIntegrityError, not a raw sqlite3 error.
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn, "op_a"); _add_parent_op(conn, "op_b")
            repo = OperationRepository(Path(td))
            ca = _do_claim(repo, conn, parent="op_a")
            repo.apply_asset_outcome_in_tx(conn, upload_id=ca.upload_id, asset_id="dup",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=ca.fence)
            cb = _do_claim(repo, conn, parent="op_b")
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id=cb.upload_id, asset_id="dup",
                    now_iso="2026-07-30T00:00:02Z",
                    lease_owner=LEASE, expected_fence=cb.fence)
        finally:
            conn.close()

    def test_reapply_after_uploaded_is_noop(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            rid1 = repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                asset_id="ax", now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence).resource_id
            rid2 = repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                asset_id="ax", now_iso="2026-07-30T00:00:02Z",
                lease_owner=LEASE, expected_fence=c.fence).resource_id
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
                repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                    asset_id="  ", now_iso=NOW,
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
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            from lecturecast.operation_repository import OperationIntegrityError
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id=_U1,
                    asset_id="different", now_iso="2026-07-30T00:00:02Z",
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
                conn, upload_id=_U1, error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-30T00:00:30Z",
                lease_owner=LEASE, expected_fence=c.fence)
            assert status == "reconciliation_required"
            row = conn.execute("SELECT maybe_sent_at, idempotency_expires_at, "
                               "last_error_code FROM heygen_asset_uploads "
                               "WHERE upload_id=?", (_U1,)).fetchone()
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
                conn, upload_id=_U1, error_code="auth_failed",
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
                conn, upload_id=_U1, error_code="connection_error",
                submission_certainty="not_sent", retryable=True, now_iso=NOW,
                lease_owner=LEASE, expected_fence=c.fence)
            assert status == "upload_pending"
            nr = conn.execute("SELECT next_retry_at FROM heygen_asset_uploads "
                              "WHERE upload_id=?", (_U1,)).fetchone()[0]
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
                conn, upload_id=_U1, error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-30T00:00:30Z",
                lease_owner=LEASE, expected_fence=c.fence)
            # reclaim 25h later: past the 24h window → terminal (manual).
            reclaim = _do_claim(repo, conn, now="2026-07-31T01:00:30Z")
            assert reclaim.status == "terminal"
            st = conn.execute("SELECT status FROM heygen_asset_uploads "
                              "WHERE upload_id=?", (_U1,)).fetchone()[0]
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
                    conn, upload_id=_U1, asset_id="ax",
                    now_iso="2026-07-30T00:02:30Z",
                    lease_owner=LEASE, expected_fence=c1.fence)
        finally:
            conn.close()


class TestCheckAssetResourceConsistency:
    """Direct unit tests for the asset↔resource correspondence matrix:
    blocker #3 (status matrix) + round-3 #2 (deletion_reason /
    last_error_code matrix). Pure-function — no DB setup."""

    # --- status matrix (blocker #3, must not regress) ---
    def test_status_matrix_happy_paths(self):
        _check_asset_resource_consistency(
            "uploaded", "not_started",
            deletion_reason=None, last_error_code=None)
        _check_asset_resource_consistency(
            "cleanup_required", "deletion_pending",
            deletion_reason="consent_withdrawal", last_error_code=None)
        _check_asset_resource_consistency(
            "deleted", "deleted",
            deletion_reason="post_download", last_error_code=None)

    def test_status_mismatch_rejected(self):
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "uploaded", "deletion_pending",
                deletion_reason="consent_withdrawal", last_error_code=None)

    def test_unknown_deletion_status_rejected(self):
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "uploaded", "bogus",
                deletion_reason=None, last_error_code=None)

    # --- reason matrix (round-3 #2) ---
    def test_not_started_requires_null_reason(self):
        _check_asset_resource_consistency(
            "uploaded", "not_started",
            deletion_reason=None, last_error_code=None)
        # any reason set while nothing is being deleted is corrupt
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "uploaded", "not_started",
                deletion_reason="post_download", last_error_code=None)

    def test_deletion_pending_requires_known_reason(self):
        for reason in ("post_download", "consent_withdrawal"):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_pending",
                deletion_reason=reason, last_error_code=None)
        # null reason on a deletion-bearing state is corrupt
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_pending",
                deletion_reason=None, last_error_code=None)
        # out-of-vocabulary reason is corrupt
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_pending",
                deletion_reason="bogus", last_error_code=None)

    def test_manual_force_pending_requires_integrity_error_code(self):
        # the integrity path always records consent_integrity_failure on apply
        _check_asset_resource_consistency(
            "cleanup_required", "deletion_pending",
            deletion_reason="manual_force",
            last_error_code=_CONSENT_INTEGRITY_ERROR_CODE)
        # missing error code → the manual_force claim is unproven
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_pending",
                deletion_reason="manual_force", last_error_code=None)
        # wrong error code → corrupt
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_pending",
                deletion_reason="manual_force", last_error_code="upload_timeout")

    def test_manual_force_failed_requires_integrity_error_code(self):
        _check_asset_resource_consistency(
            "cleanup_required", "deletion_failed",
            deletion_reason="manual_force",
            last_error_code=_CONSENT_INTEGRITY_ERROR_CODE)
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "cleanup_required", "deletion_failed",
                deletion_reason="manual_force", last_error_code=None)

    def test_manual_force_deleted_requires_integrity_error_code(self):
        # round-4 #2: the resource row's deletion_reason is a generic cause
        # marker that does NOT durably encode consent_integrity_failure, so the
        # asset's last_error_code is the ONLY durable integrity-cause signal —
        # and it must persist through EVERY deletion state, including 'deleted'.
        _check_asset_resource_consistency(
            "deleted", "deleted",
            deletion_reason="manual_force",
            last_error_code=_CONSENT_INTEGRITY_ERROR_CODE)
        # a cleared/missing code on a terminal manual_force is a forgery of the
        # integrity path, NOT an accepted terminal state.
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "deleted", "deleted",
                deletion_reason="manual_force", last_error_code=None)
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "deleted", "deleted",
                deletion_reason="manual_force", last_error_code="upload_timeout")

    def test_deleted_requires_known_reason(self):
        # post_download / consent_withdrawal carry no asset-side cause marker
        _check_asset_resource_consistency(
            "deleted", "deleted",
            deletion_reason="post_download", last_error_code=None)
        _check_asset_resource_consistency(
            "deleted", "deleted",
            deletion_reason="consent_withdrawal", last_error_code=None)
        # manual_force is the integrity path — it must keep the cause code even
        # at the terminal deleted state (round-4 #2)
        _check_asset_resource_consistency(
            "deleted", "deleted",
            deletion_reason="manual_force",
            last_error_code=_CONSENT_INTEGRITY_ERROR_CODE)
        with pytest.raises(OperationIntegrityError):
            _check_asset_resource_consistency(
                "deleted", "deleted",
                deletion_reason=None, last_error_code=None)


class TestE5b0c3bRound3Regressions:
    """Round-3 closures: fenced-apply receipt tampering → manual_force with the
    remote asset still recorded; consent-withdrawal cleanup enqueue state
    machine + fail-closed edges (unknown status, half-lease); idempotent-replay
    status/deletion_reason matrix (round-3 #2/#3)."""

    def _insert_upload(self, conn, *, upload_id, parent="op1",
                       role="portrait_photo", status="upload_pending",
                       remote_resource_id=None, maybe_sent_at=None,
                       idempotency_expires_at=None, attempt_started_at=None,
                       lease_owner=None, lease_expires_at=None):
        conn.execute(
            "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, "
            "asset_role, content_digest, local_ref, content_type, size_bytes, "
            "provider_filename, idempotency_key, status, remote_resource_id, "
            "maybe_sent_at, idempotency_expires_at, attempt_started_at, lease_owner, "
            "lease_expires_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (upload_id, parent, role, D_PORT, "r/p", "image/png", 10,
             "portrait.png", f"k_{upload_id}", status, remote_resource_id,
             maybe_sent_at, idempotency_expires_at, attempt_started_at, lease_owner,
             lease_expires_at, "t", "t"))

    def _assert_manual_force_with_remote(self, conn, out):
        # The remote asset is ALWAYS recorded (never orphaned); the integrity
        # path docks it manual_force + stamps consent_integrity_failure.
        assert out.status == "cleanup_required"
        assert out.resource_id > 0
        res = conn.execute(
            "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
            "WHERE resource_id=?", (out.resource_id,)).fetchone()
        assert res["deletion_status"] == "deletion_pending"
        assert res["deletion_reason"] == "manual_force"
        err = conn.execute(
            "SELECT last_error_code FROM heygen_asset_uploads "
            "WHERE upload_id=?", (_U1,)).fetchone()[0]
        assert err == "consent_integrity_failure"

    # --- fenced-apply receipt tampering → integrity → manual_force ----------
    def test_apply_corrupt_receipt_digest_is_manual_force(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)  # granted + valid digest
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            conn.execute(
                "UPDATE heygen_consent_receipts SET receipt_digest='sha256:bogus' "
                "WHERE operation_id='op1'")
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            self._assert_manual_force_with_remote(conn, out)
        finally:
            conn.close()

    def test_apply_corrupt_receipt_json_is_manual_force(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            # mutating the stored JSON invalidates the recomputed digest
            conn.execute(
                "UPDATE heygen_consent_receipts SET disclosed_assets_json='[\"x\"]' "
                "WHERE operation_id='op1'")
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            self._assert_manual_force_with_remote(conn, out)
        finally:
            conn.close()

    def test_apply_corrupt_binding_is_manual_force(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            # op.request_digest no longer matches the receipt binding field
            conn.execute(
                "UPDATE heygen_operations SET request_digest='sha256:zzz' "
                "WHERE operation_id='op1'")
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            self._assert_manual_force_with_remote(conn, out)
        finally:
            conn.close()

    # --- fenced-apply: malformed/invalid receipt fields → manual_force (round-4 #1)
    def test_apply_malformed_receipt_json_is_manual_force(self):
        # Syntactically broken JSON in a receipt column must be normalized to a
        # ConsentIntegrityError (the normalization try already catches
        # ValueError→ConsentIntegrityError), routing to manual_force — never a
        # raw JSONDecodeError that aborts the apply tx and orphans the asset.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            # relax the JSON-array CHECK only to plant syntactically-broken JSON
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "UPDATE heygen_consent_receipts SET disclosed_assets_json='{broken' "
                "WHERE operation_id='op1'")
            conn.execute("PRAGMA ignore_check_constraints = OFF")
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            self._assert_manual_force_with_remote(conn, out)
        finally:
            conn.close()

    def test_apply_invalid_withdrawn_at_is_manual_force(self):
        # A withdrawn receipt with a NAIVE (tz-unaware) withdrawn_at must route
        # to manual_force, NOT escape as a raw ValueError that the fenced-apply
        # `except ConsentError` misses (rolling its tx back and orphaning the
        # already-returned remote asset) — round-4 #1.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            # withdraw the receipt but plant a tz-NAIVE withdrawn_at (no 'Z')
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='withdrawn', "
                "withdrawn_at='2026-07-30T00:00:00' WHERE operation_id='op1'")
            conn.execute(
                "UPDATE heygen_operations SET consent_receipt_digest=NULL "
                "WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            out = repo.apply_asset_outcome_in_tx(
                conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)
            self._assert_manual_force_with_remote(conn, out)
        finally:
            conn.close()

    # --- granted receipt mis-call on enqueue is refused ---------------------
    def test_granted_receipt_mis_enqueue_is_rejected(self):
        # A still-granted operation must not have its assets enqueued for
        # withdrawal cleanup (internal mis-call, not a real withdraw).
        td = tempfile.mkdtemp()
        conn = init_database(Path(td))
        conn.row_factory = sqlite3.Row
        try:
            op_id = _grant_parent(td, assets=[("portrait_photo", D_PORT)])
            repo = OperationRepository(Path(td))
            conn.execute("BEGIN")
            from lecturecast.operation_repository import OperationStateError
            with pytest.raises(OperationStateError):
                repo.enqueue_consent_withdrawal_cleanup_in_tx(
                    conn, parent_operation_id=op_id, now_iso=NOW)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    # --- idempotent-replay status / deletion_reason matrix (round-3 #2) -----
    def test_replay_status_matrix_mismatch_rejected(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)  # uploaded / not_started
            # corrupt: resource advanced to deletion_pending but asset still uploaded
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deletion_pending' "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                    now_iso="2026-07-30T00:00:02Z",
                    lease_owner=LEASE, expected_fence=c.fence)
        finally:
            conn.close()

    def test_replay_deletion_reason_on_not_started_rejected(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)  # not_started / reason NULL
            # corrupt: a reason set while nothing is being deleted
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                    now_iso="2026-07-30T00:00:02Z",
                    lease_owner=LEASE, expected_fence=c.fence)
        finally:
            conn.close()

    def test_replay_deletion_reason_missing_on_pending_rejected(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            conn.execute(
                "UPDATE heygen_consent_receipts SET status='withdrawn', "
                "withdrawn_at='2026-07-30T00:00:00Z' WHERE operation_id='op1'")
            conn.execute(
                "UPDATE heygen_operations SET consent_receipt_digest=NULL "
                "WHERE operation_id='op1'")
            repo = OperationRepository(Path(td))
            c = _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                now_iso="2026-07-30T00:00:01Z",
                lease_owner=LEASE, expected_fence=c.fence)  # deletion_pending / consent_withdrawal
            # corrupt: reason dropped while the resource is still pending deletion
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason=NULL "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_outcome_in_tx(conn, upload_id=_U1, asset_id="ax",
                    now_iso="2026-07-30T00:00:02Z",
                    lease_owner=LEASE, expected_fence=c.fence)
        finally:
            conn.close()

    # --- consent-withdrawal cleanup enqueue edges (round-3 #3) --------------
    def test_enqueue_expired_uploading_is_manual(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")  # lease 60s → 00:01:00
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso="2026-07-30T00:02:00Z")
            assert tally["manual"] == 1
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)
            ).fetchone()[0] == "manual_reconciliation_required"
        finally:
            conn.close()

    def test_enqueue_active_lease_is_left_uploading(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")  # lease to 00:01:00
            _withdraw_op(conn)
            tally = repo.enqueue_consent_withdrawal_cleanup_in_tx(
                conn, parent_operation_id="op1", now_iso="2026-07-30T00:00:30Z")
            assert tally["left_uploading"] == 1
            assert conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)
            ).fetchone()[0] == "uploading"
        finally:
            conn.close()

    def test_enqueue_half_lease_rejected(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn, now="2026-07-30T00:00:00Z")
            _withdraw_op(conn)
            # corrupt half-lease: owner cleared but lease_expires_at left set
            conn.execute(
                "UPDATE heygen_asset_uploads SET lease_owner=NULL WHERE upload_id=?",
                (_U1,))
            with pytest.raises(OperationIntegrityError):
                repo.enqueue_consent_withdrawal_cleanup_in_tx(
                    conn, parent_operation_id="op1", now_iso="2026-07-30T00:00:30Z")
        finally:
            conn.close()

    def test_enqueue_unknown_status_rejected(self):
        # A status outside the handled enum must fail closed, NOT fall into a
        # catch-all "kept" (round-3 #3). The schema CHECK normally forbids an
        # unknown status, so it is relaxed only for this injected-corruption
        # probe to reach the enqueue else-branch.
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            conn.execute("PRAGMA ignore_check_constraints = ON")
            self._insert_upload(conn, upload_id="u_unk", status="unknown_future")
            conn.execute("PRAGMA ignore_check_constraints = OFF")
            _withdraw_op(conn)
            with pytest.raises(OperationIntegrityError):
                repo.enqueue_consent_withdrawal_cleanup_in_tx(
                    conn, parent_operation_id="op1", now_iso=NOW)
        finally:
            conn.close()


class TestMarkAssetCleanupGuards:
    """Round-4 #3: direct regression tests for the _mark_asset_cleanup_in_tx
    fail-closed guards. The resource UPDATE is gated on deletion_status=
    not_started (never revive a deleted/deletion_failed resource); identity
    topology is re-verified (never touch a foreign resource); the parent ref
    must exist. Each guard must leave BOTH the asset and the resource row
    untouched on failure."""

    def _setup_uploaded(self):
        # granted receipt → apply docks the asset at 'uploaded' with its bound
        # resource at deletion_status='not_started' (the only valid starting
        # point for _mark_asset_cleanup_in_tx).
        conn, td = _db()
        conn.execute("BEGIN"); _add_parent_op(conn)
        repo = OperationRepository(Path(td))
        c = _do_claim(repo, conn)
        repo.apply_asset_outcome_in_tx(
            conn, upload_id=_U1, asset_id="ax",
            now_iso="2026-07-30T00:00:01Z",
            lease_owner=LEASE, expected_fence=c.fence)
        return conn, td, repo

    def _row(self, conn):
        return conn.execute(
            "SELECT * FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)
        ).fetchone()

    def _resource(self, conn):
        return conn.execute(
            "SELECT * FROM heygen_remote_resources WHERE remote_id='ax'"
        ).fetchone()

    def test_refuses_to_resurrect_deleted_resource(self):
        # A resource already at terminal 'deleted' must NOT be flipped back to
        # deletion_pending — the gated UPDATE matches 0 rows and fails closed.
        conn, td, repo = self._setup_uploaded()
        try:
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deleted', "
                "deletion_reason='post_download' WHERE remote_id='ax'")
            row = self._row(conn)
            with pytest.raises(OperationIntegrityError):
                repo._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal",
                    now_iso="2026-07-30T00:00:02Z")
            # both rows untouched: asset still uploaded, resource still deleted
            assert self._row(conn)["status"] == "uploaded"
            res = self._resource(conn)
            assert res["deletion_status"] == "deleted"
            assert res["deletion_reason"] == "post_download"
        finally:
            conn.close()

    def test_refuses_to_mutate_foreign_resource(self):
        # A resource whose created_by_operation_id no longer matches the asset's
        # parent is foreign — topology re-verification fails before any UPDATE.
        conn, td, repo = self._setup_uploaded()
        try:
            # a SECOND parent op the resource does NOT belong to (satisfies the
            # resource→operations FK so the rebind itself is admissible)
            _add_parent_op(conn, op_id="op_other")
            # rebind the resource to the OTHER op → the asset's
            # remote_resource_id now points at a foreign resource
            conn.execute(
                "UPDATE heygen_remote_resources SET created_by_operation_id='op_other' "
                "WHERE remote_id='ax'")
            row = self._row(conn)
            with pytest.raises(OperationIntegrityError):
                repo._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal",
                    now_iso="2026-07-30T00:00:02Z")
            # both rows untouched
            assert self._row(conn)["status"] == "uploaded"
            res = self._resource(conn)
            assert res["deletion_status"] == "not_started"
            assert res["created_by_operation_id"] == "op_other"
        finally:
            conn.close()

    def test_refuses_with_missing_parent_ref(self):
        # If the asset's parent_operation_id has no operation row, the guard
        # fails closed before touching the resource.
        conn, td, repo = self._setup_uploaded()
        try:
            conn.commit()
            # detach the parent without cascading the asset (FK off) → dangling
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN")
            conn.execute("DELETE FROM heygen_operations WHERE operation_id='op1'")
            row = self._row(conn)
            with pytest.raises(OperationIntegrityError):
                repo._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal",
                    now_iso="2026-07-30T00:00:02Z")
            assert self._row(conn)["status"] == "uploaded"
            assert self._resource(conn)["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_refuses_with_missing_resource_ref(self):
        # The resource→operation ref row is the binding record. With it gone
        # (parent op + resource both intact), _validate_asset_binding sees 0
        # refs and fails closed — a DISTINCT branch from a missing parent
        # operation row (round-5).
        conn, td, repo = self._setup_uploaded()
        try:
            rid = self._resource(conn)["resource_id"]
            conn.execute(
                "DELETE FROM heygen_resource_operation_refs "
                "WHERE resource_id=? AND operation_id='op1'", (rid,))
            row = self._row(conn)
            with pytest.raises(OperationIntegrityError):
                repo._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal",
                    now_iso="2026-07-30T00:00:02Z")
            assert self._row(conn)["status"] == "uploaded"
            assert self._resource(conn)["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_refuses_with_shared_resource_ref(self):
        # A resource referenced by TWO operations is shared — _validate_asset_binding
        # requires exactly ONE ref matching the asset's parent (round-5).
        conn, td, repo = self._setup_uploaded()
        try:
            _add_parent_op(conn, op_id="op_other")   # satisfy the ref→op FK
            rid = self._resource(conn)["resource_id"]
            conn.execute(
                "INSERT INTO heygen_resource_operation_refs "
                "(resource_id, operation_id, created_at) VALUES (?, 'op_other', 't')",
                (rid,))
            row = self._row(conn)
            with pytest.raises(OperationIntegrityError):
                repo._mark_asset_cleanup_in_tx(
                    conn, row=row, reason="consent_withdrawal",
                    now_iso="2026-07-30T00:00:02Z")
            assert self._row(conn)["status"] == "uploaded"
            assert self._resource(conn)["deletion_status"] == "not_started"
        finally:
            conn.close()


# === asset deletion lifecycle (§5.5e5b0c3c) ================================
#
# c1 primitives: claim_asset_deletion_in_tx / apply_asset_deletion_outcome_in_tx
# / AssetDeletionProcessor.delete_once. Fenced on the asset's OWN lease columns;
# claim flips asset uploaded→cleanup_required + resource not_started→
# deletion_pending(post_download) in one tx so the correspondence matrix stays
# self-consistent. manual_force resources are never auto-deleted.

def _setup_uploaded_for_delete():
    """granted receipt → asset 'uploaded' + resource 'not_started' (the normal
    post-download starting point for asset deletion). Returns (conn, td, repo)
    with an open BEGIN tx."""
    conn, td = _db()
    conn.execute("BEGIN"); _add_parent_op(conn)
    repo = OperationRepository(Path(td))
    c = _do_claim(repo, conn)
    repo.apply_asset_outcome_in_tx(
        conn, upload_id=_U1, asset_id="ax",
        now_iso="2026-07-30T00:00:01Z",
        lease_owner=LEASE, expected_fence=c.fence)
    return conn, td, repo


def _asset(conn):
    return conn.execute(
        "SELECT * FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)).fetchone()


def _resource(conn):
    return conn.execute(
        "SELECT * FROM heygen_remote_resources WHERE remote_id='ax'").fetchone()


def _force_state(conn, *, asset_status, ds, reason=None, lec=None,
                 attempts=0, next_retry=None, asset_err=None,
                 lease_owner=None, lease_exp=None, att_started=None):
    """Stamp both rows to an exact (asset_status, resource deletion) pair that
    satisfies the correspondence matrix, clearing the asset lease unless an
    active/half lease is explicitly requested."""
    conn.execute(
        "UPDATE heygen_asset_uploads SET status=?, last_error_code=?, "
        "lease_owner=?, lease_expires_at=?, attempt_started_at=?, "
        "next_retry_at=? WHERE upload_id=?",
        (asset_status, asset_err, lease_owner, lease_exp, att_started,
         None, _U1))
    conn.execute(
        "UPDATE heygen_remote_resources SET deletion_status=?, deletion_reason=?, "
        "last_deletion_error=?, deletion_attempts=?, deletion_next_retry_at=? "
        "WHERE remote_id='ax'",
        (ds, reason, lec, attempts, next_retry))


class TestAssetDeletionClaim:
    def test_uploaded_flips_to_cleanup_and_pending(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "claimed"
            assert claim.remote_id == "ax"
            assert claim.resource_id == _resource(conn)["resource_id"]
            # asset uploaded→cleanup_required, resource not_started→deletion_pending(post_download)
            a = _asset(conn); r = _resource(conn)
            assert a["status"] == "cleanup_required"
            assert r["deletion_status"] == "deletion_pending"
            assert r["deletion_reason"] == "post_download"
            assert r["deletion_attempts"] == 1
            # lease acquired on the asset's OWN columns; fence bumped 1→2.
            assert a["lease_owner"] == LEASE
            assert a["lease_expires_at"] == "2026-07-30T00:01:00+00:00"
            assert a["lease_fence"] == 2
            assert a["attempt_started_at"] is not None
            assert claim.fence == 2
        finally:
            conn.close()

    def test_cleanup_required_reclaim_inherits_reason(self):
        # A consent_withdrawal resource (asset cleanup_required, resource
        # deletion_pending/consent_withdrawal) reclaims WITHOUT reseeding
        # post_download — the reason is inherited.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                         reason="consent_withdrawal", attempts=1)
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "claimed"
            r = _resource(conn)
            assert r["deletion_status"] == "deletion_pending"
            assert r["deletion_reason"] == "consent_withdrawal"
            assert r["deletion_attempts"] == 2
        finally:
            conn.close()

    def test_unknown_upload_not_ready(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id="does-not-exist", lease_owner=LEASE,
                now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
            assert claim.resource_id is None
        finally:
            conn.close()

    @pytest.mark.parametrize("asset_status", [
        "upload_pending", "uploading", "failed", "cancelled",
        "reconciliation_required", "manual_reconciliation_required", "deleted",
    ])
    def test_not_ready_for_ineligible_asset_status(self, asset_status):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status=asset_status, ds="not_started")
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
        finally:
            conn.close()

    def test_not_ready_when_asset_already_deleted(self):
        # Legal terminal state (asset+resource both deleted): the asset status
        # gate returns not_ready before any state mutation. (A deleted resource
        # can only pair matrix-validly with a deleted asset, which this gate
        # already catches — so there is no separate resource-deleted branch.)
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="deleted", ds="deleted", reason="post_download")
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
        finally:
            conn.close()

    def test_claim_matrix_rejects_resource_deleted_with_live_asset(self):
        # A resource at 'deleted' paired with a still-live asset
        # (cleanup_required) is a matrix violation — claim fails closed rather
        # than silently "fixing" or resurrecting it (round-1 blocker #2).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deleted",
                         reason="post_download")
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_deletion_in_tx(
                    conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
        finally:
            conn.close()

    def test_claim_matrix_rejects_illegal_uploaded_pending(self):
        # uploaded must pair with not_started; uploaded+deletion_pending is a
        # silent-corruption vector — claim rejects it, never "advances" it.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="uploaded", ds="deletion_pending",
                         reason="post_download")
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_deletion_in_tx(
                    conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
        finally:
            conn.close()

    def test_not_ready_for_manual_force_resource(self):
        # manual_force is the integrity path's durable record — never auto-delete.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                         reason="manual_force", asset_err=_CONSENT_INTEGRITY_ERROR_CODE)
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
            assert _asset(conn)["status"] == "cleanup_required"
            assert _resource(conn)["deletion_reason"] == "manual_force"
        finally:
            conn.close()

    def test_not_ready_for_deletion_failed_manual_code(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_failed",
                         reason="post_download", lec="deletion_retry_exhausted", attempts=1)
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
        finally:
            conn.close()

    def test_not_ready_when_attempts_exhausted(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_failed",
                         reason="post_download", lec="network_timeout", attempts=3)
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "not_ready"
        finally:
            conn.close()

    def test_retry_wait_when_backoff_not_elapsed(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_failed",
                         reason="post_download", lec="network_timeout", attempts=1,
                         next_retry="2026-07-30T00:05:00+00:00")
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "retry_wait"
        finally:
            conn.close()

    def test_reclaim_after_backoff_eligible(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_failed",
                         reason="post_download", lec="network_timeout", attempts=1,
                         next_retry=None)  # backoff elapsed
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "claimed"
            assert _resource(conn)["deletion_status"] == "deletion_pending"
            assert _resource(conn)["deletion_reason"] == "post_download"
            assert _resource(conn)["deletion_attempts"] == 2
        finally:
            conn.close()

    def test_busy_on_active_lease(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                         reason="post_download", attempts=1,
                         lease_owner="other-worker",
                         lease_exp="2026-07-30T00:05:00+00:00",
                         att_started="2026-07-30T00:00:00+00:00")
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "busy"
        finally:
            conn.close()

    def test_half_lease_raises(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                         reason="post_download", attempts=1,
                         lease_owner="other-worker", lease_exp=None)  # half lease
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_deletion_in_tx(
                    conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
        finally:
            conn.close()

    def test_rejects_foreign_resource(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _add_parent_op(conn, op_id="op_other")
            conn.execute(
                "UPDATE heygen_remote_resources SET created_by_operation_id='op_other' "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_deletion_in_tx(
                    conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert _asset(conn)["status"] == "uploaded"
            assert _resource(conn)["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_rejects_missing_resource_ref(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            rid = _resource(conn)["resource_id"]
            conn.execute(
                "DELETE FROM heygen_resource_operation_refs "
                "WHERE resource_id=? AND operation_id='op1'", (rid,))
            with pytest.raises(OperationIntegrityError):
                repo.claim_asset_deletion_in_tx(
                    conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert _asset(conn)["status"] == "uploaded"
        finally:
            conn.close()

    def test_claim_preserves_error_code(self):
        # The claim SET must not clear last_error_code (the manual_force marker
        # must persist through reclaim). Verified with a sentinel on a
        # non-manual cleanup_required resource.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                         reason="consent_withdrawal", attempts=1)
            conn.execute(
                "UPDATE heygen_asset_uploads SET last_error_code='sentinel' "
                "WHERE upload_id=?", (_U1,))
            claim = repo.claim_asset_deletion_in_tx(
                conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW, lease_seconds=60)
            assert claim.status == "claimed"
            assert _asset(conn)["last_error_code"] == "sentinel"
        finally:
            conn.close()


class TestApplyAssetDeletionOutcome:
    def _claim(self, repo, conn, max_attempts=3):
        return repo.claim_asset_deletion_in_tx(
            conn, upload_id=_U1, lease_owner=LEASE, now_iso=NOW,
            lease_seconds=60, max_attempts=max_attempts)

    def test_apply_deleted_flips_both_rows(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso="2026-07-30T00:00:30Z",
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "deleted"
            a = _asset(conn); r = _resource(conn)
            assert a["status"] == "deleted"
            assert r["deletion_status"] == "deleted"
            assert r["deleted_at"] == "2026-07-30T00:00:30+00:00"
            assert r["last_deletion_error"] is None
            # lease cleared, fence preserved
            assert a["lease_owner"] is None
            assert a["attempt_started_at"] is None
            assert a["lease_fence"] == claim.fence
            assert a["last_error_code"] is None
        finally:
            conn.close()

    def test_apply_already_absent_is_deleted(self):
        # 404 is idempotent success per spec §3.5.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="already_absent"))
            assert out.status == "deleted"
            assert _asset(conn)["status"] == "deleted"
            assert _resource(conn)["deletion_status"] == "deleted"
        finally:
            conn.close()

    def test_apply_retryable_sets_backoff(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetReadError(code="network_timeout", retryable=True))
            assert out.status == "failed"
            assert out.last_error == "network_timeout"
            assert out.next_retry_at == "2026-07-30T00:02:00+00:00"
            a = _asset(conn); r = _resource(conn)
            assert a["status"] == "cleanup_required"
            assert a["lease_owner"] is None
            assert a["attempt_started_at"] is None
            assert a["lease_fence"] == claim.fence
            assert r["deletion_status"] == "deletion_failed"
            assert r["last_deletion_error"] == "network_timeout"
            assert r["deletion_next_retry_at"] == "2026-07-30T00:02:00+00:00"
        finally:
            conn.close()

    def test_apply_permanent_sets_reconciliation_required(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetReadError(code="validation_error", retryable=False))
            assert out.status == "failed"
            assert out.last_error == "deletion_reconciliation_required"
            assert out.next_retry_at is None
            r = _resource(conn)
            assert r["deletion_status"] == "deletion_failed"
            assert r["last_deletion_error"] == "deletion_reconciliation_required"
            assert r["deletion_next_retry_at"] is None
        finally:
            conn.close()

    def test_apply_exhausted_sets_retry_exhausted(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn, max_attempts=2)
            rid = claim.resource_id
            # Simulate being on the LAST allowed attempt.
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_attempts=2 "
                "WHERE remote_id='ax'")
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW, max_attempts=2,
                expected_remote_id=claim.remote_id,
                result=AssetReadError(code="network_timeout", retryable=True))
            assert out.status == "failed"
            assert out.last_error == "deletion_retry_exhausted"
            assert out.next_retry_at is None
            assert _resource(conn)["last_deletion_error"] == "deletion_retry_exhausted"
        finally:
            conn.close()

    def test_apply_fence_conflict_wrong_owner(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner="not-the-owner",
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "fence_conflict"
            assert _asset(conn)["status"] == "cleanup_required"
            assert _resource(conn)["deletion_status"] == "deletion_pending"
        finally:
            conn.close()

    def test_apply_fence_conflict_wrong_fence(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence + 99, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "fence_conflict"
        finally:
            conn.close()

    def test_apply_fence_conflict_when_resource_not_pending(self):
        # resource flipped to deletion_failed between claim and apply. The asset
        # is still cleanup_required+leased (CAS matches), and cleanup_required↔
        # deletion_failed is matrix-valid, but the resource is no longer
        # deletion_pending → fence_conflict (never blindly re-advance it).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deletion_failed', "
                "deletion_reason='post_download' WHERE remote_id='ax'")
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "fence_conflict"
        finally:
            conn.close()

    def test_apply_rejects_resource_deleted_tamper(self):
        # resource flipped to 'deleted' between claim and apply while the asset
        # is still cleanup_required+leased. cleanup_required↔deleted is a matrix
        # violation (a legitimate apply would have flipped the asset to deleted
        # in the same tx and cleared the lease) → fail-closed integrity error,
        # never a silent fence_conflict that hides the corruption (round-1 #2).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deleted', "
                "deletion_reason='post_download' WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                    fence=claim.fence, now_iso=NOW,
                    expected_remote_id=claim.remote_id,
                    result=AssetDeleteResult(status="deleted"))
        finally:
            conn.close()

    def test_apply_rejects_foreign_resource(self):
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            rid = claim.resource_id
            _add_parent_op(conn, op_id="op_other")
            conn.execute(
                "UPDATE heygen_remote_resources SET created_by_operation_id='op_other' "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=rid, lease_owner=LEASE,
                    fence=claim.fence, now_iso=NOW,
                    expected_remote_id=claim.remote_id,
                    result=AssetDeleteResult(status="deleted"))
        finally:
            conn.close()

    def test_apply_rejects_wrong_resource_id(self):
        # The claim's resource_id is bound into the lease CAS; an apply with a
        # different resource_id cannot record the DELETE against the wrong row
        # (round-1 blocker #1).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=99999, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "fence_conflict"
            # nothing mutated
            assert _asset(conn)["status"] == "cleanup_required"
            assert _resource(conn)["deletion_status"] == "deletion_pending"
        finally:
            conn.close()

    def test_apply_rejects_sibling_resource_swap(self):
        # Swap the asset's remote_resource_id to a topology-valid sibling
        # between claim and apply. _validate_asset_binding would pass for the
        # sibling (it only proves B is currently valid, not B==A), so the lease
        # CAS — which binds remote_resource_id to the claim's resource_id — is
        # what catches the swap: it no longer matches → fence_conflict, and the
        # sibling is never touched (round-1 blocker #1).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            orig_rid = claim.resource_id
            conn.execute(
                "INSERT INTO heygen_remote_resources ("
                "  credential_profile_id, resource_kind, remote_id, retention_mode,"
                "  created_by_operation_id, deletion_status, created_at, updated_at"
                ") VALUES ('heygen_env_default','portrait_asset','sib','ephemeral','op1',"
                "          'not_started','t','t')")
            sib_rid = conn.execute(
                "SELECT resource_id FROM heygen_remote_resources WHERE remote_id='sib'"
                ).fetchone()[0]
            conn.execute(
                "INSERT INTO heygen_resource_operation_refs "
                "(resource_id, operation_id, created_at) VALUES (?, 'op1', 't')",
                (sib_rid,))
            # rebind the asset row to the sibling (the attack)
            conn.execute(
                "UPDATE heygen_asset_uploads SET remote_resource_id=? "
                "WHERE upload_id=?", (sib_rid, _U1))
            out = repo.apply_asset_deletion_outcome_in_tx(
                conn, upload_id=_U1, resource_id=orig_rid, lease_owner=LEASE,
                fence=claim.fence, now_iso=NOW,
                expected_remote_id=claim.remote_id,
                result=AssetDeleteResult(status="deleted"))
            assert out.status == "fence_conflict"
            # neither resource was deleted
            assert _resource(conn)["deletion_status"] == "deletion_pending"
            sib = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='sib'").fetchone()
            assert sib["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_apply_rejects_reason_swap_to_manual_force(self):
        # claim (post_download) → between txs the resource reason is flipped to
        # manual_force while the asset's last_error_code stays NULL → apply's
        # matrix check fails closed (manual_force requires the integrity marker),
        # never auto-deleting an integrity-path record nor clearing the marker
        # (round-1 blocker #2).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='manual_force' "
                "WHERE remote_id='ax'")
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=claim.resource_id,
                    lease_owner=LEASE, fence=claim.fence, now_iso=NOW,
                    expected_remote_id=claim.remote_id,
                    result=AssetDeleteResult(status="deleted"))
            # nothing applied: asset still leased cleanup_required, resource pending
            assert _asset(conn)["status"] == "cleanup_required"
            assert _resource(conn)["deletion_status"] == "deletion_pending"
        finally:
            conn.close()

    def test_apply_rejects_remote_id_tamper(self):
        # claim resource (remote_id='ax') → adapter DELETEs 'ax' → between txs
        # the SAME resource row's remote_id is renamed to 'swapped' (resource_id
        # unchanged, so the lease CAS still matches). Without binding the CURRENT
        # remote_id to the one the adapter operated on, apply would mark the
        # renamed row deleted — recording the DELETE of 'ax' against 'swapped'.
        # _validate_asset_binding(expected_remote_id=claim.remote_id) catches it
        # → integrity error, neither row state advances (round-2 blocker).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            conn.execute(
                "UPDATE heygen_remote_resources SET remote_id='swapped' "
                "WHERE resource_id=?", (claim.resource_id,))
            with pytest.raises(OperationIntegrityError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=claim.resource_id,
                    lease_owner=LEASE, fence=claim.fence, now_iso=NOW,
                    expected_remote_id=claim.remote_id,
                    result=AssetDeleteResult(status="deleted"))
            # nothing applied
            a = _asset(conn)
            assert a["status"] == "cleanup_required"
            assert a["lease_owner"] == LEASE  # lease still held
            renamed = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE resource_id=?", (claim.resource_id,)).fetchone()
            assert renamed["deletion_status"] == "deletion_pending"
        finally:
            conn.close()

    def test_apply_rejects_null_expected_remote_id(self):
        # 'required kwarg' only prevents OMISSION; a caller can still pass None
        # explicitly, and _validate_asset_binding treats None as "skip". The
        # entry guard must reject it before any state mutation so the
        # remote-identity binding cannot be silently disabled (round-3 blocker).
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            conn.execute(
                "UPDATE heygen_remote_resources SET remote_id='swapped' "
                "WHERE resource_id=?", (claim.resource_id,))
            with pytest.raises(ValueError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=claim.resource_id,
                    lease_owner=LEASE, fence=claim.fence, now_iso=NOW,
                    expected_remote_id=None,
                    result=AssetDeleteResult(status="deleted"))
            # nothing applied: lease still held, resource still pending
            a = _asset(conn)
            assert a["status"] == "cleanup_required"
            assert a["lease_owner"] == LEASE
        finally:
            conn.close()

    def test_apply_rejects_empty_expected_remote_id(self):
        # An empty / malformed id is rejected by the same guard.
        conn, td, repo = _setup_uploaded_for_delete()
        try:
            claim = self._claim(repo, conn)
            with pytest.raises(ValueError):
                repo.apply_asset_deletion_outcome_in_tx(
                    conn, upload_id=_U1, resource_id=claim.resource_id,
                    lease_owner=LEASE, fence=claim.fence, now_iso=NOW,
                    expected_remote_id="",
                    result=AssetDeleteResult(status="deleted"))
            assert _asset(conn)["status"] == "cleanup_required"
        finally:
            conn.close()


class _FakeAdapter:
    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc

    def delete_asset(self, asset_id):
        if self._exc is not None:
            raise self._exc
        return self._result


class TestAssetDeletionProcessor:
    def _db_path(self, td):
        return str(Path(td) / ".lecturecast" / "runtime" / "heygen-operations.db")

    def test_happy_path_deleted(self):
        conn, td, repo = _setup_uploaded_for_delete()
        rid = _resource(conn)["resource_id"]
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(result=AssetDeleteResult(status="deleted")),
            now_iso=NOW, lease_seconds=60)
        assert res.claim.status == "claimed"
        assert res.outcome.status == "deleted"
        conn2 = sqlite3.connect(self._db_path(td))
        conn2.row_factory = sqlite3.Row
        a = conn2.execute("SELECT status FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)).fetchone()
        r = conn2.execute("SELECT deletion_status FROM heygen_remote_resources WHERE resource_id=?", (rid,)).fetchone()
        assert a["status"] == "deleted"
        assert r["deletion_status"] == "deleted"
        conn2.close()

    def test_already_absent_is_deleted(self):
        conn, td, repo = _setup_uploaded_for_delete()
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(result=AssetDeleteResult(status="already_absent")),
            now_iso=NOW, lease_seconds=60)
        assert res.outcome.status == "deleted"

    def test_retryable_error_failed_retry(self):
        conn, td, repo = _setup_uploaded_for_delete()
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(exc=AssetReadError(code="rate_limited", retryable=True)),
            now_iso=NOW, lease_seconds=60)
        assert res.outcome.status == "failed"
        assert res.outcome.last_error == "rate_limited"
        assert res.outcome.next_retry_at is not None

    def test_permanent_error_failed_terminal(self):
        conn, td, repo = _setup_uploaded_for_delete()
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(exc=AssetReadError(code="auth_failed", retryable=False)),
            now_iso=NOW, lease_seconds=60)
        assert res.outcome.status == "failed"
        assert res.outcome.last_error == "deletion_reconciliation_required"
        assert res.outcome.next_retry_at is None

    def test_not_ready_claim_no_outcome(self):
        # resource already deleted → claim not_ready → no adapter call, no outcome
        conn, td, repo = _setup_uploaded_for_delete()
        _force_state(conn, asset_status="deleted", ds="deleted", reason="post_download")
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(result=AssetDeleteResult(status="deleted")),
            now_iso=NOW, lease_seconds=60)
        assert res.claim.status == "not_ready"
        assert res.outcome is None

    def test_busy_claim_no_outcome(self):
        conn, td, repo = _setup_uploaded_for_delete()
        _force_state(conn, asset_status="cleanup_required", ds="deletion_pending",
                     reason="post_download", attempts=1,
                     lease_owner="other-worker",
                     lease_exp="2026-07-30T00:05:00+00:00",
                     att_started="2026-07-30T00:00:00+00:00")
        conn.commit(); conn.close()
        proc = AssetDeletionProcessor(td)
        res = proc.delete_once(
            upload_id=_U1, lease_owner=LEASE,
            adapter=_FakeAdapter(result=AssetDeleteResult(status="deleted")),
            now_iso=NOW, lease_seconds=60)
        assert res.claim.status == "busy"
        assert res.outcome is None

    def test_unknown_exception_propagates(self):
        # A non-AssetReadError exception from the adapter is NOT mapped to an
        # outcome — it propagates, leaving the lease to expire (certainty is
        # unknowable, never guess a phantom outcome).
        conn, td, repo = _setup_uploaded_for_delete()
        conn.commit(); conn.close()

        class _Boom(Exception):
            pass
        proc = AssetDeletionProcessor(td)
        with pytest.raises(_Boom):
            proc.delete_once(
                upload_id=_U1, lease_owner=LEASE,
                adapter=_FakeAdapter(exc=_Boom("transport died")),
                now_iso=NOW, lease_seconds=60)
        # asset is now leased (cleanup_required) but no outcome applied; a later
        # run after lease expiry can reclaim.
        conn2 = sqlite3.connect(self._db_path(td))
        conn2.row_factory = sqlite3.Row
        a = conn2.execute("SELECT status, lease_owner FROM heygen_asset_uploads WHERE upload_id=?", (_U1,)).fetchone()
        assert a["status"] == "cleanup_required"
        assert a["lease_owner"] == LEASE
        conn2.close()
