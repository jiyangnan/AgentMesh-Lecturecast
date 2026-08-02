"""§5.5e5d-c maintenance wiring tests (D-T10a/b/c, D-T11, D-T12, M1, M2, m4).

Covers the maintenance client recovery driver (``src/lecturecast/maintenance.py``)
+ its CLI leaf (``commands/maintenance.py``). The matrix mirrors the design-doc
§4.2 contract, strengthened by the 8-lens adversarial design audit (2026-08-02):

  D-T10a  DB-only pass runs to COMMITTED completion before the network pass is
          entered (call-order spy on empty journal + seeded committed-visibility
          proof — a fresh conn opened inside the recover_deletions spy already
          sees the cleanup_required row the DB pass wrote).
  D-T10b  dual adapters (deleter=HeyGenVideosAdapter + adapter=HeyGenAssetAdapter)
          built from ONE shared transport; BOTH passed to recover_deletions.
  D-T10c  whitespace/empty/unset HEYGEN_API_KEY fail-closed (audit B1): network
          pass skipped, NOT over-claimed; DB pass still runs.
  D-T11   non-bool force rejected at the LIB boundary before any DB read (m1) —
          proven key/journal-independent on a bare tmp_path.
  D-T12   ONLY the two locked entries are called (constraint d); NEVER
          adapter.delete_video / delete_asset / delete_pass_for_operation.
  M1      a non-current journal (fresh / parent_unwritable) skips WITHOUT calling
          init/recover, so the durable prior-use sentinel (.lecturecast/heygen.used)
          is never touched (which would later fail-close the capability probe).
  M2      exit code carries the recovery contract: 0 clean full sweep / 2 partial
          or skip / 1 reserved for harness exceptions (never reached — the lib
          wraps recover_deletions failures into a skip report, m3).
  m4      dual adapters are not SWAPPED (videos→deleter, asset→adapter).

All network is stubbed: recover_deletions is replaced by a spy returning a canned
8-key tally; the dual adapters are replaced by stub classes capturing the shared
transport. No real HeyGen call is ever made.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import lecturecast.maintenance as maintenance_mod
from lecturecast.cli import app
from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService,
    DisclosedAsset,
    HeyGenOperationIdentity,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.heygen_journal import init_database
from lecturecast.maintenance import MaintenanceReport, run_maintenance
from lecturecast.operation_repository import DeletionCoordinator, OperationRepository

NOW = "2026-08-02T00:00:00Z"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"
SENTINEL_REL = Path(".lecturecast") / "heygen.used"
D_PORT = "sha256:" + "a" * 64

# Canned recover_deletions tallies — mirror the 8-key dict shape the locked
# DeletionCoordinator.recover_deletions returns
# {ops_driven, ops_empty, ops_alerted, attempted, deleted, failed, skipped, alerted}.
_CLEAN_DEL_TALLY = {
    "ops_driven": 1, "ops_empty": 0, "ops_alerted": 0,
    "attempted": 1, "deleted": 1, "failed": 0, "skipped": 0, "alerted": 0,
}
_PARTIAL_DEL_TALLY = {
    "ops_driven": 1, "ops_empty": 0, "ops_alerted": 1,
    "attempted": 2, "deleted": 1, "failed": 1, "skipped": 0, "alerted": 1,
}
_EMPTY_DB_TALLY = {
    "cancelled": 0, "cleanup_required": 0, "manual": 0,
    "kept": 0, "left_uploading": 0,
}

# Codex round-2 — realistic tallies that ISOLATE each non-clean dimension the
# 8-key return can carry. ``attempted`` is always internally consistent with
# the per-dimension counts (the locked coordinator's invariant:
# attempted == deleted + failed + skipped + alerted + not_advanced), so these
# mirror states the coordinator can ACTUALLY produce (not impossible shapes).
_SKIPPED_DEL_TALLY = {
    # one candidate skipped (skipped_no_upload_id / skipped_unknown_kind), none deleted.
    "ops_driven": 1, "ops_empty": 0, "ops_alerted": 0,
    "attempted": 1, "deleted": 0, "failed": 0, "skipped": 1, "alerted": 0,
}
_BUSY_DEL_TALLY = {
    # one candidate stuck not_advanced (busy / retry_wait / not_ready / fence_conflict)
    # — the class recover_deletions does NOT aggregate into the 8-key return; it
    # is invisible except as attempted - (deleted+failed+skipped+alerted) > 0.
    "ops_driven": 1, "ops_empty": 0, "ops_alerted": 0,
    "attempted": 1, "deleted": 0, "failed": 0, "skipped": 0, "alerted": 0,
}
_MANUAL_DB_TALLY = {
    # DB-side pending work: an asset needs human reconciliation.
    "cancelled": 0, "cleanup_required": 0, "manual": 1,
    "kept": 0, "left_uploading": 0,
}
_LEFT_UPLOADING_DB_TALLY = {
    # DB-side pending work: an active upload lease was left intact (its fenced
    # apply will catch the withdraw on the next upload attempt).
    "cancelled": 0, "cleanup_required": 0, "manual": 0,
    "kept": 0, "left_uploading": 1,
}

_ASSET_CATEGORIES = {
    "portrait_photo": ["facial_biometric_template", "portrait_image"],
    "synthetic_narration_audio": ["synthetic_narration_audio"],
}


# --- DB setup helpers (mirror test_asset_journal / test_deletion_coordinator) -

def _current_project(tmp_path: Path) -> Path:
    """init_database → classification=='current' (the only class that proceeds)."""
    init_database(tmp_path)
    return tmp_path


def _fresh_conn(project: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(project / DB_REL))
    conn.row_factory = sqlite3.Row
    return conn


def _grant_parent(project: Path, assets=(("portrait_photo", D_PORT),)) -> str:
    """Canonical granted receipt via ConsentService.record_decision (correct
    digest), so the full receipt-integrity validator passes at the fenced
    enqueue. Mirrors test_asset_journal._grant_parent."""
    svc = ConsentService(project)
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
    svc.record_decision(prepared=prepared, disclosure=disclosure, decision="granted",
                        creative_brief_digest="sha256:" + "b" * 64,
                        decision_at="2026-07-29T00:00:00Z")
    return prepared.operation_id


def _withdraw_op(conn: sqlite3.Connection, op_id: str) -> None:
    """Simulate ConsentService.withdraw's DB effect: receipt → withdrawn (valid
    tz-aware withdrawn_at) + clear the op's consent pointer — the exact shape
    enqueue_consent_withdrawal_cleanup_in_tx requires to proceed."""
    conn.execute(
        "UPDATE heygen_consent_receipts SET status='withdrawn', "
        "withdrawn_at=? WHERE operation_id=?", (NOW, op_id))
    conn.execute(
        "UPDATE heygen_operations SET consent_receipt_digest=NULL "
        "WHERE operation_id=?", (op_id,))


def _add_uploaded_asset(conn: sqlite3.Connection, *, op_id: str,
                        role: str = "portrait_photo", remote_id: str = "p1") -> int:
    """Full uploaded asset: resource (portrait_asset, ephemeral, not_started) +
    upload (status='uploaded', remote_resource_id set). The post-download entry
    point that consent-withdrawal cleanup flips to cleanup_required."""
    kind = "portrait_asset" if role == "portrait_photo" else "audio_asset"
    rid = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind,"
        " remote_id, retention_mode, created_by_operation_id, deletion_status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("heygen_env_default", kind, remote_id, "ephemeral", op_id,
         "not_started", NOW, NOW)).lastrowid
    # The asset-binding topology check (op_repo:3187) requires exactly ONE ref
    # row pointing at the parent op, else OperationIntegrityError. Mirror
    # test_deletion_coordinator._add_resource.
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id,"
        " created_at) VALUES (?,?,?)", (rid, op_id, NOW))
    conn.execute(
        "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
        " asset_role, content_digest, local_ref, content_type, size_bytes,"
        " provider_filename, idempotency_key, remote_resource_id, status,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"u-{op_id}", op_id, role, D_PORT, "loc", "application/octet-stream",
         1, f"{remote_id}.bin", f"idem-{remote_id}", rid, "uploaded", NOW, NOW))
    return rid


class _SentinelTransport:
    """Identity-comparable transport stand-in. Exposes the ONE method
    run_maintenance reads (``_api_key_provider``) so the B1 gate proceeds; the
    adapters built around it are stubbed too, so no real network method is
    ever called."""

    def _api_key_provider(self):  # noqa: ANN001 - matches the real provider shape
        return "test-key"


def _spy_recover_deletions(monkeypatch, capture: dict, tally=None):
    """Replace DeletionCoordinator.recover_deletions with a capturing spy that
    returns a canned tally (no network). ``capture`` is populated with the
    kwargs (deleter/adapter/force/...) for assertions."""
    canned = dict(tally if tally is not None else _CLEAN_DEL_TALLY)

    def _spy(self, **kw):
        capture.update(kw)
        return dict(canned)

    monkeypatch.setattr(DeletionCoordinator, "recover_deletions", _spy)
    return _spy


# ===========================================================================
# D-T11 — non-bool force rejected at the lib boundary before any DB read (m1)
# ===========================================================================

@pytest.mark.parametrize("bad", ["false", "true", 1, 0, None, [], 1.0, "yes"])
def test_d_t11_force_non_bool_raises_before_any_db_read(tmp_path: Path, bad) -> None:
    """D-T11 / m1: a truthy-or-falsy non-bool force is rejected at the lib
    boundary BEFORE any DB read. Proven key/journal-independent by running on a
    BARE tmp_path (no .lecturecast/, no journal): the guard fires before
    _journal_state/init, so the outcome is deterministic regardless of journal
    or key state. ``type() is bool`` (not ``isinstance``) — int 1 / int 0 /
    ``True``-as-int would slip through ``isinstance(force, int)``."""
    with pytest.raises(ValueError, match="force must be a bool"):
        run_maintenance(tmp_path, now_iso=NOW, force=bad)
    # Nothing was created — the guard fired before _journal_state/init.
    assert not (tmp_path / DB_REL).exists()
    assert not (tmp_path / SENTINEL_REL).exists()


def test_d_t11_force_bool_passes_the_guard(tmp_path: Path, monkeypatch) -> None:
    """D-T11 boundary: bool False AND bool True both pass the m1 guard (the
    guard rejects only non-bool, not True). Verified by reaching the journal
    gate on a current project with the network stubbed."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    capture: dict = {}
    _spy_recover_deletions(monkeypatch, capture)
    for f in (False, True):
        capture.clear()
        report = run_maintenance(project, now_iso=NOW, force=f)
        assert report.force is f
        assert type(report.force) is bool
        assert capture["force"] is f


# ===========================================================================
# D-T12 — ONLY the two locked entries are called (constraint d, source-level)
# ===========================================================================

def test_d_t12_only_locked_entries_called_source_level() -> None:
    """D-T12 / constraint (d): run_maintenance calls ONLY the two locked
    entries — ``recover_withdrawn_asset_cleanups`` + ``recover_deletions``.
    It NEVER calls ``adapter.delete_video`` / ``adapter.delete_asset`` /
    ``delete_pass_for_operation`` directly (those are reached ONLY via the
    locked recover_deletions → delete_pass_for_operation → _drive_video /
    _drive_asset chain). Static source assertion — the wiring is one file."""
    src = Path(maintenance_mod.__file__).read_text()
    assert "recover_withdrawn_asset_cleanups" in src
    assert "recover_deletions" in src
    for forbidden in (".delete_video(", ".delete_asset(", "delete_pass_for_operation"):
        assert forbidden not in src, (
            f"maintenance.py must not call {forbidden!r} directly "
            f"(constraint d — only locked coordinator entries)")


# ===========================================================================
# M1 — non-current journal skips WITHOUT touching the prior-use sentinel
# ===========================================================================

def test_m1_fresh_journal_skips_without_init_or_sentinel(tmp_path: Path) -> None:
    """M1: a 'fresh' journal (.lecturecast/ exists, no DB) skips the network
    pass WITHOUT calling init/recover — so the durable prior-use sentinel
    (.lecturecast/heygen.used) is NEVER touched. init_database would touch it
    (heygen_journal._mark_prior_use at line 475), later fail-closing the
    capability probe after a runtime/ delete; the _journal_state gate refuses
    every non-current class before init is reached."""
    (tmp_path / ".lecturecast").mkdir()  # writable .lecturecast/, no DB → 'fresh'
    report = run_maintenance(tmp_path, now_iso=NOW)
    assert report.network_skipped is True
    assert report.skip_reason is not None
    assert "无 HeyGen journal" in report.skip_reason  # 'fresh' reason text
    # M1 core: the sentinel + DB were NOT created (init never ran).
    assert not (tmp_path / SENTINEL_REL).exists()
    assert not (tmp_path / DB_REL).exists()
    assert report.clean is False  # skipped → not clean (M2)


def test_m1_parent_unwritable_journal_skips_without_sentinel(tmp_path: Path) -> None:
    """M1 (second class): a bare tmp_path (no .lecturecast/) classifies as
    'parent_unwritable' — maintenance still skips WITHOUT touching the sentinel
    or creating a DB. The gate refuses every non-current class, not just 'fresh'."""
    report = run_maintenance(tmp_path, now_iso=NOW)
    assert report.network_skipped is True
    assert report.skip_reason is not None
    assert "父目录不可写" in report.skip_reason  # 'parent_unwritable' reason text
    assert not (tmp_path / SENTINEL_REL).exists()
    assert not (tmp_path / DB_REL).exists()
    assert report.clean is False


# ===========================================================================
# D-T10c — whitespace / empty / unset HEYGEN_API_KEY fail-closed (audit B1)
# ===========================================================================

@pytest.mark.parametrize("keyval", ["", "   ", "\t\n ", None])
def test_d_t10c_blank_key_fail_closed(tmp_path: Path, monkeypatch, keyval) -> None:
    """D-T10c / B1: a whitespace-only, empty, OR unset HEYGEN_API_KEY is treated
    identically to an absent key — the network pass is skipped (fail-closed:
    deletion_recovery stays ``{}``, NOT over-claimed as deleted). The DB pass
    STILL runs (state recovery is key-independent). The predicate reads the
    transport's OWN provider (single source of truth) + applies the SAME check
    the transport applies per-request (heygen_http.py:107:
    ``not isinstance(key, str) or not key.strip()``) — so it cannot drift from
    the value the transport would actually use, and a whitespace key cannot
    mask a config bug (audit B1)."""
    project = _current_project(tmp_path)
    if keyval is None:
        monkeypatch.delenv("HEYGEN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("HEYGEN_API_KEY", keyval)
    report = run_maintenance(project, now_iso=NOW)
    assert report.network_skipped is True
    assert report.deletion_recovery == {}  # network did not run
    assert report.skip_reason is not None
    assert "HEYGEN_API_KEY" in report.skip_reason
    # DB pass ran (key-independent) — empty aggregate on a journal with no
    # withdrawn receipts.
    assert report.db_recovery == dict(_EMPTY_DB_TALLY)
    assert report.clean is False


# ===========================================================================
# D-T10a — DB-only pass COMMITTED before the network pass is entered
# ===========================================================================

def test_d_t10a_db_pass_completes_before_network_pass(tmp_path: Path, monkeypatch) -> None:
    """D-T10a (call order): recover_withdrawn_asset_cleanups (DB-only) runs to
    COMPLETION before recover_deletions (network) is entered. Sequential
    single-threaded Python: the DB pass RETURNS (hence its begin_immediate has
    COMMITTED, op_repo:429) before the network pass is entered. The order list
    records exactly ``["db", "net"]`` — no interleave, no duplicate."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    order: list[str] = []

    orig_db = OperationRepository.recover_withdrawn_asset_cleanups

    def db_spy(self, *, now_iso):
        order.append("db")
        return orig_db(self, now_iso=now_iso)

    def del_spy(self, **kw):
        order.append("net")
        return dict(_CLEAN_DEL_TALLY)

    monkeypatch.setattr(OperationRepository, "recover_withdrawn_asset_cleanups", db_spy)
    monkeypatch.setattr(DeletionCoordinator, "recover_deletions", del_spy)

    report = run_maintenance(project, now_iso=NOW)
    assert order == ["db", "net"]
    assert report.network_skipped is False
    assert report.deletion_recovery == dict(_CLEAN_DEL_TALLY)
    assert report.db_recovery == dict(_EMPTY_DB_TALLY)


def test_d_t10a_committed_visibility_fresh_conn_sees_cleanup_required(
    tmp_path: Path, monkeypatch,
) -> None:
    """D-T10a (committed-visibility, audit m2 full strength): seed a withdrawn
    receipt + an 'uploaded' asset, run the REAL recover_withdrawn_asset_cleanups
    (no DB-pass spy), then inside the recover_deletions spy open a FRESH
    connection and assert the asset is ALREADY 'cleanup_required'. A fresh conn
    seeing the row proves the DB pass COMMITTED before the network pass was
    entered — not just call order, but durable committed order. The maintenance
    wiring adds no new transaction logic; committed-visibility is inherited
    from begin_immediate + unit-proven at test_asset_journal.py:260-264."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    op_id = _grant_parent(project, assets=(("portrait_photo", D_PORT),))
    conn = _fresh_conn(project)
    try:
        conn.execute("BEGIN")
        _withdraw_op(conn, op_id)
        _add_uploaded_asset(conn, op_id=op_id, role="portrait_photo", remote_id="p1")
        conn.commit()
    finally:
        conn.close()

    seen: dict = {}

    def del_spy(self, **kw):
        # Open a FRESH conn — if the DB pass committed, cleanup_required is
        # already visible to a brand-new connection (not just the DB-pass conn).
        fresh = _fresh_conn(project)
        try:
            row = fresh.execute(
                "SELECT status FROM heygen_asset_uploads WHERE parent_operation_id=?",
                (op_id,)).fetchone()
            seen["status_at_network_enter"] = row["status"]
        finally:
            fresh.close()
        return dict(_CLEAN_DEL_TALLY)

    monkeypatch.setattr(DeletionCoordinator, "recover_deletions", del_spy)

    report = run_maintenance(project, now_iso=NOW)
    assert report.network_skipped is False
    # The DB pass committed cleanup_required BEFORE the network pass was entered.
    assert seen["status_at_network_enter"] == "cleanup_required"
    # And the db_recovery tally reflects the one flipped asset.
    assert report.db_recovery["cleanup_required"] == 1
    assert report.db_recovery["kept"] == 0
    assert report.db_recovery["manual"] == 0


# ===========================================================================
# D-T10b + m4 — dual adapters, one shared transport, correct (not swapped)
# ===========================================================================

def test_d_t10b_dual_adapter_one_transport_not_swapped(tmp_path: Path, monkeypatch) -> None:
    """D-T10b + m4: recover_deletions receives deleter=HeyGenVideosAdapter AND
    adapter=HeyGenAssetAdapter, BOTH built from the ONE shared transport
    instance, and NOT swapped (videos→deleter, asset→adapter). Proven by
    replacing the transport + both adapter classes with stubs that capture the
    transport passed to ``__init__``; the spy on recover_deletions captures the
    built adapters. force is forwarded as a literal bool (constraint a/c)."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    sentinel = _SentinelTransport()
    built: dict = {}

    class _FakeVideosAdapter:
        def __init__(self, transport):
            built["videos_transport"] = transport

    class _FakeAssetAdapter:
        def __init__(self, transport):
            built["asset_transport"] = transport

    monkeypatch.setattr(maintenance_mod, "HeyGenHttpTransport", lambda: sentinel)
    monkeypatch.setattr(maintenance_mod, "HeyGenVideosAdapter", _FakeVideosAdapter)
    monkeypatch.setattr(maintenance_mod, "HeyGenAssetAdapter", _FakeAssetAdapter)

    capture: dict = {}
    _spy_recover_deletions(monkeypatch, capture)

    report = run_maintenance(project, now_iso=NOW, force=False)
    assert report.network_skipped is False
    # D-T10b: BOTH adapters built from the ONE shared transport instance.
    assert built["videos_transport"] is sentinel
    assert built["asset_transport"] is sentinel
    assert built["videos_transport"] is built["asset_transport"]
    # m4: correct assignment (videos→deleter, asset→adapter; not swapped).
    assert isinstance(capture["deleter"], _FakeVideosAdapter)
    assert isinstance(capture["adapter"], _FakeAssetAdapter)
    # constraint a/c: force forwarded as a literal bool.
    assert capture["force"] is False
    assert type(capture["force"]) is bool


# ===========================================================================
# M2 — exit code carries the recovery contract (0 clean / 2 partial-or-skip)
# ===========================================================================

def _run_cli(project: Path, *, force: bool = False) -> object:
    runner = CliRunner()
    args = ["maintenance", "--project-root", str(project)]
    if force:
        args.append("--force")
    return runner.invoke(app, args)


def test_m2_exit_0_on_clean_full_sweep(tmp_path: Path, monkeypatch) -> None:
    """M2: a clean full sweep (network ran, zero failures/alerts) → exit 0.
    A cron / ``&&`` consumer can gate on the exit code alone."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    _spy_recover_deletions(monkeypatch, {}, tally=_CLEAN_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 0


def test_m2_exit_2_on_missing_key(tmp_path: Path, monkeypatch) -> None:
    """M2: missing key → network skipped → exit 2 (NOT 0 — a piping script must
    detect the skip)."""
    project = _current_project(tmp_path)
    monkeypatch.delenv("HEYGEN_API_KEY", raising=False)
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_whitespace_key(tmp_path: Path, monkeypatch) -> None:
    """M2 + B1: whitespace key → network skipped → exit 2."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "   ")
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_partial_failure(tmp_path: Path, monkeypatch) -> None:
    """M2: recover_deletions returns failed/alerted > 0 → partial → exit 2
    (NOT 0 — partial recovery must surface to the operator)."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    _spy_recover_deletions(monkeypatch, {}, tally=_PARTIAL_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_recover_deletions_exception(tmp_path: Path, monkeypatch) -> None:
    """M2 + m3: recover_deletions RAISING is wrapped into a skip report (the
    committed db_tally is preserved) → exit 2, NOT exit 1. The lib never lets a
    post-DB failure escape as a harness exception; exit 1 is reserved for
    harness exceptions raised before emit (never reached post-m3)."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    def boom(self, **kw):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(DeletionCoordinator, "recover_deletions", boom)
    result = _run_cli(project)
    assert result.exit_code == 2


# ===========================================================================
# Report-shape sanity (the MaintenanceReport contract the CLI + tests rely on)
# ===========================================================================

def test_report_clean_property_matrix() -> None:
    """The ``clean`` property is the exit-0 condition (M2 + Codex round-2:
    every dimension the fail-closed invariant requires). True iff ALL of:
      - DB pass did not raise (``db_recovery_failed`` False);
      - network pass ran (``network_skipped`` False);
      - deletion_recovery has the EXACT 8-key shape (malformed → not clean);
      - failed/alerted/ops_alerted all zero;
      - ``attempted == deleted`` (catches skipped AND the not_advanced class
        recover_deletions does not aggregate);
      - DB-side manual/left_uploading both zero.
    Static matrix over the report shape so the CLI exit-code contract cannot
    drift from the property."""
    # Base: clean full sweep.
    assert MaintenanceReport(
        deletion_recovery=dict(_CLEAN_DEL_TALLY), force=False).clean is True
    # network_skipped → not clean.
    assert MaintenanceReport(
        deletion_recovery={}, network_skipped=True, force=False).clean is False
    # db_recovery_failed → not clean (Codex round-1 blocker 4).
    assert MaintenanceReport(
        deletion_recovery={}, db_recovery_failed=True,
        network_skipped=True, force=False).clean is False
    # each explicit failure dimension independently → not clean.
    for dim in ("failed", "alerted", "ops_alerted"):
        tally = dict(_CLEAN_DEL_TALLY)
        tally[dim] = 1
        assert MaintenanceReport(deletion_recovery=tally, force=False).clean is False, dim
    # skipped > 0 (realistic tally: attempted bumps with skipped) → not clean
    # (Codex round-1 blocker 1: clean used to ignore skipped).
    assert MaintenanceReport(
        deletion_recovery=dict(_SKIPPED_DEL_TALLY), force=False).clean is False
    # attempted > deleted with no explicit failure (the not_advanced / busy
    # class — invisible in the 8-key return) → not clean (Codex round-1
    # blocker 1: the attempted==deleted predicate is what catches it).
    assert MaintenanceReport(
        deletion_recovery=dict(_BUSY_DEL_TALLY), force=False).clean is False
    # DB-side pending work → not clean (Codex round-1 blocker 2).
    assert MaintenanceReport(
        db_recovery=dict(_MANUAL_DB_TALLY),
        deletion_recovery=dict(_CLEAN_DEL_TALLY), force=False).clean is False
    assert MaintenanceReport(
        db_recovery=dict(_LEFT_UPLOADING_DB_TALLY),
        deletion_recovery=dict(_CLEAN_DEL_TALLY), force=False).clean is False
    # shape: empty deletion_recovery with network_skipped=False → not clean
    # (a silent no-op that looks like success — Codex round-1 empty-tally gap).
    assert MaintenanceReport(
        deletion_recovery={}, network_skipped=False, force=False).clean is False
    # shape: missing a key → not clean.
    short = {k: v for k, v in _CLEAN_DEL_TALLY.items() if k != "skipped"}
    assert MaintenanceReport(
        deletion_recovery=short, force=False).clean is False
    # shape: extra key → not clean.
    extra = dict(_CLEAN_DEL_TALLY)
    extra["phantom"] = 0
    assert MaintenanceReport(
        deletion_recovery=extra, force=False).clean is False


# ===========================================================================
# Codex round-2 — exit code surfaces every non-clean dimension (blockers 1+2)
# ===========================================================================

def test_m2_exit_2_on_skipped(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 blocker 1: recover_deletions returns skipped > 0 (a
    candidate skipped_no_upload_id / skipped_unknown_kind) → exit 2. ``clean``
    used to ignore ``skipped``; the round-2 ``attempted == deleted`` predicate
    catches it (attempted=1, deleted=0). A cron consumer must NOT see exit 0."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    _spy_recover_deletions(monkeypatch, {}, tally=_SKIPPED_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_busy_not_advanced(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 blocker 1: a candidate stuck not_advanced (busy /
    retry_wait / not_ready / fence_conflict) — the class recover_deletions does
    NOT aggregate into the 8-key return. It is invisible except as
    ``attempted - (deleted+failed+skipped+alerted) > 0``; the round-2
    ``attempted == deleted`` predicate catches it → exit 2."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")
    _spy_recover_deletions(monkeypatch, {}, tally=_BUSY_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_db_manual(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 blocker 2: DB-side ``manual`` > 0 (an asset needs human
    reconciliation) → exit 2. The network pass may have run clean, but there is
    unresolved DB-side work this sweep — a cron consumer must NOT see exit 0."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    orig_db = OperationRepository.recover_withdrawn_asset_cleanups

    def db_spy(self, *, now_iso):
        orig_db(self, now_iso=now_iso)  # run the real DB pass (empty)
        return dict(_MANUAL_DB_TALLY)   # ...but report a manual tally
    monkeypatch.setattr(OperationRepository, "recover_withdrawn_asset_cleanups", db_spy)
    _spy_recover_deletions(monkeypatch, {}, tally=_CLEAN_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 2


def test_m2_exit_2_on_db_left_uploading(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 blocker 2: DB-side ``left_uploading`` > 0 (an active upload
    lease was left intact — maintenance correctly does not touch it, but it IS
    unresolved this sweep) → exit 2."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    orig_db = OperationRepository.recover_withdrawn_asset_cleanups

    def db_spy(self, *, now_iso):
        orig_db(self, now_iso=now_iso)
        return dict(_LEFT_UPLOADING_DB_TALLY)
    monkeypatch.setattr(OperationRepository, "recover_withdrawn_asset_cleanups", db_spy)
    _spy_recover_deletions(monkeypatch, {}, tally=_CLEAN_DEL_TALLY)
    result = _run_cli(project)
    assert result.exit_code == 2


# ===========================================================================
# Codex round-2 — DB-pass exception wrapped (blocker 4) + shape wrap (empty gap)
# ===========================================================================

def test_db_pass_exception_wrapped_to_skip_report(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 blocker 4: recover_withdrawn_asset_cleanups RAISING (e.g. a
    withdrawn receipt with corrupt topology → OperationIntegrityError) is wrapped
    into a structured skip report (db_recovery_failed=True, network_skipped=True,
    db_recovery={}) → exit 2, NOT exit 1. The tx rolled back (begin_immediate),
    so nothing was over-claimed; the network pass does NOT run on a half-recovered
    journal. db_recovery_failed disambiguates "DB pass did not complete" from
    "DB pass ran with zero work" (db_recovery={} would otherwise be ambiguous)."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    def boom(self, *, now_iso):
        raise RuntimeError("DB integrity exploded")
    monkeypatch.setattr(OperationRepository, "recover_withdrawn_asset_cleanups", boom)

    report = run_maintenance(project, now_iso=NOW)
    assert report.db_recovery_failed is True
    assert report.network_skipped is True
    assert report.db_recovery == {}  # DB pass did not complete → no tally
    assert report.deletion_recovery == {}  # network did not run
    assert report.skip_reason is not None
    assert "DB 状态恢复失败" in report.skip_reason
    assert "RuntimeError" in report.skip_reason
    assert report.clean is False

    # CLI surfaces exit 2 (NOT exit 1 — exit 1 is reserved for harness exceptions).
    result = _run_cli(project)
    assert result.exit_code == 2


def test_malformed_tally_shape_wrapped_to_skip_report(tmp_path: Path, monkeypatch) -> None:
    """Codex round-1 empty-tally gap: recover_deletions returning a malformed
    tally (missing/extra keys, or empty {}) is surfaced as a skip report (NOT
    clean) rather than masquerading as a no-op sweep. Parametrized over: empty,
    missing-one-key, extra-key."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    malformed = {
        "empty": {},
        "missing": {k: v for k, v in _CLEAN_DEL_TALLY.items() if k != "skipped"},
        "extra": {**_CLEAN_DEL_TALLY, "phantom": 0},
    }
    for name, bad_tally in malformed.items():
        _spy_recover_deletions(monkeypatch, {}, tally=bad_tally)
        report = run_maintenance(project, now_iso=NOW)
        assert report.network_skipped is True, name
        assert report.deletion_recovery == {}, name  # rejected → not surfaced as a result
        assert report.skip_reason is not None, name
        assert "畸形 tally" in report.skip_reason, name
        assert report.clean is False, name


# ===========================================================================
# Codex round-2 — lib-boundary entry validation (programming-error guards)
# ===========================================================================

@pytest.mark.parametrize("bad_seconds", ["300", 1.0, True, False, None, 29, 3601, [300]])
def test_entry_guard_rejects_bad_lease_seconds(tmp_path: Path, bad_seconds) -> None:
    """Codex round-1 (lease/now_iso entry validation): an invalid lease_seconds
    is rejected at the lib boundary BEFORE any DB read — fires on a bare tmp_path
    (no journal created). type() is int rejects str/float/bool/None/list; the
    range check rejects 29 (< LEASE_MIN_SECONDS=30) + 3601 (> LEASE_MAX_SECONDS=3600).
    bool is rejected by the type guard (``type(True) is int`` is False even though
    bool is an int subclass). The CLI's default (300) is always valid, so this
    never fires via the CLI — it guards direct lib callers."""
    with pytest.raises(ValueError, match="lease_seconds"):
        run_maintenance(tmp_path, now_iso=NOW, lease_seconds=bad_seconds)
    assert not (tmp_path / DB_REL).exists()
    assert not (tmp_path / SENTINEL_REL).exists()


@pytest.mark.parametrize("bad_now", ["not-a-date", "2026-08-02T00:00:00", "", "2026-13-99"])
def test_entry_guard_rejects_bad_now_iso(tmp_path: Path, bad_now) -> None:
    """Codex round-1 (lease/now_iso entry validation): a non-ISO / naive
    (no tzinfo) now_iso is rejected at the lib boundary BEFORE any DB read.
    ``2026-08-02T00:00:00`` (no tz) is rejected — lease judgment needs a
    timezone-aware timestamp."""
    with pytest.raises(ValueError):
        run_maintenance(tmp_path, now_iso=bad_now)
    assert not (tmp_path / DB_REL).exists()


@pytest.mark.parametrize("bad_owner", ["", "ab", "x" * 100, "has space", "中文owner"])
def test_entry_guard_rejects_bad_lease_owner(tmp_path: Path, bad_owner) -> None:
    """Codex round-1 (lease/now_iso entry validation): an invalid lease_owner
    (empty / too short / too long / contains spaces / non-ASCII) is rejected at
    the lib boundary BEFORE any DB read. Regex: ``^[A-Za-z0-9_:.-]{3,96}$``."""
    with pytest.raises(ValueError, match="invalid lease_owner"):
        run_maintenance(tmp_path, now_iso=NOW, lease_owner=bad_owner)
    assert not (tmp_path / DB_REL).exists()


def test_entry_guard_order_force_first(tmp_path: Path) -> None:
    """The force guard fires BEFORE the lease/now guards (it is the first check
    in run_maintenance). A non-bool force + a bad lease_seconds → force ValueError
    wins (the lease guard is never reached). Proves the m1 force invariant is the
    strongest gate (fires regardless of every other arg)."""
    with pytest.raises(ValueError, match="force must be a bool"):
        run_maintenance(tmp_path, now_iso="not-a-date", lease_seconds="bad",
                        force="yes")  # type: ignore[arg-type]


# ===========================================================================
# Codex round-2 — dynamic constraint (d): adapter delete methods never called
# ===========================================================================

def test_constraint_d_dynamic_adapter_delete_never_called(
    tmp_path: Path, monkeypatch,
) -> None:
    """Codex round-1 claim 7 (constraint d, dynamic complement to the source-level
    D-T12 test): at RUNTIME, the dual adapters' ``delete_video`` /
    ``delete_asset`` / ``delete_pass_for_operation`` methods are NEVER called by
    the maintenance wiring — the network path routes exclusively through the
    locked ``recover_deletions`` (here stubbed). Proves no dynamic path bypasses
    the source-level assertion (e.g. a helper that reaches adapter.delete_*).
    Instruments both adapter classes' delete methods + asserts zero calls."""
    project = _current_project(tmp_path)
    monkeypatch.setenv("HEYGEN_API_KEY", "test-key")

    calls: dict[str, int] = {"videos_delete": 0, "asset_delete": 0}

    # Instrument the REAL adapter classes' delete methods (not stubs) — if the
    # wiring ever called them directly, the counter would bump.
    from lecturecast.heygen_videos_adapter import HeyGenVideosAdapter
    from lecturecast.heygen_asset_adapter import HeyGenAssetAdapter

    def v_spy(self, *a, **kw):
        calls["videos_delete"] += 1
    def a_spy(self, *a, **kw):
        calls["asset_delete"] += 1

    # Patch every delete-named method on both adapter classes.
    for name in ("delete_video", "delete_pass_for_operation"):
        if hasattr(HeyGenVideosAdapter, name):
            monkeypatch.setattr(HeyGenVideosAdapter, name, v_spy)
    for name in ("delete_asset", "delete_pass_for_operation"):
        if hasattr(HeyGenAssetAdapter, name):
            monkeypatch.setattr(HeyGenAssetAdapter, name, a_spy)

    _spy_recover_deletions(monkeypatch, {}, tally=_CLEAN_DEL_TALLY)
    report = run_maintenance(project, now_iso=NOW)
    assert report.network_skipped is False
    assert calls == {"videos_delete": 0, "asset_delete": 0}


# ===========================================================================
# Codex round-2 — M1 generic classification: EVERY non-current class skips
# ===========================================================================

_NON_CURRENT_CLASSES = [
    "fresh", "missing_prior_use", "behind", "ahead", "symlink",
    "parent_unwritable", "runtime_unwritable", "db_readonly",
    "shape_mismatch", "canonical_unavailable", "unreadable",
]


@pytest.mark.parametrize("cls", _NON_CURRENT_CLASSES)
def test_m1_every_non_current_class_skips_without_sentinel(
    tmp_path: Path, monkeypatch, cls: str,
) -> None:
    """Codex round-1 claim 2 (M1 classification completeness): EVERY non-current
    class _journal_state can return skips the network pass WITHOUT calling
    init/recover, so the durable prior-use sentinel (.lecturecast/heygen.used)
    is never touched. Mocks _journal_state to yield each class in turn (the two
    real-state tests above cover 'fresh' + 'parent_unwritable' at the file-
    system level; this parametrization covers the FULL set the gate must refuse,
    including the classes hard to fabricate on disk — behind/ahead/symlink/
    db_readonly/shape_mismatch/canonical_unavailable/unreadable). The journal
    gate is the ONLY init-avoiding barrier, so proving it refuses all 11 classes
    closes M1 exhaustively."""
    # init_database is never reached → never touches the sentinel. Patch it to
    # RAISE so the test fails loudly if any code path bypasses the gate.
    def boom_init(*a, **kw):
        raise AssertionError(f"init_database reached on classification={cls!r} (M1 bypass)")
    monkeypatch.setattr("lecturecast.heygen_journal.init_database", boom_init)
    monkeypatch.setattr(maintenance_mod, "init_database", boom_init, raising=False)

    # _journal_state is read-only; stub it to yield the class under test.
    monkeypatch.setattr(
        maintenance_mod, "_journal_state",
        lambda project_dir: {"classification": cls})

    report = run_maintenance(tmp_path, now_iso=NOW)
    assert report.network_skipped is True, cls
    assert report.skip_reason is not None, cls
    assert report.clean is False, cls
    # M1 core: the sentinel + DB were NOT created (init never ran).
    assert not (tmp_path / SENTINEL_REL).exists(), cls
    assert not (tmp_path / DB_REL).exists(), cls
