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
    ConsentService, ConsentConflictError, ConsentStateError,
)
from lecturecast.operation_repository import OperationRepository


D_PORT = "sha256:" + "a" * 64
D_AUDIO = "sha256:" + "b" * 64
D_OTHER = "sha256:" + "c" * 64
LEASE = "worker-1"
NOW = "2026-07-30T00:00:00Z"


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


def _add_receipt(conn, op_id="op1", status="granted",
                 assets=(("portrait_photo", D_PORT),)):
    disclosed = [{"kind": k, "filename": f"{k}.bin", "digest": d} for k, d in assets]
    conn.execute(
        "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id,"
        " disclosure_version, generation_id, request_digest, creative_brief_digest,"
        " provider, operation_kind, disclosed_assets_json, data_categories_json,"
        " provider_cost_disclosure, agentmesh_non_processor_disclosure, status,"
        " consented_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"rd-{op_id}", op_id, "heygen-transfer-2026-07-27", "gen", "sha256:r",
         "sha256:b", "heygen", "video", json.dumps(disclosed),
         json.dumps(["portrait_image"]), "cost", "nonproc", status, "t", "t"),
    )


# === consent guard =========================================================

class TestAssetConsentGuard:
    def _svc(self, td):
        return ConsentService(Path(td))

    def test_grants_when_disclosed(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            _add_receipt(conn, assets=[("portrait_photo", D_PORT)])
            r = self._svc(td).validate_asset_upload_consent_in_tx(
                conn, parent_operation_id="op1",
                asset_role="portrait_photo", content_digest=D_PORT)
            assert r.asset_role == "portrait_photo"
            assert r.content_digest == D_PORT
            assert r.receipt_digest == "rd-op1"
        finally:
            conn.close()

    def test_rejects_digest_not_disclosed(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            _add_receipt(conn, assets=[("portrait_photo", D_PORT)])
            with pytest.raises(ConsentConflictError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op1",
                    asset_role="portrait_photo", content_digest=D_OTHER)
        finally:
            conn.close()

    def test_rejects_role_digest_mismatch(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            _add_receipt(conn, assets=[("portrait_photo", D_PORT),
                                       ("synthetic_narration_audio", D_AUDIO)])
            # audio digest under portrait role → not disclosed for that role.
            with pytest.raises(ConsentConflictError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op1",
                    asset_role="portrait_photo", content_digest=D_AUDIO)
        finally:
            conn.close()

    def test_rejects_withdrawn_receipt(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            _add_receipt(conn, status="withdrawn",
                         assets=[("portrait_photo", D_PORT)])
            with pytest.raises(ConsentStateError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op1",
                    asset_role="portrait_photo", content_digest=D_PORT)
        finally:
            conn.close()

    def test_rejects_no_receipt(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN")
            _add_parent_op(conn)
            with pytest.raises(ConsentStateError):
                self._svc(td).validate_asset_upload_consent_in_tx(
                    conn, parent_operation_id="op1",
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
            _do_claim(repo, conn)
            repo.apply_asset_outcome_in_tx(conn, upload_id="u1", asset_id="ax",
                retention_mode="ephemeral", credential_profile_id="prof",
                now_iso="2026-07-30T00:00:01Z")
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
            _do_claim(repo, conn)
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z")
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
            _do_claim(repo, conn, role="synthetic_narration_audio",
                      digest=D_AUDIO, ctype="audio/wav", fname="narration.wav",
                      lref="r/n.wav")
            rid = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="aud", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z")
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
            _do_claim(repo, conn)
            rid1 = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:01Z")
            rid2 = repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                asset_id="ax", retention_mode="ephemeral",
                credential_profile_id="prof", now_iso="2026-07-30T00:00:02Z")
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
            _do_claim(repo, conn)
            with pytest.raises(ValueError):
                repo.apply_asset_outcome_in_tx(conn, upload_id="u1",
                    asset_id="  ", retention_mode="ephemeral",
                    credential_profile_id="prof", now_iso=NOW)
        finally:
            conn.close()


class TestAssetFailure:
    def test_maybe_sent_sets_reconciliation_and_expiry(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u2-fail" if False else "u1",
                error_code="network_timeout", submission_certainty="maybe_sent",
                retryable=True, now_iso="2026-07-30T00:00:30Z")
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
            _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="auth_failed",
                submission_certainty="not_sent", retryable=False, now_iso=NOW)
            assert status == "failed"
        finally:
            conn.close()

    def test_not_sent_retryable_returns_to_pending(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn)
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="connection_error",
                submission_certainty="not_sent", retryable=True, now_iso=NOW)
            assert status == "upload_pending"
            nr = conn.execute("SELECT next_retry_at FROM heygen_asset_uploads "
                              "WHERE upload_id=?", ("u1",)).fetchone()[0]
            assert nr is not None
        finally:
            conn.close()

    def test_past_idempotency_window_goes_manual(self):
        conn, td = _db()
        try:
            conn.execute("BEGIN"); _add_parent_op(conn)
            repo = OperationRepository(Path(td))
            _do_claim(repo, conn)
            # first maybe-sent at t0
            repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-30T00:00:30Z")
            # replay still ambiguous 25h later → past the 24h window
            status = repo.apply_asset_upload_failure_in_tx(
                conn, upload_id="u1", error_code="network_timeout",
                submission_certainty="maybe_sent", retryable=True,
                now_iso="2026-07-31T01:00:30Z")
            assert status == "manual_reconciliation_required"
        finally:
            conn.close()
