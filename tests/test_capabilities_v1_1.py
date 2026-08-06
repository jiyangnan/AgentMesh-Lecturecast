"""V1.1 capability capture (§5.5b): F5 + HeyGen BYO detection.

F5 / HeyGen detection is FAIL-CLOSED until the adapters + journal ship in
§5.5e: the default runtime/adapter/journal probes return False, so production
never claims an unexecutable capability (which the server would bill M2/M3 on).
Tests inject True probes to exercise the declaration logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lecturecast.capabilities import (
    F5_MODEL_PATH_ENV,
    HEYGEN_API_KEY_ENV,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
    f5_available,
    heygen_processor,
)
from lecturecast.protocol import ClientCapabilitiesV1_1


_TRUE = lambda: True  # noqa: E731


# ---- f5_available (fail-closed without the runtime probe) ----

def test_f5_absent_when_no_model_path() -> None:
    assert f5_available(env={}, runtime_probe=_TRUE) is False


def test_f5_fail_closed_without_runtime_even_with_model(tmp_path: Path) -> None:
    """A model file alone is NOT enough — the F5 runtime/adapter probe must
    pass. Default probe fails closed (no adapter shipped yet)."""
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(env={F5_MODEL_PATH_ENV: str(model)}) is False  # default probe


def test_f5_present_only_when_model_and_runtime(tmp_path: Path) -> None:
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(model)}, runtime_probe=_TRUE,
    ) is True


def test_f5_absent_when_model_missing_even_with_runtime(tmp_path: Path) -> None:
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(tmp_path / "nope.pth")}, runtime_probe=_TRUE,
    ) is False


# ---- heygen_processor (fail-closed without adapter + journal) ----

def test_heygen_none_when_no_key() -> None:
    assert heygen_processor(env={}, adapter_probe=_TRUE, journal_probe=_TRUE) is None


def test_heygen_fail_closed_without_adapter_and_journal() -> None:
    """A key alone is NOT enough — adapter + journal probes must pass. Defaults
    fail closed (neither shipped yet)."""
    assert heygen_processor(env={HEYGEN_API_KEY_ENV: "sk"}) is None


def test_heygen_declared_when_key_adapter_journal_no_secret_uploaded() -> None:
    proc = heygen_processor(
        env={HEYGEN_API_KEY_ENV: "sk_secret_value"},
        adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    assert proc == {
        "provider": "heygen",
        "api_version": "v3",
        "configured": True,
        "credential_mode": "byo_local",
        # §5.5e5c: operations reflect locked primitives — asset_delete/video_delete
        # added (DeletionCoordinator + adapters shipped); avatar_delete omitted
        # (no dedicated primitive). features add title_query (query_videos_by_title)
        # + read_only_auth_check (get_asset docstring "doctor only" backed).
        "operations": [
            "direct_asset_upload",
            "photo_avatar",
            "prerecorded_audio_lipsync",
            "asset_delete",
            "video_delete",
        ],
        "features": ["idempotency_24h", "title_query", "read_only_auth_check"],
    }
    assert "sk_secret_value" not in repr(proc)
    assert "verified" not in proc


# ---- §5.5e5c real probes (production wiring: director.py + project.py) ----

def test_default_adapter_probe_imports_shipped_modules() -> None:
    """The real adapter probe returns True: the three HeyGen adapter modules +
    key classes are shipped (§5.5e5 locked)."""
    assert default_heygen_adapter_probe() is True


def test_default_adapter_probe_fail_closed_on_missing_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a shipped module is missing the expected class, the probe fails closed
    (module importability alone is NOT enough — §3.1 over-report guard)."""
    import lecturecast.heygen_asset_adapter as mod

    monkeypatch.setattr(mod, "HeyGenAssetAdapter", None)
    assert default_heygen_adapter_probe() is False


def test_default_adapter_probe_fail_closed_on_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-2 (B2): a non-ImportError failure during adapter import
    (RuntimeError / OSError / binary-dep init) must fail closed, not raise —
    a raise would escape the shared v1.1 capture path and could block the M1
    base delivery that does not even depend on HeyGen."""
    import importlib

    real_import_module = importlib.import_module

    def _boom(name, *args, **kwargs):
        if name == "lecturecast.heygen_http":
            raise RuntimeError("binary dependency failed to load")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _boom)
    assert default_heygen_adapter_probe() is False


def test_default_adapter_probe_fail_closed_on_missing_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-2 (W1): class resolution alone is not enough — if a
    reported operation's backing method is stripped (partial / mixed-version
    install), the probe must fail closed so the server does not bill an
    operation the client cannot serve."""
    import lecturecast.heygen_videos_adapter as mod

    monkeypatch.delattr(mod.HeyGenVideosAdapter, "delete_video", raising=False)
    assert default_heygen_adapter_probe() is False


_V6_TABLES = (
    "heygen_operations",
    "heygen_consent_receipts",
    "heygen_remote_resources",
    "heygen_resource_operation_refs",
    "heygen_asset_uploads",
)


def _make_db(
    tmp_path: Path,
    *,
    head: int | None = None,
    core_table: bool = False,
    missing_table: str | None = None,
    stub_columns: bool = False,
) -> Path:
    """Build a journal DB at the expected probe path.

    core_table=True creates the REAL v6 schema — the same _DDL_STATEMENTS
    init_database runs — so a head==_SCHEMA_VERSION DB the probe should ACCEPT
    (round-4 R3-1/R3-2: the prior stub `(id INTEGER PRIMARY KEY)` tables were
    a test-only fabrication that asserted the very over-report Codex flagged).
    missing_table drops one table AFTER creating the full schema (to exercise
    the partial-schema guard). stub_columns=True instead creates the five table
    NAMES with only `(id INTEGER PRIMARY KEY)` columns (Codex round-3 repro) —
    names + user_version present but column shape wrong, which the probe must
    REJECT. Without core_table/stub_columns the DB is empty (the version-only-
    lying state the probe must reject, B5)."""
    import sqlite3

    from lecturecast.heygen_journal import _DDL_STATEMENTS

    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    db = runtime / "heygen-operations.db"
    conn = sqlite3.connect(str(db))
    if core_table:
        # Mirror init_database exactly: execute each DDL statement individually
        # (executescript would issue an implicit COMMIT; irrelevant for a fresh
        # test DB, but mirroring prod avoids masking a statement-level issue).
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        if missing_table:
            # Drop AFTER create so the remaining tables keep their FK references
            # (dangling refs are exactly the "missing one table" shape we test).
            conn.execute(f"DROP TABLE IF EXISTS {missing_table}")
    elif stub_columns:
        for table in _V6_TABLES:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
    if head is not None:
        conn.executescript(f"PRAGMA user_version = {head};")
    conn.close()
    return db


def test_default_journal_probe_missing_db_is_ready(tmp_path: Path) -> None:
    """A not-yet-initialized journal is ready — the first op auto-inits via
    init_database (locked, idempotent). The probe is read-only (never creates).
    Real fresh state: the project is initialized (.lecturecast/ exists,
    writable) but HeyGen was never used (no runtime/, no prior configured caps)."""
    (tmp_path / ".lecturecast").mkdir(parents=True)
    assert default_heygen_journal_probe(tmp_path) is True
    assert not (tmp_path / ".lecturecast" / "runtime" / "heygen-operations.db").exists()


def test_default_journal_probe_head_current_is_ready(tmp_path: Path) -> None:
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    # §5.5e5c round-3 (R3-1): head alone is not enough — ALL five v6 tables
    # must exist. A real head=_SCHEMA_VERSION DB with the full schema is ready.
    _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=True)
    assert default_heygen_journal_probe(tmp_path) is True


def test_default_journal_probe_head_below_is_not_ready(tmp_path: Path) -> None:
    """§5.5e5c round-3 (R3-2): head < _SCHEMA_VERSION -> fail-closed. A legit
    prior-version DB CAN be migrated, but the probe cannot cheaply verify the
    prior-version schema is complete enough to migrate without failing (a
    partial old schema can advance user_version yet leave an unusable DB). The
    doctor / canary path (§5.5e5d) provides explicit migration; the billing
    path never depends on an upgrade succeeding."""
    _make_db(tmp_path, head=3, core_table=True)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_head_ahead_is_not_ready(tmp_path: Path) -> None:
    """head > _SCHEMA_VERSION is refuse-downgrade (genuinely incompatible) → False."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION + 1)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_empty_db_head_set_is_not_ready(tmp_path: Path) -> None:
    """§5.5e5c round-2 (B5): a DB whose user_version was set without the tables
    (manual PRAGMA / partial copy / corruption) must NOT be reported ready.
    init_database sees head==_SCHEMA_VERSION and skips migration, so the first
    op would fail 'no such table'. Version alone is not schema shape."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=False)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_missing_db_with_runtime_dir_is_not_ready(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-2 (B3): runtime/ exists (created by a prior init_database)
    but the DB is gone -> the journal was initialized before and has since been
    deleted. Prior remote resources are unrecoverable; reporting ready would
    over-claim idempotency_24h / delete capability on lost history."""
    (tmp_path / ".lecturecast" / "runtime").mkdir(parents=True)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_symlink_component_is_not_ready(tmp_path: Path) -> None:
    """§5.5e5c round-2 (B4): init_database rejects symlink components
    (heygen_journal:406). The probe mirrors that rejection so it does not
    over-report against an init that will raise."""
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    link_target = tmp_path / "evil.db"
    link_target.write_bytes(b"not a sqlite db")
    (runtime / "heygen-operations.db").symlink_to(link_target)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_missing_one_v6_table_is_not_ready(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-3 (R3-1): head==_SCHEMA_VERSION but one of the five v6
    tables is missing (here heygen_asset_uploads) -> asset upload / delete
    would fail at runtime. The probe requires the FULL v6 schema, not just one
    core table."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=True,
             missing_table="heygen_asset_uploads")
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_runtime_deleted_but_caps_configured_is_not_ready(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-3 (R3-3): the whole runtime/ was deleted after prior use.
    FS has no HeyGen trace under runtime/, but client-capabilities.json (stored
    at .lecturecast/, outside runtime/) still records a prior configured=true
    claim -> prior-use data loss -> fail-closed (do not re-report on lost history)."""
    import json

    lecturecast_dir = tmp_path / ".lecturecast"
    lecturecast_dir.mkdir(parents=True)
    # Prior caps once claimed configured HeyGen. runtime/ is gone entirely.
    (lecturecast_dir / "client-capabilities.json").write_text(json.dumps({
        "schema_version": "1.1",
        "third_party_processors": [{"provider": "heygen", "configured": True}],
    }))
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_resolve_failure_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-3 (R3-4): if db_path.resolve() raises (OSError / symlink
    loop / permission), the probe must fail closed, not propagate a raise that
    could escape the shared v1.1 capture path and block M1."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=True)

    def _boom(*_args, **_kwargs):
        raise OSError("symbolic link loop")

    monkeypatch.setattr(Path, "resolve", _boom)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_adapter_probe_fail_closed_on_non_callable_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-3 (R3-5): a required attribute set to a non-callable
    (e.g. delete_video = 1) must fail closed. `is not None` would pass it and
    the operation would raise TypeError at runtime."""
    import lecturecast.heygen_videos_adapter as mod

    monkeypatch.setattr(mod.HeyGenVideosAdapter, "delete_video", 1)
    assert default_heygen_adapter_probe() is False


def test_default_adapter_probe_fail_closed_on_missing_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-3 (R3-6): the adapters ship but the journal-backed
    processor / orchestrator module (operation_repository) does not import in
    a mixed install -> the operations cannot execute -> fail closed."""
    import importlib

    real_import_module = importlib.import_module

    def _boom(name, *args, **kwargs):
        if name == "lecturecast.operation_repository":
            raise ImportError("operation_repository not shipped in this install")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _boom)
    assert default_heygen_adapter_probe() is False


def test_default_journal_probe_unwritable_runtime_is_not_ready(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-3 (R3-7): runtime/ is readable but not writable. Every
    processor writes (WAL + BEGIN IMMEDIATE + journal rows), so a read-only
    probe that ignores writability would over-report against an init that will
    fail. The probe requires W_OK|X_OK on runtime/."""
    import os

    if os.name == "nt":
        pytest.skip("Windows does not enforce POSIX directory mode bits")

    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=True)
    runtime = tmp_path / ".lecturecast" / "runtime"
    os.chmod(runtime, 0o500)  # r-x: readable + traversable, NOT writable
    try:
        assert default_heygen_journal_probe(tmp_path) is False
    finally:
        os.chmod(runtime, 0o700)  # restore so pytest can clean up


def test_default_journal_probe_stub_columns_is_not_ready(tmp_path: Path) -> None:
    """§5.5e5c round-4 (R3-1/R3-2): table NAMES + user_version present but each
    table is a stub `(id INTEGER PRIMARY KEY)` (Codex's repro: a hand-crafted or
    partially-corrupted head==6 DB). The probe must compare COLUMN sets against
    the canonical schema, not just names — otherwise the first real repository
    query fails 'no such column'."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _make_db(tmp_path, head=_SCHEMA_VERSION, stub_columns=True)
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_readonly_db_file_is_not_ready(tmp_path: Path) -> None:
    """§5.5e5c round-4 (R3-7): runtime/ is writable but the DB file itself is
    read-only (chmod 0400 — e.g. a restored backup or a chmod accident). A
    directory-only writability check passes, but every operation write fails
    'attempt to write a readonly database'. The probe requires W_OK on the DB
    file too."""
    import os

    if os.name == "nt":
        pytest.skip("Windows does not enforce POSIX file mode bits")

    from lecturecast.heygen_journal import _SCHEMA_VERSION

    db = _make_db(tmp_path, head=_SCHEMA_VERSION, core_table=True)
    os.chmod(db, 0o400)
    try:
        assert default_heygen_journal_probe(tmp_path) is False
    finally:
        os.chmod(db, 0o600)  # restore so pytest can clean up


def test_default_journal_probe_sentinel_marks_prior_use(tmp_path: Path) -> None:
    """§5.5e5c round-4 (R3-3): the whole runtime/ was deleted after prior use AND
    client-capabilities.json was since overwritten with an unconfigured snapshot
    (the non-monotonic failure Codex identified: recapture erases the caps
    marker). The durable `.lecturecast/heygen.used` sentinel — written once by
    init_database, outside runtime/ — survives both, so the probe still treats
    the missing journal as data loss, not a fresh project."""
    from lecturecast.heygen_journal import _PRIOR_USE_SENTINEL

    lecturecast_dir = tmp_path / ".lecturecast"
    lecturecast_dir.mkdir(parents=True)
    # Prior use happened: init_database wrote the sentinel. runtime/ is gone and
    # caps never carried configured HeyGen — yet the durable marker persists.
    (lecturecast_dir / _PRIOR_USE_SENTINEL).touch()
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_adapter_probe_fail_closed_on_missing_orchestrator_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-4 (R3-6): operation_repository IMPORTS but a required
    processor / coordinator class is absent (partial / mixed install). Module
    import alone would pass; the probe must verify each required class resolves
    as a real type (isinstance(x, type) — mirrors the round-3 R3-5 callable
    lesson: presence is not enough)."""
    import lecturecast.operation_repository as mod

    monkeypatch.delattr(mod, "DeleteProcessor", raising=False)
    assert default_heygen_adapter_probe() is False


def test_default_adapter_probe_fail_closed_on_method_stripped_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-5 (R4-6): a required class resolves as a real type but its
    entry method is stripped (e.g. a mixed-version install that renamed
    delete_once). isinstance(type) alone would pass it; the probe must verify
    each entry method is callable (parallel to the adapter method check)."""
    import lecturecast.operation_repository as mod

    monkeypatch.delattr(mod.DeleteProcessor, "delete_once", raising=False)
    assert default_heygen_adapter_probe() is False


def test_default_adapter_probe_fail_closed_on_stripped_poll_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-6 (R5-6): HeyGenVideosAdapter.poll_video is a PUBLIC adapter
    entry method called by PollProcessor.poll_once (op_repo:3534) AND
    DownloadProcessor.download_once (op_repo:3644). The round-5 probe checked
    submit_video / query_videos_by_title / delete_video but omitted poll_video,
    so a mixed-version install that strips it passed the probe while making
    polling + download unservable. The probe must now check the FULL public
    surface of every adapter class."""
    import lecturecast.heygen_videos_adapter as mod

    monkeypatch.delattr(mod.HeyGenVideosAdapter, "poll_video", raising=False)
    assert default_heygen_adapter_probe() is False


def test_default_journal_probe_sentinel_directory_marks_prior_use(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-5 (R4-3): the sentinel path exists as a DIRECTORY (so
    init's best-effort touch raised IsADirectoryError and was swallowed — no
    sentinel file written). The probe must treat ANY path entry as prior-use
    (lexists, not isfile), else a later runtime/ deletion over-reports on lost
    idempotency history."""
    from lecturecast.heygen_journal import _PRIOR_USE_SENTINEL

    lecturecast_dir = tmp_path / ".lecturecast"
    lecturecast_dir.mkdir(parents=True)
    (lecturecast_dir / _PRIOR_USE_SENTINEL).mkdir()  # a directory, not a file
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_malformed_non_object_caps_is_not_ready(
    tmp_path: Path,
) -> None:
    """§5.5e5c round-4 (R3-4): client-capabilities.json is valid JSON but a
    non-object (`[1, 2, 3]`). Previously `data.get(...)` raised AttributeError
    (fail-stop, escaping the shared capture path); now the isinstance guard (or
    the top-level backstop) treats it as prior-use -> fail-closed. The probe
    must NOT raise."""
    lecturecast_dir = tmp_path / ".lecturecast"
    lecturecast_dir.mkdir(parents=True)
    (lecturecast_dir / "client-capabilities.json").write_text("[1, 2, 3]")
    assert default_heygen_journal_probe(tmp_path) is False


def test_default_journal_probe_backstop_on_unexpected_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-4 (R3-4): the probe is a boolean predicate whose contract
    is 'never raise'. Any UNEXPECTED error (not sqlite3.Error / OSError) inside
    the probe must hit the top-level fail-closed backstop, not escape into the
    shared v1.1 capture path and block M1."""
    import lecturecast.capabilities as caps

    (tmp_path / ".lecturecast").mkdir(parents=True)

    def _boom(_lecturecast_dir: Path) -> bool:
        raise ValueError("unexpected non-OS, non-sqlite error")

    monkeypatch.setattr(caps, "_prior_heygen_use_detected", _boom)
    # Missing DB + writable .lecturecast -> reaches the prior-use call, which
    # raises ValueError -> top-level backstop -> False (no exception escapes).
    assert default_heygen_journal_probe(tmp_path) is False


def test_m1_independent_of_heygen_config() -> None:
    """§5.5e5c C7 / spec §2.6 line 489: with no HEYGEN_API_KEY, third_party_processors
    is absent even when the real adapter + journal probes pass — the key is gated
    inside heygen_processor. The v1.1 capability still captures + round-trips,
    so the M1 base path (edge-tts, no avatar) is unaffected by HeyGen config."""
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
        adapter_probe=default_heygen_adapter_probe,
        journal_probe=lambda: default_heygen_journal_probe(Path("/tmp")),
    )
    payload = caps.model_dump()
    assert payload.get("third_party_processors") in (None, [])
    assert payload["schema_version"] == "1.1"


def test_saving_configured_heygen_caps_initializes_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UAT Path D regression: M1 captures a configured-HeyGen capability on a
    fresh project (journal probe True because no DB yet = 'fresh'). If nothing
    initializes the journal, the NEXT command's B1 live re-probe sees
    client-capabilities.json claiming configured HeyGen with no runtime/ DB ->
    _journal_state Guard 3 -> 'missing_prior_use' -> probe False ->
    heygen_processor None -> _stored_heygen_still_live False -> snapshot
    dropped -> re-capture without HeyGen -> save_capabilities rejects the digest
    rewrite (manifest bound) -> M2 permanently deadlocks. save_capabilities must
    initialize the journal (init_database) the moment it persists a
    configured-HeyGen doc, so B1 on the next command sees 'current' and reuses
    the snapshot."""
    from lecturecast.capabilities import default_heygen_journal_diagnostic
    from lecturecast.commands.director import _stored_heygen_still_live
    from lecturecast.project import ProjectStore

    # Real Path D: HEYGEN_API_KEY lives in the process env (B1 reads os.environ).
    monkeypatch.setenv(HEYGEN_API_KEY_ENV, "sk_live")
    store = ProjectStore(tmp_path)
    state = store.init(name="Path D")
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={HEYGEN_API_KEY_ENV: "sk_live"}, path_probe=Path,
        adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    assert caps.model_dump()["third_party_processors"][0]["configured"] is True

    store.save_capabilities(caps, expected_revision=state.revision)

    # The journal must now be initialized (DB present, head==schema) — NOT the
    # prior-use-data-loss state a missing DB + configured caps would produce.
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "current", diag
    # The B1 stale guard on the next command now reuses the snapshot.
    assert _stored_heygen_still_live(caps, tmp_path) is True


def test_stored_heygen_capability_invalidated_when_live_probe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5e5c round-2 (B1): a stored configured=true HeyGen payload must be
    invalidated when the live probes no longer agree, so Director re-captures
    (omitting HeyGen) instead of billing real credits on a stale snapshot."""
    from lecturecast.capabilities import capture_capabilities_v1_1
    from lecturecast.commands.director import _stored_heygen_still_live

    # A stored doc that DOES claim configured HeyGen.
    doc = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={HEYGEN_API_KEY_ENV: "sk_live"}, path_probe=Path,
        adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    assert doc.model_dump()["third_party_processors"][0]["configured"] is True

    # A doc with no HeyGen claim is always still-live (nothing to invalidate).
    plain = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
    )
    assert _stored_heygen_still_live(plain, tmp_path) is True

    # Stored doc claims configured HeyGen. Key present but the journal is now in
    # a prior-use-data-loss state (runtime/ exists, DB missing) -> live probes
    # disagree -> the snapshot is stale.
    monkeypatch.setenv(HEYGEN_API_KEY_ENV, "sk_live")
    (tmp_path / ".lecturecast" / "runtime").mkdir(parents=True)
    assert _stored_heygen_still_live(doc, tmp_path) is False

    # Key removed entirely -> also stale (M2 must not bill on the old snapshot).
    monkeypatch.delenv(HEYGEN_API_KEY_ENV, raising=False)
    assert _stored_heygen_still_live(doc, tmp_path) is False


# ---- capture_capabilities_v1_1 end-to-end (fail-closed by default) ----

def _probe_runner(args: Any) -> Any:
    import subprocess

    return subprocess.CompletedProcess(args=args, returncode=0, stdout="1.0.0", stderr="")


def test_capture_v1_1_fail_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no real F5/HeyGen adapters shipped, capture reports NEITHER — the
    server must not bill M2/M3 on an unexecutable capability."""
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={F5_MODEL_PATH_ENV: "/tmp/f5.pth", HEYGEN_API_KEY_ENV: "sk"}, path_probe=Path,
    )
    payload = caps.model_dump()
    assert isinstance(caps, ClientCapabilitiesV1_1)
    assert "f5" not in payload["tts_engines"]
    assert payload.get("third_party_processors") in (None, [])


def test_capture_v1_1_reports_f5_and_heygen_when_probes_pass(tmp_path: Path) -> None:
    model = tmp_path / "f5.pth"
    model.write_bytes(b"x")
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={F5_MODEL_PATH_ENV: str(model), HEYGEN_API_KEY_ENV: "sk_live"},
        path_probe=Path, runtime_probe=_TRUE, adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    payload = caps.model_dump()
    assert "f5" in payload["tts_engines"]
    assert payload["third_party_processors"][0]["provider"] == "heygen"
    assert payload["third_party_processors"][0]["configured"] is True
    assert "sk_live" not in repr(payload)


def test_v1_1_capabilities_round_trip_through_dispatcher() -> None:
    """A saved v1.1 capability reloads via parse_client_capabilities (not the
    v1.0 strict schema)."""
    from lecturecast.protocol import parse_client_capabilities

    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
    )
    payload = caps.model_dump()
    reloaded = parse_client_capabilities(payload)
    assert isinstance(reloaded, ClientCapabilitiesV1_1)
    assert reloaded.model_dump()["schema_version"] == "1.1"


def test_f5_fail_closed_when_file_unreadable(tmp_path: Path) -> None:
    """An unreadable model file must fail closed. Use the injected
    readable_probe (portable across platforms) rather than chmod 000."""
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(model)},
        runtime_probe=_TRUE,
        readable_probe=lambda _: False,
    ) is False


def test_stored_capabilities_version_mismatch_returns_none(tmp_path: Path) -> None:
    """_stored_capabilities returns None when the saved caps' schema_version
    doesn't match the requested protocol_version (forcing re-capture)."""
    import json

    from lecturecast.commands.director import _stored_capabilities
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.init(name="T")
    v10_caps = json.loads(
        (Path(__file__).parent / "fixtures" / "client-capabilities-v1.json").read_text(
            encoding="utf-8"
        )
    )
    from lecturecast.protocol import ClientCapabilities
    store.save_capabilities(ClientCapabilities.model_validate(v10_caps), expected_revision=1)

    # Requesting v1.1 with stored v1.0 caps → None (re-capture).
    adapter = v10_caps["adapter"]
    result = _stored_capabilities(
        store, adapter_kind=adapter["kind"], adapter_version=adapter["version"],
        protocol_version="1.1",
    )
    assert result is None

    # Requesting v1.0 with stored v1.0 caps → returns the document.
    result_v10 = _stored_capabilities(
        store, adapter_kind=adapter["kind"], adapter_version=adapter["version"],
        protocol_version="1.0",
    )
    assert result_v10 is not None
    assert result_v10.model_dump()["schema_version"] == "1.0"


def test_v1_1_brief_projectstore_round_trip(tmp_path: Path) -> None:
    """Real ProjectStore durable round-trip: init → save_brief(V1.1)
    → new instance → load → verify CreativeBriefV1_1 + digest match."""
    import json

    from lecturecast.protocol import CreativeBriefV1_1, canonical_digest, parse_creative_brief
    from lecturecast.project import ProjectStore

    brief_payload = json.loads(
        (Path(__file__).parent / "fixtures" / "creative-brief-v1_1.json").read_text(
            encoding="utf-8"
        )
    )
    brief = CreativeBriefV1_1.model_validate(brief_payload)

    store = ProjectStore(tmp_path)
    store.init(name="T")
    store.save_brief(brief, expected_revision=1)

    # New store instance reads from disk.
    store2 = ProjectStore(tmp_path)
    loaded = parse_creative_brief(
        json.loads(store2.brief_path.read_text(encoding="utf-8"))
    )
    assert isinstance(loaded, CreativeBriefV1_1)
    assert loaded.model_dump()["schema_version"] == "1.1"
    assert canonical_digest(loaded) == canonical_digest(brief)
