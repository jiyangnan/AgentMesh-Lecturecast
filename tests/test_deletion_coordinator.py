"""DeletionCoordinator §3.5 normal-order pass + maintenance sweep
(§5.5e5b0c3c-c3). Spans video + asset routing, distinct from test_deletion.py
(video-only processor) and test_asset_journal.py (asset/planner)."""

from __future__ import annotations
import sqlite3, tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from lecturecast.heygen_adapter import DeleteResult
from lecturecast.heygen_asset_adapter import AssetDeleteResult, AssetReadError
from lecturecast.heygen_journal import init_database
from lecturecast.operation_repository import (
    DELETION_MAX_ATTEMPTS, DeleteProcessor, AssetDeletionProcessor,
    DeletionCoordinator, OperationRepository, _CONSENT_INTEGRITY_ERROR_CODE)

NOW = "2026-07-31T00:00:00Z"
OWNER = "deletion-coord-w1"
LEASE_SECONDS = 300
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"

_ROLE_KIND = {
    "portrait_photo": "portrait_asset",
    "synthetic_narration_audio": "audio_asset",
}


# --- DB setup helpers (direct SQL mirror of the real post-download state) --

def _db():
    td = tempfile.mkdtemp()
    init_database(Path(td))
    return td


def _fresh_conn(td):
    conn = sqlite3.connect(str(Path(td) / DB_REL))
    conn.row_factory = sqlite3.Row
    return conn


def _add_op(conn, op_id, *, download_status="verified",
            credential="heygen_env_default"):
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint,"
        " generation_id, manifest_digest, request_digest, idempotency_key,"
        " heygen_title, credential_profile_id, download_status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, "video", "/v3/videos", "gen", "sha256:m", "sha256:r",
         f"idem-{op_id}", f"lc:{op_id}", credential, download_status, "t", "t"))


def _add_resource(conn, *, op_id, kind, remote_id, retention="ephemeral",
                  ds="not_started", reason=None, credential="heygen_env_default"):
    # A 'deleted' resource carries the apply terminal proof —
    # apply_deletion_outcome_in_tx always sets deleted_at NOT NULL and the claim
    # bumps deletion_attempts (>=1) before apply succeeds. Fixtures model the
    # REAL terminal state so the round-7 B1 witness gate (which requires this
    # proof) is satisfiable by legit deleted witnesses, and bypass tests hit
    # their INTENDED dimension instead of being short-circuited by deleted_at.
    # Tests that need an ANOMALOUS deleted row (no terminal proof) UPDATE it
    # away explicitly.
    deleted_at = "t" if ds == "deleted" else None
    attempts = 1 if ds == "deleted" else 0
    cur = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind,"
        " remote_id, retention_mode, created_by_operation_id, deletion_status,"
        " deletion_reason, deleted_at, deletion_attempts, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (credential, kind, remote_id, retention, op_id, ds, reason,
         deleted_at, attempts, "t", "t"))
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id,"
        " created_at) VALUES (?,?,?)", (rid, op_id, "t"))
    return rid


def _add_asset(conn, *, op_id, role, upload_id, remote_id, ds="not_started",
               asset_status="uploaded", credential="heygen_env_default"):
    """Full claimable asset: resource + upload + ref. Defaults to the normal
    post-download entry point (uploaded / not_started)."""
    rid = _add_resource(conn, op_id=op_id, kind=_ROLE_KIND[role],
                        remote_id=remote_id, retention="ephemeral", ds=ds,
                        credential=credential)
    conn.execute(
        "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
        " asset_role, content_digest, local_ref, content_type, size_bytes,"
        " provider_filename, idempotency_key, remote_resource_id, status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (upload_id, op_id, role, "sha256:" + remote_id, "loc",
         "application/octet-stream", 1, remote_id + ".bin", "idem-" + upload_id,
         rid, asset_status, "t", "t"))
    return rid


def _add_manual_asset(conn, *, op_id, role, upload_id, remote_id,
                      ds="deletion_pending"):
    """A matrix-valid manual_force asset: resource deletion_pending/failed +
    reason manual_force, upload cleanup_required + the consent_integrity_failure
    marker such a resource must carry. The c1 claim returns not_ready for
    manual_force (never auto-deleted) — but before the round-3 fix this resource
    still acted as a sweep authorization WITNESS for its siblings."""
    rid = _add_resource(conn, op_id=op_id, kind=_ROLE_KIND[role],
                        remote_id=remote_id, retention="ephemeral", ds=ds,
                        reason="manual_force")
    conn.execute(
        "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
        " asset_role, content_digest, local_ref, content_type, size_bytes,"
        " provider_filename, idempotency_key, remote_resource_id, status,"
        " last_error_code, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (upload_id, op_id, role, "sha256:" + remote_id, "loc",
         "application/octet-stream", 1, remote_id + ".bin", "idem-" + upload_id,
         rid, "cleanup_required", _CONSENT_INTEGRITY_ERROR_CODE, "t", "t"))
    return rid


# --- fakes (double as spies: .calls / .snapshots record arguments) --------

class _StubDeleter:
    """Video deleter. Raises exc if set, else returns result."""
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else DeleteResult("deleted")
        self.exc = exc
        self.calls = []

    def delete_video(self, remote_id):
        self.calls.append(remote_id)
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeAdapter:
    """Asset deleter. Raises exc if set, else returns result."""
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else AssetDeleteResult("deleted")
        self.exc = exc
        self.calls = []

    def delete_asset(self, asset_id):
        self.calls.append(asset_id)
        if self.exc is not None:
            raise self.exc
        return self.result


class _ScriptedAdapter:
    """Per-remote-id asset deleter: script[remote_id] = result | Exception."""
    def __init__(self, script):
        self.script = script
        self.calls = []

    def delete_asset(self, asset_id):
        self.calls.append(asset_id)
        v = self.script.get(asset_id, AssetDeleteResult("deleted"))
        if isinstance(v, Exception):
            raise v
        return v


class _Boom(Exception):
    pass


def _setup_full_op(td, *, op_id="op1", download_status="verified",
                   video_ds="not_started"):
    """Op with verified download + video + audio + portrait (all claimable)."""
    conn = _fresh_conn(td)
    try:
        conn.execute("BEGIN")
        _add_op(conn, op_id, download_status=download_status)
        vrid = _add_resource(conn, op_id=op_id, kind="video", remote_id="v1",
                             ds=video_ds)
        _add_asset(conn, op_id=op_id, role="synthetic_narration_audio",
                   upload_id="u_audio", remote_id="a1")
        _add_asset(conn, op_id=op_id, role="portrait_photo",
                   upload_id="u_port", remote_id="p1")
        conn.commit()
    finally:
        conn.close()
    return vrid


def _coord(td):
    return DeletionCoordinator(td)


# ===========================================================================
# §3.5 normal-mode gate + multi-pass tail
# ===========================================================================

class TestDeletePassNormalOrder:
    def test_normal_only_video_attempted_then_deleted(self, tmp_path):
        # T1 — a non-deleted video gates audio/portrait (§3.5).
        td = _db()
        vrid = _setup_full_op(td)
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        # Only the video is attempted this pass.
        assert [a.routed for a in res.attempts] == ["video"]
        assert res.attempts[0].outcome_status == "deleted"
        assert res.deleted == 1 and res.attempted == 1
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deleted_at FROM heygen_remote_resources "
                "WHERE resource_id=?", (vrid,)).fetchone()
            assert v["deletion_status"] == "deleted"
            assert v["deleted_at"] is not None
            # audio/portrait untouched (not attempted).
            audio = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='a1'").fetchone()
            port = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='p1'").fetchone()
            assert audio["deletion_status"] == "not_started"
            assert port["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_multi_pass_releases_tail_after_video_deleted(self, tmp_path):
        # T2 — once the video is deleted, audio→portrait become available.
        td = _db()
        _setup_full_op(td)
        coord = _coord(td)
        coord.delete_pass_for_operation(  # pass 1: delete video
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        res2 = coord.delete_pass_for_operation(  # pass 2: tail
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert [a.entry.resource_kind for a in res2.attempts] == \
            ["audio_asset", "portrait_asset"]
        assert [a.routed for a in res2.attempts] == ["asset", "asset"]
        assert [a.outcome_status for a in res2.attempts] == ["deleted", "deleted"]
        conn = _fresh_conn(td)
        try:
            for rid in ("a1", "p1"):
                r = conn.execute(
                    "SELECT deletion_status FROM heygen_remote_resources "
                    "WHERE remote_id=?", (rid,)).fetchone()
                assert r["deletion_status"] == "deleted"
        finally:
            conn.close()

    def test_force_excludes_video_attempts_tail(self, tmp_path):
        # T3 — force=True bypasses the video stage entirely (§3.5 force-cleanup).
        td = _db()
        _setup_full_op(td)
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=True, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert all(a.routed == "asset" for a in res.attempts)
        assert "video" not in [a.routed for a in res.attempts]
        assert sorted(a.entry.resource_kind for a in res.attempts) == \
            ["audio_asset", "portrait_asset"]
        assert [a.outcome_status for a in res.attempts] == ["deleted", "deleted"]


# ===========================================================================
# Routing matrix + skip/alert branches
# ===========================================================================

class TestDeletePassRouting:
    def test_video_routes_to_deleter_not_adapter(self, tmp_path):
        # T4 — video entry drives delete_video, never delete_asset.
        td = _db()
        _setup_full_op(td)
        deleter = _StubDeleter()
        adapter = _FakeAdapter()
        _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=deleter, adapter=adapter,
            lease_owner=OWNER, now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert deleter.calls == ["v1"]
        assert adapter.calls == []

    def test_assets_route_to_adapter_not_deleter(self, tmp_path):
        # T4 — force releases tail; assets drive delete_asset, never delete_video.
        td = _db()
        _setup_full_op(td)
        deleter = _StubDeleter()
        adapter = _FakeAdapter()
        _coord(td).delete_pass_for_operation(
            operation_id="op1", force=True, deleter=deleter, adapter=adapter,
            lease_owner=OWNER, now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert sorted(adapter.calls) == ["a1", "p1"]
        assert deleter.calls == []

    def test_asset_without_upload_id_is_skipped_with_alert(self, tmp_path):
        # T5 — bare asset resource (broken LEFT JOIN) is surfaced but not
        # drivable; never silently dropped.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "op1", download_status="verified")
            # audio resource with NO matching asset_uploads row → upload_id None.
            _add_resource(conn, op_id="op1", kind="audio_asset", remote_id="a1")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert len(res.attempts) == 1
        assert res.attempts[0].routed == "skipped_no_upload_id"
        assert res.skipped == 1
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='a1'").fetchone()
            assert r["deletion_status"] == "not_started"  # untouched
        finally:
            conn.close()

    def test_unknown_resource_kind_is_skipped_with_alert(self, tmp_path):
        # T6 — an unexpected ephemeral kind (order_key 9) is surfaced, not
        # dropped; the coordinator never guesses a route.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "op1", download_status="verified")
            _add_resource(conn, op_id="op1", kind="avatar_look", remote_id="al1",
                          retention="ephemeral")
            conn.commit()
        finally:
            conn.close()
        deleter = _StubDeleter()
        adapter = _FakeAdapter()
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=deleter, adapter=adapter,
            lease_owner=OWNER, now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert len(res.attempts) == 1
        assert res.attempts[0].routed == "skipped_unknown_kind"
        assert deleter.calls == [] and adapter.calls == []


# ===========================================================================
# Per-resource independence + untyped-exception contract
# ===========================================================================

class TestDeletePassPerResourceIndependence:
    def _tail_op(self, td, *, op_id="op1"):
        """Op with audio + portrait (no video) so the normal-mode tail is
        returned in one pass."""
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, op_id, download_status="verified")
            _add_asset(conn, op_id=op_id, role="synthetic_narration_audio",
                       upload_id="u_audio", remote_id="a1")
            _add_asset(conn, op_id=op_id, role="portrait_photo",
                       upload_id="u_port", remote_id="p1")
            conn.commit()
        finally:
            conn.close()

    def test_one_asset_failing_does_not_block_others(self, tmp_path):
        # T7 — a retryable failure on one asset leaves it failed+backoff while
        # the other still advances.
        td = _db()
        self._tail_op(td)
        adapter = _ScriptedAdapter({
            "a1": AssetReadError(code="rate_limited", retryable=True),
            "p1": AssetDeleteResult("deleted"),
        })
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        by_kind = {a.entry.resource_kind: a for a in res.attempts}
        assert by_kind["audio_asset"].outcome_status == "failed"
        assert by_kind["audio_asset"].last_error == "rate_limited"
        assert by_kind["audio_asset"].next_retry_at is not None
        assert by_kind["portrait_asset"].outcome_status == "deleted"
        assert sorted(adapter.calls) == ["a1", "p1"]  # both attempted
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='a1'").fetchone()
            p = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='p1'").fetchone()
            assert a["deletion_status"] == "deletion_failed"
            assert p["deletion_status"] == "deleted"
        finally:
            conn.close()

    def test_busy_claim_continues_to_next_entry(self, tmp_path):
        # T8 — a resource held by another worker (busy) yields no outcome and
        # no adapter call, but the next entry still advances.
        td = _db()
        self._tail_op(td)
        # Stamp the audio as cleanup_required + deletion_pending under another
        # owner's active lease (a valid mid-delete state).
        conn = _fresh_conn(td)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "UPDATE heygen_asset_uploads SET status='cleanup_required',"
                " lease_owner='other', lease_expires_at='2026-08-01T00:00:00Z',"
                " lease_fence=1, attempt_started_at=? WHERE upload_id='u_audio'",
                (NOW,))
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_status='deletion_pending',"
                " deletion_reason='post_download', deletion_attempts=1 "
                "WHERE remote_id='a1'")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        by_kind = {a.entry.resource_kind: a for a in res.attempts}
        assert by_kind["audio_asset"].claim_status == "busy"
        assert by_kind["audio_asset"].outcome_status is None
        assert by_kind["portrait_asset"].outcome_status == "deleted"
        assert adapter.calls == ["p1"]  # audio NOT called; portrait was

    def test_untyped_exception_alerts_and_writes_no_phantom_outcome(self, tmp_path):
        # T9 — a non-AssetReadError raise means the remote result is unknowable:
        # the coordinator records an alert, writes NO outcome, and continues.
        td = _db()
        self._tail_op(td)
        adapter = _ScriptedAdapter({
            "a1": _Boom("transport died"),
            "p1": AssetDeleteResult("deleted"),
        })
        res = _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=_StubDeleter(),
            adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        by_kind = {a.entry.resource_kind: a for a in res.attempts}
        assert by_kind["audio_asset"].routed == "alerted_exception"
        assert by_kind["audio_asset"].claim_status is None
        assert by_kind["audio_asset"].outcome_status is None
        assert by_kind["portrait_asset"].outcome_status == "deleted"  # continued
        assert res.alerted == 1
        # CRITICAL: no phantom outcome for the stuck resource. The claim's
        # legitimate flip to deletion_pending stands, but NO apply ran — so it
        # is neither deleted nor deletion_failed, carries no error, no retry,
        # no deleted_at. The held lease expires naturally.
        conn = _fresh_conn(td)
        try:
            r = conn.execute(
                "SELECT deletion_status, deleted_at, last_deletion_error,"
                " deletion_next_retry_at FROM heygen_remote_resources "
                "WHERE remote_id='a1'").fetchone()
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='u_audio'").fetchone()
            assert r["deletion_status"] == "deletion_pending"
            assert r["deleted_at"] is None
            assert r["last_deletion_error"] is None
            assert r["deletion_next_retry_at"] is None
            assert a["status"] == "cleanup_required"  # claim wrote it; not deleted
        finally:
            conn.close()


# ===========================================================================
# Force authority guard (defense in depth on top of the resolver's guard)
# ===========================================================================

class TestDeletePassForceGuard:
    def test_force_string_false_rejected_before_anything_runs(self, tmp_path):
        # T10 — a truthy non-bool cannot reach the force branch.
        td = _db()
        _setup_full_op(td)
        deleter = _StubDeleter()
        adapter = _FakeAdapter()
        with pytest.raises(ValueError):
            _coord(td).delete_pass_for_operation(
                operation_id="op1", force="false", deleter=deleter,
                adapter=adapter, lease_owner=OWNER, now_iso=NOW,
                lease_seconds=LEASE_SECONDS)
        assert deleter.calls == [] and adapter.calls == []

    @pytest.mark.parametrize("bad", [1, None, [], 0, 1.0, "true"])
    def test_force_wrong_type_rejected(self, tmp_path, bad):
        # T11 — int 0 is rejected too (type() is bool, not truthiness).
        td = _db()
        _setup_full_op(td)
        with pytest.raises(ValueError):
            _coord(td).delete_pass_for_operation(
                operation_id="op1", force=bad, deleter=_StubDeleter(),
                adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
                lease_seconds=LEASE_SECONDS)

    def test_force_tamper_does_not_release_video(self, tmp_path):
        # T12 — with a non-deleted video present, force='false' must be rejected
        # BEFORE routing, so the video is never deleted this call.
        td = _db()
        vrid = _setup_full_op(td)
        with pytest.raises(ValueError):
            _coord(td).delete_pass_for_operation(
                operation_id="op1", force="false", deleter=_StubDeleter(),
                adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
                lease_seconds=LEASE_SECONDS)
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE resource_id=?", (vrid,)).fetchone()
            assert v["deletion_status"] == "not_started"
        finally:
            conn.close()


# ===========================================================================
# Transaction isolation + identity binding invariants
# ===========================================================================

def _patch_counting_begin_immediate(monkeypatch):
    """Wrap OperationRepository.begin_immediate to count currently-open txs.
    Returns a dict ref the spy fakes read at adapter-call time."""
    import lecturecast.operation_repository as mod
    orig = mod.OperationRepository.begin_immediate
    state = {"open": 0}

    @contextmanager
    def counting(self):
        state["open"] += 1
        try:
            with orig(self) as conn:
                yield conn
        finally:
            state["open"] -= 1

    monkeypatch.setattr(mod.OperationRepository, "begin_immediate", counting)
    return state


class _ProbeDeleter:
    def __init__(self, state, result=None):
        self.state = state
        self.result = result if result is not None else DeleteResult("deleted")
        self.snapshots = []

    def delete_video(self, remote_id):
        self.snapshots.append(self.state["open"])
        return self.result


class _ProbeAdapter:
    def __init__(self, state, result=None):
        self.state = state
        self.result = result if result is not None else AssetDeleteResult("deleted")
        self.snapshots = []

    def delete_asset(self, asset_id):
        self.snapshots.append(self.state["open"])
        return self.result


class TestDeletePassTxIsolation:
    def test_no_coordinator_tx_open_during_video_deleter_call(self, tmp_path, monkeypatch):
        # T13 — the plan-resolution tx is closed before any processor runs; the
        # processor's claim tx is committed+closed before the deleter call.
        td = _db()
        _setup_full_op(td)
        state = _patch_counting_begin_immediate(monkeypatch)
        deleter = _ProbeDeleter(state)
        _coord(td).delete_pass_for_operation(
            operation_id="op1", force=False, deleter=deleter,
            adapter=_ProbeAdapter(state), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert deleter.snapshots == [0]

    def test_no_coordinator_tx_open_during_asset_adapter_call(self, tmp_path, monkeypatch):
        # T13 — same invariant on the asset path (force releases the tail).
        td = _db()
        _setup_full_op(td)
        state = _patch_counting_begin_immediate(monkeypatch)
        adapter = _ProbeAdapter(state)
        _coord(td).delete_pass_for_operation(
            operation_id="op1", force=True, deleter=_ProbeDeleter(state),
            adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert adapter.snapshots == [0, 0]

    def test_plan_resolved_exactly_once_per_pass(self, tmp_path, monkeypatch):
        # T14 — resolve runs once per pass regardless of entry count.
        td = _db()
        _setup_full_op(td)
        calls = {"n": 0}
        orig = OperationRepository.resolve_deletion_plan_in_tx

        def counting(self, conn, *, operation_id, force=False):
            calls["n"] += 1
            return orig(self, conn, operation_id=operation_id, force=force)

        monkeypatch.setattr(OperationRepository, "resolve_deletion_plan_in_tx",
                            counting)
        _coord(td).delete_pass_for_operation(
            operation_id="op1", force=True, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert calls["n"] == 1


class TestDeletePassIdentityBinding:
    def _bind_spies(self, td):
        """Wrap the coordinator's processors to capture per-call kwargs."""
        coord = _coord(td)
        seen = {"lease_owners": [], "now_isos": []}
        v_orig = coord._video_processor.delete_once
        a_orig = coord._asset_processor.delete_once

        def v_wrap(**kw):
            seen["lease_owners"].append(kw["lease_owner"])
            seen["now_isos"].append(kw["now_iso"])
            return v_orig(**kw)

        def a_wrap(**kw):
            seen["lease_owners"].append(kw["lease_owner"])
            seen["now_isos"].append(kw["now_iso"])
            return a_orig(**kw)

        coord._video_processor.delete_once = v_wrap
        coord._asset_processor.delete_once = a_wrap
        return coord, seen

    def test_same_lease_owner_forwarded_to_every_call(self, tmp_path):
        # T15 / R9 — one lease_owner per pass, no per-item mutation.
        td = _db()
        _setup_full_op(td)
        coord, seen = self._bind_spies(td)
        coord.delete_pass_for_operation(
            operation_id="op1", force=True, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert seen["lease_owners"] == [OWNER, OWNER]  # audio + portrait

    def test_same_now_iso_forwarded_to_every_call(self, tmp_path):
        # T16 / R10 — one now_iso per pass, no per-item clock drift.
        td = _db()
        _setup_full_op(td)
        coord, seen = self._bind_spies(td)
        coord.delete_pass_for_operation(
            operation_id="op1", force=True, deleter=_StubDeleter(),
            adapter=_FakeAdapter(), lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert seen["now_isos"] == [NOW, NOW]


# ===========================================================================
# Maintenance sweep (recover_deletions)
# ===========================================================================

class TestRecoverDeletions:
    def test_drives_only_candidates_and_aggregates(self, tmp_path):
        # T17 — op_A (post-download video, deletable), op_B (video already
        # deleted → audio tail cleanup is authorized), op_C (fully deleted →
        # not a candidate). The sweep drives A+B and skips C.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opA", download_status="verified")
            _add_resource(conn, op_id="opA", kind="video", remote_id="vA")
            _add_op(conn, "opB", download_status="verified")
            _add_resource(conn, op_id="opB", kind="video", remote_id="vB",
                          ds="deleted", reason="post_download")  # honest lifecycle
            _add_asset(conn, op_id="opB", role="synthetic_narration_audio",
                       upload_id="uB", remote_id="aB")
            _add_op(conn, "opC", download_status="verified")
            _add_resource(conn, op_id="opC", kind="video", remote_id="vC",
                          ds="deleted", reason="post_download")  # fully deleted
            conn.commit()
        finally:
            conn.close()
        deleter = _StubDeleter()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=deleter, adapter=adapter, lease_owner=OWNER, now_iso=NOW,
            lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 2
        assert agg["ops_empty"] == 0
        assert agg["deleted"] == 2
        assert deleter.calls == ["vA"]   # op_A video (op_B video already deleted)
        assert adapter.calls == ["aB"]   # op_B audio tail
        # op_C never touched.
        conn = _fresh_conn(td)
        try:
            c = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='vC'").fetchone()
            assert c["deletion_status"] == "deleted"
        finally:
            conn.close()

    def test_inflight_asset_only_op_not_swept_by_default(self, tmp_path):
        # Codex round-1 blocker regression: an op that is still in flight
        # (submit_pending, assets uploaded at not_started, NO video resource)
        # must NOT be swept by the default pass — those assets are still in
        # production use and the resolver would release them ungated (no video
        # to hold them behind).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='aLive'").fetchone()
            assert a["status"] == "uploaded"          # untouched
            assert r["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_force_sweep_does_delete_inflight_asset_only_op(self, tmp_path):
        # The mirror of the blocker: an explicit force sweep IS authorized to
        # delete the in-flight asset (operator force-cleanup). This keeps the
        # broad candidate set for force while the default sweep stays gated.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS, force=True)
        assert agg["ops_driven"] == 1
        assert adapter.calls == ["aLive"]
        conn = _fresh_conn(td)
        try:
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='aLive'").fetchone()
            assert r["deletion_status"] == "deleted"
        finally:
            conn.close()

    def test_reusable_video_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-2 P1 regression: a reusable_avatar video must NOT act as
        # the "has video" authorization witness for the default sweep. The outer
        # candidate filter already excludes reusable rows, but the EXISTS
        # witness r2 lacked the same retention filter — so an in-flight op with
        # a reusable video + an ephemeral asset in production use was swept;
        # the resolver skips the reusable video and releases the tail, deleting
        # the in-use asset. The witness must carry the same retention gate.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vKeep",
                          retention="reusable_avatar")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='aLive'").fetchone()
            assert a["status"] == "uploaded"          # untouched
            assert r["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_reusable_pending_resource_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-2 P1 mirror: a reusable resource sitting in
        # deletion_pending must NOT act as the "in deletion pipeline" witness
        # either. Same retention bypass on the r2.deletion_status branch — a
        # reusable pending resource says nothing about whether sibling ephemeral
        # assets should be deleted, so it must not authorize their sweep.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_resource(conn, op_id="opLive", kind="portrait_asset",
                          remote_id="pKeep", retention="reusable_avatar",
                          ds="deletion_pending")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='aLive'").fetchone()
            assert a["status"] == "uploaded"          # untouched
            assert r["deletion_status"] == "not_started"
        finally:
            conn.close()

    def test_manual_force_pending_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-3 P1 regression: a manual_force resource (c1 locked
        # semantic: NEVER auto-deleted) must NOT act as the "in deletion
        # pipeline" authorization witness for the default sweep. The witness
        # checked only deletion_status ∈ {pending, failed} with no deletion_reason
        # gate — so an in-flight op with a manual_force asset + a sibling
        # uploaded/not_started asset was swept; the manual_force asset's own
        # claim correctly returns not_ready, but the resolver releases the tail
        # and the sibling's asset claim (no download_status gate) deletes it.
        # The witness must be restricted to auto-recoverable reasons
        # (post_download / consent_withdrawal); manual_force is operator-only.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_manual_asset(conn, op_id="opLive",
                              role="synthetic_narration_audio",
                              upload_id="uManual", remote_id="aManual",
                              ds="deletion_pending")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            for uid in ("uManual", "uLive"):
                a = conn.execute(
                    "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                    (uid,)).fetchone()
                assert a["status"] != "deleted"      # neither asset touched
        finally:
            conn.close()

    def test_manual_force_failed_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-3 P1 mirror: the deletion_failed branch of the witness
        # must also exclude manual_force. A manual_force resource in
        # deletion_failed is the same operator-only integrity path and must not
        # authorize auto-sweeping a sibling.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_manual_asset(conn, op_id="opLive",
                              role="synthetic_narration_audio",
                              upload_id="uManual", remote_id="aManual",
                              ds="deletion_failed")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            for uid in ("uManual", "uLive"):
                a = conn.execute(
                    "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
                    (uid,)).fetchone()
                assert a["status"] != "deleted"      # neither asset touched
        finally:
            conn.close()

    def test_deleted_manual_force_video_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-4 P1 regression: a schema-legal deleted/manual_force VIDEO
        # must not authorize the default sweep. The video branch of the witness
        # carried no reason gate, so a deleted/manual_force video (unreachable
        # via the current producer, but schema-legal) acted as a witness; the
        # resolver skips a deleted video and releases the tail, deleting the
        # sibling asset. The whole deletion subsystem fails closed against
        # schema-legal anomalous states (topology/matrix/retention all do) —
        # "the producer never makes one" is NOT a fail-closed boundary. The
        # witness must exclude manual_force in EVERY branch via a common reason
        # gate (reason IS NULL OR reason IN auto-recoverable).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vKeep",
                          retention="ephemeral", ds="deleted",
                          reason="manual_force")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_null_reason_video_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-5 P1 regression: a schema-legal deleted/NULL-reason VIDEO
        # must not authorize the default sweep. The round-4 COMMON reason gate
        # (reason IS NULL OR reason IN auto-recoverable) admitted ANY NULL-reason
        # resource as a witness — including a deleted/NULL video, which the
        # resolver skips (releasing the tail) exactly like a deleted/manual_force
        # video. The only legit NULL-reason witness is a not_started video
        # (in-flight, never claimed → no reason set); a deleted video must carry
        # a non-NULL auto-recoverable reason (video apply inherits the claim's
        # reason; claim from not_started always sets post_download). So the
        # witness must be gated on the full (status, reason) STATE MATRIX —
        # Option B — not on reason alone. Same fail-closed-against-schema-legal-
        # anomalous-states threat model as round-4 (corrupt/直插 deleted+NULL,
        # not producer-reachable, still in-model).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # in flight
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vKeep",
                          retention="ephemeral", ds="deleted")  # NULL reason
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_video_missing_ref_does_not_authorize_default_sweep(self, tmp_path):
        # Codex round-6 P1 regression (topology dimension): a schema-legal
        # deleted/post_download video that LACKS the operation's exclusive ref
        # still passes the (status,reason) matrix witness — it matched only on
        # created_by_operation_id. Because the resolver skips a deleted video
        # and the video claim therefore never re-runs to re-verify the exclusive-
        # ref topology, this broken-topology deleted video falsely authorizes
        # the op, releasing the tail and deleting the sibling asset. The witness
        # must self-verify the SAME exclusive-ref topology the claim enforces
        # (own ref + no foreign ref + credential match).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download")
            # strip the ref _add_resource created → topology-invalid
            conn.execute(
                "DELETE FROM heygen_resource_operation_refs WHERE resource_id IN "
                "(SELECT resource_id FROM heygen_remote_resources WHERE remote_id='vDel')")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_video_foreign_ref_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 topology mirror: a deleted video carrying a FOREIGN ref (claims
        # to also belong to another op) must not authorize the sweep. The video
        # claim raises OperationIntegrityError on a foreign ref, but a deleted
        # video skips the claim — so the witness must enforce no-foreign-ref.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_op(conn, "opOther", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download")
            rid = conn.execute(
                "SELECT resource_id FROM heygen_remote_resources WHERE remote_id='vDel'"
                ).fetchone()[0]
            conn.execute(
                "INSERT INTO heygen_resource_operation_refs "
                "(resource_id, operation_id, created_at) VALUES (?,?,?)",
                (rid, "opOther", "t"))
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []

    def test_deleted_video_credential_mismatch_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 topology mirror: a deleted video whose credential_profile_id
        # does NOT match its op's credential must not authorize. The video claim
        # raises on credential mismatch; a deleted video skips the claim.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified",
                    credential="heygen_env_default")
            _add_op(conn, "opOther", download_status="verified",
                    credential="heygen_other")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download",
                          credential="heygen_other")  # mismatched credential
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []

    def test_bare_pending_asset_no_upload_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 asset-binding dimension: a pending/post_download audio resource
        # with NO heygen_asset_uploads row (a bare resource — schema-legal) still
        # passes the witness (it only needs pending/failed + auto-recoverable
        # reason). It authorizes the op; the resolver scopes the sibling asset
        # (no non-deleted video to gate the tail) and the asset claim's
        # not_started branch does not re-gate on download_status → sibling
        # deleted. The asset witness must be BOUND to this op's asset upload.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            # bare pending/pd audio resource — NO upload row
            _add_resource(conn, op_id="opLive", kind="audio_asset", remote_id="aBare",
                          retention="ephemeral", ds="deletion_pending",
                          reason="post_download")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_video_valid_topology_unverified_op_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 download_status dimension (a dimension Codex round-6 did NOT
        # flag — found by empirical enumeration). A deleted/post_download video
        # with FULLY VALID topology (own ref + no foreign + matching credential)
        # on an UNVERIFIED op (download_status='not_started') still authorizes
        # tail release: the legit path to 'deleted' goes through the not_started
        # → deletion_pending claim, which gates on download_status='verified',
        # but a deleted video is never re-claimed so that gate is skipped. A
        # corrupt 直插 deleted video on an unverified op must not release the
        # tail. The deleted-video witness branch must additionally require the op
        # to be verified. (Asset/consent witnesses must NOT require verified —
        # consent_withdrawal cleanup is delivery-independent — so the gate is
        # specific to the deleted-video-tail path, not a blanket op filter.)
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # NOT verified
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download")
            # topology is valid (default _add_resource ref + matching credential)
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_video_active_lease_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 op-lease dimension (workflow-confirmed). The video claim gates
        # on the OP lease (active lease held by another owner → busy; half lease
        # → OperationIntegrityError), enforcing op-level mutual exclusion. But a
        # deleted-video witness is skipped by the resolver (never re-claimed), so
        # that gate is bypassed; the resolver then releases the tail (no
        # non-deleted video) and the asset claim checks only the ASSET's own
        # lease, never op.lease_* — so a sibling asset is deleted while another
        # worker actively holds the op. The deleted-video witness branch must
        # require the op to be clean-idle (mirror the video claim's lease gate).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            # Another worker actively holds the op lease (future expiry).
            conn.execute(
                "UPDATE heygen_operations SET lease_owner='other-worker', "
                "lease_expires_at='2026-08-15T00:00:00Z' "
                "WHERE operation_id='opLive'")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_avatar_look_pending_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 resource_kind dimension (workflow-confirmed). The witness
        # non-video kind clause admitted ANY non-video kind, but avatar_look /
        # avatar_group have NO deletion processor (coordinator routes them to
        # skipped_unknown_kind) — so a corrupt 直插 avatar_look row
        # (deletion_pending/post_download, wrong credential, no ref) acts as an
        # entirely unverified witness: it authorizes the op, the resolver
        # (no non-deleted video) releases the tail, and the sibling asset is
        # deleted. The tail-releasing witness branch must restrict to
        # audio_asset/portrait_asset (the kinds with real upload bindings).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="avatar_look", remote_id="al1",
                          retention="ephemeral", ds="deletion_pending",
                          reason="post_download", credential="WRONG_CRED")
            # strip the avatar's ref (corrupt topology); no upload can bind it
            conn.execute(
                "DELETE FROM heygen_resource_operation_refs WHERE resource_id IN "
                "(SELECT resource_id FROM heygen_remote_resources WHERE remote_id='al1')")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_double_deleted_video_does_not_authorize_default_sweep(self, tmp_path):
        # round-6 single-video-count dimension (workflow-confirmed). The video
        # claim enforces _single_video (COUNT(video refs)==1) on every
        # post_download path and apply re-checks it — so NO video can legit-
        # imately reach 'deleted' in a count>=2 op (claim returns not_ready,
        # apply returns fence_conflict). But the deleted-video witness has no
        # count check, so a 直插 op with TWO deleted/post_download videos (each
        # valid topology, verified op) is admitted; the resolver skips both
        # deleted videos → tail released → sibling asset deleted. The
        # deleted-video witness branch must require count(video)==1 (mirroring
        # the claim/apply gate) for post_download.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="v1",
                          retention="ephemeral", ds="deleted", reason="post_download")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="v2",
                          retention="ephemeral", ds="deleted", reason="post_download")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_pending_asset_matrix_mismatch_does_not_authorize_default_sweep(self, tmp_path):
        # round-7 P1 regression (B2 asset-binding MATRIX dimension). A
        # deletion_pending/post_download asset witness whose upload is matrix-
        # INCONSISTENT (uploaded, not cleanup_required — _check_asset_resource_
        # consistency would raise) still passed the round-6 "upload EXISTS"
        # witness: it authorized the op, the resolver released the sibling tail,
        # the witness's own claim alerted on the matrix mismatch, and the
        # coordinator's dumb iterator deleted the legit sibling. The asset
        # witness must mirror the SAME asset<->resource matrix the claim enforces
        # (deletion_pending <-> upload cleanup_required), not merely "an upload
        # row exists". Same fail-closed-against-schema-legal-anomalous-states
        # threat model as round-4/5/6 (corrupt 直插 uploaded+pending pair, not
        # producer-reachable, still in-model).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            # deletion_pending resource paired with an UPLOADED upload (the
            # claim would raise on this matrix mismatch).
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="uploaded")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_pending_asset_role_kind_mismatch_does_not_authorize_default_sweep(self, tmp_path):
        # round-7 P1 regression (B2 asset-binding ROLE<->KIND dimension). A
        # deletion_pending/post_download audio_asset resource with a cleanup_
        # required upload whose asset_role is portrait_photo (role->kind mismatch
        # — _validate_asset_binding maps portrait_photo to portrait_asset, not
        # audio_asset) still passed the round-6 witness: the upload EXISTS and is
        # cleanup_required, but the role does not match the resource kind. The
        # witness's own claim raises on the role-kind mismatch; the coordinator
        # continues and deletes the legit sibling. The asset witness must mirror
        # the role<->kind pair the claim's _validate_asset_binding enforces.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            rid = _add_resource(conn, op_id="opLive", kind="audio_asset",
                                remote_id="aBad", retention="ephemeral",
                                ds="deletion_pending", reason="post_download")
            # upload claims portrait_photo role but is bound to an audio_asset.
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
                " asset_role, content_digest, local_ref, content_type, size_bytes,"
                " provider_filename, idempotency_key, remote_resource_id, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("uBad", "opLive", "portrait_photo", "sha256:aBad", "loc",
                 "application/octet-stream", 1, "aBad.bin", "idem-uBad", rid,
                 "cleanup_required", "t", "t"))
            # sibling is a LEGIT audio (synthetic_narration_audio / audio_asset).
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uLive", remote_id="aLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_deleted_video_without_terminal_proof_does_not_authorize_default_sweep(self, tmp_path):
        # round-7 P1 regression (B1 apply-TERMINAL-PROOF dimension). A successful
        # video delete via apply_deletion_outcome_in_tx ALWAYS writes deleted_at
        # NOT NULL + deletion_attempts>=1 (the claim bumps it before apply) +
        # deletion_next_retry_at IS NULL + last_deletion_error IS NULL. A 直插
        # 'deleted'/post_download video with deleted_at=NULL, deletion_attempts=0
        # is schema-legal but unreachable via apply; the round-6 witness admitted
        # it (it only checked status/reason/topology/count/download_status/lease)
        # and released the tail. The deleted-video witness branch must require
        # the apply terminal proof, mirroring the only state the live apply path
        # can actually produce.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="vDel",
                          retention="ephemeral", ds="deleted", reason="post_download")
            # strip the terminal proof _add_resource now sets — model the anomaly.
            conn.execute(
                "UPDATE heygen_remote_resources SET deleted_at=NULL,"
                " deletion_attempts=0 WHERE remote_id='vDel'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched
        finally:
            conn.close()

    def test_b2_post_download_asset_witness_on_unverified_op_does_not_authorize_default_sweep(self, tmp_path):
        # round-8 P1 regression (B2 download_status mirror — the 8th class an
        # independent workflow audit caught after round-7). The VIDEO claim gates
        # post_download on op.download_status='verified'; the ASSET claim does NOT
        # (claim_asset_deletion_in_tx reads only credential_profile_id from the
        # op), and the resolver only carries download_status as informational
        # context. B1 (deleted-video witness) mirrors download_status; B2 (non-
        # video asset witness) did not. So a B2-only op (no live/verified video)
        # had NO layer enforcing "delivery verified before asset cleanup": a 直插
        # deletion_pending/post_download asset witness with a fully matrix-
        # consistent, role-kind-paired cleanup_required upload authorized the op,
        # the resolver released the tail, and the asset claim deleted a pre-delivery
        # sibling portrait (still not_started/uploaded) without ever checking
        # download_status. The fix mirrors B1's clause into B2. (A-vs-B probe
        # isolates download_status: the IDENTICAL unverified op + sibling under a
        # B1 deleted-video witness was already correctly blocked — only B2 admitted
        # it.) Same schema-legal-anomalous-state threat model as round-4/5/6/7.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")  # ANOMALOUS
            # fully legit B2 witness: pending/post_download + cleanup_required
            # upload + role-kind pair + matrix all consistent.
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # pre-delivery sibling untouched
        finally:
            conn.close()

    def test_b2_post_download_asset_witness_on_verified_op_authorizes_default_sweep(self, tmp_path):
        # round-8 control: the fix must NOT over-block. The identical B2 witness on
        # a download-VERIFIED op IS a legit post-delivery asset cleanup and must
        # still authorize the sweep (the delivery happened, the assets are now
        # expendable). This is the (C) arm of the probe.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 1               # legit post-delivery cleanup
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] == "deleted"          # sibling cleaned (delivery done)
        finally:
            conn.close()

    def test_b2_consent_asset_witness_on_unverified_op_authorizes_default_sweep(self, tmp_path):
        # round-8 control: consent_withdrawal cleanup is delivery-INDEPENDENT (a
        # hard constraint since round-4). A B2 consent_withdrawal asset witness on
        # an UNVERIFIED op must STILL authorize the sweep — the download_status
        # mirror's `!= 'post_download'` short-circuit keeps consent exempt. This is
        # the (L) arm of the probe and guards against the fix regressing consent.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="not_started")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='consent_withdrawal' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 1               # consent cleanup is delivery-free
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] == "deleted"          # sibling cleaned (consent)
        finally:
            conn.close()

    def test_b2_post_download_asset_witness_on_double_video_op_does_not_authorize_default_sweep(self, tmp_path):
        # round-9 P1 regression (B2 single-video mirror — the 9th class a second
        # independent workflow re-audit caught after round-8). The VIDEO claim's
        # _single_video gate is an OP-LEVEL invariant (resolver contract "at
        # most one video per op"), enforced ONLY on the live-video path; the
        # ASSET claim reads zero video count and the resolver only comments on
        # it (trusting the video claim to fail-closed on doubles — a trust
        # broken once the videos are already deleted/skipped). B1 (deleted-
        # video witness) mirrors single-video; B2 (non-video asset witness)
        # did not. So a 直插 double-video op (COUNT==2, both deleted/
        # post_download with valid terminal proof) admitted a
        # deletion_pending/post_download asset witness: B1 refused (COUNT==1
        # fails its gate), B2 authorized, the resolver skipped both deleted
        # videos -> released the tail -> the coordinator swept individually-
        # eligible assets (the witness audio AND an innocent not_started/
        # uploaded portrait sibling) on a structurally-corrupt op that B1's
        # gate exists to freeze for human reconciliation. Same B1↔B2 asymmetry
        # class as round-8's download_status (A-vs-B probe isolates the branch:
        # the IDENTICAL double-video op + sibling is SAFE under B1-only).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            # TWO 直插 deleted/post_download videos (valid terminal proof) —
            # the schema-legal corruption B1's count gate refuses.
            _add_resource(conn, op_id="opLive", kind="video",
                          remote_id="v1", ds="deleted", reason="post_download")
            _add_resource(conn, op_id="opLive", kind="video",
                          remote_id="v2", ds="deleted", reason="post_download")
            # fully legit B2 witness: pending/post_download + cleanup_required
            # upload + role-kind pair + matrix all consistent.
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 0
        assert adapter.calls == []
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] != "deleted"          # sibling untouched (op frozen)
        finally:
            conn.close()

    def test_b2_post_download_asset_witness_on_single_video_op_authorizes_default_sweep(self, tmp_path):
        # round-9 field control: the count mirror must NOT over-block legit
        # single-video cleanup. The IDENTICAL setup but with exactly ONE deleted
        # video (COUNT==1) is a legit post-delivery op whose pending audio +
        # not_started portrait must still be swept. This is the (C) arm of the
        # probe and guards against the mirror regressing the normal path.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video",
                          remote_id="v1", ds="deleted", reason="post_download")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 1               # legit single-video cleanup
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] == "deleted"          # sibling cleaned
        finally:
            conn.close()

    def test_b2_consent_asset_witness_on_double_video_op_authorizes_default_sweep(self, tmp_path):
        # round-9 consent control: consent_withdrawal cleanup is delivery- AND
        # structure-independent (a hard constraint since round-4, mirrored in
        # round-8's download_status exemption). A B2 consent_withdrawal asset
        # witness on a DOUBLE-VIDEO op (B1 blocked on count) must STILL
        # authorize the sweep — the single-video mirror's `!= 'post_download'`
        # short-circuit keeps consent exempt. Guards against the count mirror
        # regressing consent cleanup on structurally-anomalous ops.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video",
                          remote_id="v1", ds="deleted", reason="post_download")
            _add_resource(conn, op_id="opLive", kind="video",
                          remote_id="v2", ds="deleted", reason="post_download")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uBad", remote_id="aBad",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='consent_withdrawal' "
                "WHERE remote_id='aBad'")
            _add_asset(conn, op_id="opLive", role="portrait_photo",
                       upload_id="uLive", remote_id="pLive")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 1               # consent cleanup is structure-free
        conn = _fresh_conn(td)
        try:
            a = conn.execute(
                "SELECT status FROM heygen_asset_uploads WHERE upload_id='uLive'").fetchone()
            assert a["status"] == "deleted"          # sibling cleaned (consent)
        finally:
            conn.close()

    def test_not_started_manual_force_video_not_auto_deleted_via_b2_witness(self, tmp_path):
        # T17v / round-10 bypass: claim_deletion_in_tx's not_started branch was
        # reason-blind (unlike its deletion_pending/deletion_failed siblings and
        # the asset claim's not_started branch, which all reject manual_force).
        # A schema-legal (not_started, manual_force) VIDEO, driven into the sweep
        # by a sibling B2 asset witness on a verified op (the witness authorizes
        # the op; the resolver returns the not_started video as the tail gate),
        # was claimed and had its marker erased to post_download at the
        # reason-seeding line (op_repository.py:1839), then deleted by apply's
        # post_download single-video recheck — violating "manual_force never
        # auto-deleted". After round-10 the not_started branch gates manual_force
        # -> not_ready, so the operator-only video survives untouched.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="v1",
                          ds="not_started", reason="manual_force")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uAud", remote_id="a1",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='a1'")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        # The op IS authorized (B2 audio witness) and driven, but the
        # manual_force video must NOT be deleted.
        assert agg["deleted"] == 0
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE remote_id='v1'").fetchone()
            assert v["deletion_status"] == "not_started"   # survived
            assert v["deletion_reason"] == "manual_force"  # marker intact
            a = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='a1'").fetchone()
            assert a["deletion_status"] == "deletion_pending"  # behind tail, untouched
        finally:
            conn.close()

    def test_deletion_pending_manual_force_video_survives_via_b2_witness(self, tmp_path):
        # T17v-ctrl-pending / round-10 control isolating the not_started branch:
        # identical to T17v but the video is (deletion_pending, manual_force).
        # The deletion_pending branch already gates manual_force -> not_ready, so
        # this passes BOTH before and after the round-10 fix. It proves the bypass
        # was specific to the not_started branch (the one field difference from
        # T17v) and guards the pending branch's manual_force gate from regressing.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="v1",
                          ds="deletion_pending", reason="manual_force")
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uAud", remote_id="a1",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='a1'")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["deleted"] == 0
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE remote_id='v1'").fetchone()
            assert v["deletion_status"] == "deletion_pending"
            assert v["deletion_reason"] == "manual_force"
        finally:
            conn.close()

    def test_not_started_null_reason_video_deleted_via_b2_witness(self, tmp_path):
        # T17v-ctrl-legit / round-10 control: identical to T17v but the video
        # carries the normal NULL reason (the fresh post-download entry). The
        # not_started branch must STILL legitimately delete it — round-10's
        # manual_force gate must not regress the normal post-download path.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opLive", download_status="verified")
            _add_resource(conn, op_id="opLive", kind="video", remote_id="v1",
                          ds="not_started", reason=None)
            _add_asset(conn, op_id="opLive", role="synthetic_narration_audio",
                       upload_id="uAud", remote_id="a1",
                       ds="deletion_pending", asset_status="cleanup_required")
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='post_download' "
                "WHERE remote_id='a1'")
            conn.commit()
        finally:
            conn.close()
        adapter = _FakeAdapter()
        agg = _coord(td).recover_deletions(
            deleter=_StubDeleter(), adapter=adapter, lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["deleted"] == 1                       # legit post-download delete
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE remote_id='v1'").fetchone()
            assert v["deletion_status"] == "deleted"
            assert v["deletion_reason"] == "post_download"
        finally:
            conn.close()

    def test_apply_rejects_reason_swap_to_manual_force_between_claim_and_apply(self, tmp_path):
        # T17w / round-11 bypass: the fenced claim and fenced apply are SEPARATE
        # transactions (the adapter call runs outside any tx between them).
        # claim_deletion_in_tx seeds deletion_reason='post_download' and flips a
        # not_started/NULL video to deletion_pending/post_download (tx1, then
        # COMMITted). Between tx1 and the fenced apply (tx2), a schema-legal
        # UPDATE flips deletion_reason='manual_force' (status stays
        # deletion_pending). Before round-11, video apply
        # (apply_deletion_outcome_in_tx) re-checked single-video ONLY when the
        # current reason == 'post_download' (so the recheck was skipped once the
        # reason flipped), and the gated success UPDATE neither read nor wrote
        # deletion_reason — so the mutated manual_force marker persisted and the
        # row became deleted/manual_force, violating "manual_force never
        # auto-deleted". Asset apply (:2206) was already defended; video apply
        # was not. The seam is driven DIRECTLY (not via recover_deletions)
        # because the mutation must land between the claim tx and the apply tx.
        td = _db()
        rid = _setup_full_op(td, video_ds="not_started")  # verified op + v1 not_started/NULL
        repo = OperationRepository(td)
        with repo.begin_immediate() as conn:
            claim = repo.claim_deletion_in_tx(
                conn, "op1", rid, OWNER, NOW, LEASE_SECONDS)
        assert claim.status == "claimed"
        # Claim seeded deletion_pending/post_download in the (now closed) tx1.
        conn = _fresh_conn(td)
        try:
            pre = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (rid,)).fetchone()
            assert pre["deletion_status"] == "deletion_pending"
            assert pre["deletion_reason"] == "post_download"
        finally:
            conn.close()
        # TOCTOU seam: a SEPARATE connection flips only the reason marker while
        # the sweep sits between its claim tx and its apply tx.
        conn = _fresh_conn(td)
        try:
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='manual_force' "
                "WHERE resource_id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        with repo.begin_immediate() as conn:
            outcome = repo.apply_deletion_outcome_in_tx(
                conn, "op1", rid, OWNER, claim.fence, NOW, DeleteResult("deleted"))
        # The reason swap must be rejected BEFORE any outcome path — no deletion,
        # no lease clear.
        assert outcome.status == "fence_conflict"
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (rid,)).fetchone()
            assert v["deletion_status"] == "deletion_pending"  # survived
            assert v["deletion_reason"] == "manual_force"      # marker intact
            # The operation lease is preserved (apply returned before
            # _clear_operation_lease) — the sweep did not silently release it.
            op = conn.execute(
                "SELECT lease_owner, lease_expires_at FROM heygen_operations "
                "WHERE operation_id='op1'").fetchone()
            assert op["lease_owner"] == OWNER
            assert op["lease_expires_at"] is not None
        finally:
            conn.close()

    def test_apply_deletes_post_download_video_without_reason_swap(self, tmp_path):
        # T17w-ctrl-legit / round-11 control: identical to T17w but NO reason
        # swap between claim and apply. The legit post_download path must STILL
        # delete — round-11's reason gate must not regress the normal sweep.
        td = _db()
        rid = _setup_full_op(td, video_ds="not_started")
        repo = OperationRepository(td)
        with repo.begin_immediate() as conn:
            claim = repo.claim_deletion_in_tx(
                conn, "op1", rid, OWNER, NOW, LEASE_SECONDS)
        assert claim.status == "claimed"
        # No mutation between the two fenced txs.
        with repo.begin_immediate() as conn:
            outcome = repo.apply_deletion_outcome_in_tx(
                conn, "op1", rid, OWNER, claim.fence, NOW, DeleteResult("deleted"))
        assert outcome.status == "deleted"
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (rid,)).fetchone()
            assert v["deletion_status"] == "deleted"
            assert v["deletion_reason"] == "post_download"
        finally:
            conn.close()

    def test_apply_deletes_video_swapped_to_consent_withdrawal(self, tmp_path):
        # T17w-ctrl-consent / round-11 control: identical to T17w but the
        # between-tx swap is to consent_withdrawal, which is delivery-independent
        # and legitimately deletable. The round-11 gate accepts exactly
        # {post_download, consent_withdrawal}; this guards it does NOT over-block
        # by rejecting consent_withdrawal alongside manual_force.
        td = _db()
        rid = _setup_full_op(td, video_ds="not_started")
        repo = OperationRepository(td)
        with repo.begin_immediate() as conn:
            claim = repo.claim_deletion_in_tx(
                conn, "op1", rid, OWNER, NOW, LEASE_SECONDS)
        assert claim.status == "claimed"
        conn = _fresh_conn(td)
        try:
            conn.execute(
                "UPDATE heygen_remote_resources SET deletion_reason='consent_withdrawal' "
                "WHERE resource_id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        with repo.begin_immediate() as conn:
            outcome = repo.apply_deletion_outcome_in_tx(
                conn, "op1", rid, OWNER, claim.fence, NOW, DeleteResult("deleted"))
        assert outcome.status == "deleted"
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status, deletion_reason FROM heygen_remote_resources "
                "WHERE resource_id=?", (rid,)).fetchone()
            assert v["deletion_status"] == "deleted"
            assert v["deletion_reason"] == "consent_withdrawal"
        finally:
            conn.close()

    def test_candidate_tx_closed_before_any_pass_runs(self, tmp_path, monkeypatch):
        # T18 / R5 — the candidate-listing tx is closed before any network
        # deletion. The open-tx counter must read 0 at every deleter/adapter call.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opA", download_status="verified")
            _add_resource(conn, op_id="opA", kind="video", remote_id="vA")
            _add_op(conn, "opB", download_status="verified")
            _add_resource(conn, op_id="opB", kind="video", remote_id="vB")
            conn.commit()
        finally:
            conn.close()
        state = _patch_counting_begin_immediate(monkeypatch)
        deleter = _ProbeDeleter(state)
        _coord(td).recover_deletions(
            deleter=deleter, adapter=_ProbeAdapter(state), lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert deleter.snapshots == [0, 0]  # candidate tx closed before each

    def test_force_false_respects_verified_gate_sweep_wide(self, tmp_path):
        # T19 / R8 — default sweep never deletes a video whose op is not
        # verified; the claim returns not_ready.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opU", download_status="not_started")  # unverified
            _add_resource(conn, op_id="opU", kind="video", remote_id="vU")
            conn.commit()
        finally:
            conn.close()
        deleter = _StubDeleter()
        agg = _coord(td).recover_deletions(
            deleter=deleter, adapter=_FakeAdapter(), lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert deleter.calls == []  # never called — claim was not_ready
        assert agg["deleted"] == 0
        assert agg["attempted"] == 1  # the video WAS attempted (and refused)
        conn = _fresh_conn(td)
        try:
            v = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='vU'").fetchone()
            assert v["deletion_status"] == "not_started"  # not deleted
        finally:
            conn.close()

    def test_op_level_exception_does_not_abort_sweep(self, tmp_path):
        # T20 — a raise from delete_pass_for_operation for ONE op records an
        # alert and the sweep continues to later ops (earlier ops persist).
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            for op in ("opA", "opB", "opC"):
                _add_op(conn, op, download_status="verified")
                _add_resource(conn, op_id=op, kind="video", remote_id="v" + op[-1])
            conn.commit()
        finally:
            conn.close()
        coord = _coord(td)
        original = coord.delete_pass_for_operation

        def flaky(*, operation_id, **kw):
            if operation_id == "opB":
                raise RuntimeError("unexpected boom")
            return original(operation_id=operation_id, **kw)

        coord.delete_pass_for_operation = flaky
        agg = coord.recover_deletions(
            deleter=_StubDeleter(), adapter=_FakeAdapter(), lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert agg["ops_driven"] == 2      # opA + opC
        assert agg["ops_alerted"] == 1     # opB
        assert agg["deleted"] == 2

    def test_idempotent_rerun_no_hotloop(self, tmp_path):
        # T21 — re-running the sweep only re-attempts ops with surviving
        # non-deleted resources; nothing double-deletes, nothing errors.
        td = _db()
        conn = _fresh_conn(td)
        try:
            conn.execute("BEGIN")
            _add_op(conn, "opReady", download_status="verified")
            _add_resource(conn, op_id="opReady", kind="video", remote_id="vR")
            _add_op(conn, "opStuck", download_status="not_started")  # never verifies
            _add_resource(conn, op_id="opStuck", kind="video", remote_id="vS")
            conn.commit()
        finally:
            conn.close()
        coord = _coord(td)
        run1 = coord.recover_deletions(
            deleter=_StubDeleter(), adapter=_FakeAdapter(), lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        assert run1["deleted"] == 1          # opReady
        run2 = coord.recover_deletions(
            deleter=_StubDeleter(), adapter=_FakeAdapter(), lease_owner=OWNER,
            now_iso=NOW, lease_seconds=LEASE_SECONDS)
        # opReady fully deleted → no candidate resources → not driven.
        # opStuck still candidate → driven again → claim not_ready → no delete.
        assert run2["deleted"] == 0
        assert run2["ops_driven"] == 1       # opStuck only
        conn = _fresh_conn(td)
        try:
            r = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='vR'").fetchone()
            s = conn.execute(
                "SELECT deletion_status FROM heygen_remote_resources "
                "WHERE remote_id='vS'").fetchone()
            assert r["deletion_status"] == "deleted"     # stays deleted
            assert s["deletion_status"] == "not_started"  # still not_ready
        finally:
            conn.close()

