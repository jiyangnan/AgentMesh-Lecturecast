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
    DeletionCoordinator, OperationRepository)

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
    cur = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind,"
        " remote_id, retention_mode, created_by_operation_id, deletion_status,"
        " deletion_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (credential, kind, remote_id, retention, op_id, ds, reason, "t", "t"))
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
                          ds="deleted")  # video deleted → audio tail authorized
            _add_asset(conn, op_id="opB", role="synthetic_narration_audio",
                       upload_id="uB", remote_id="aB")
            _add_op(conn, "opC", download_status="verified")
            _add_resource(conn, op_id="opC", kind="video", remote_id="vC",
                          ds="deleted")  # fully deleted → not a candidate
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

