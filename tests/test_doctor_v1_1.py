"""§5.5e5d doctor v1.1 — fresh capture + HeyGen BLOCKER/WARN decomposition.

Contracts D1-D5:
  D1  doctor computes HeyGen state from LIVE probes, never a stored cache.
  D2  the journal diagnostic is READ-ONLY (mode=ro URI; never migrates).
  D3  three journal head states distinguished: behind / current / ahead.
  D4  BLOCKER/WARN decision: key_missing + adapter_unimportable + journal_ahead
      = BLOCKER; journal_behind_head = WARN; all-pass = no blockers.
  D5  no API key value is ever leaked (only `key_present` bool).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from lecturecast.capabilities import (
    HEYGEN_API_KEY_ENV,
    build_heygen_doctor_section,
    capture_capabilities_v1_1,
    default_heygen_journal_diagnostic,
    doctor_report,
)


def _make_db(tmp_path: Path, *, head: int, real_schema: bool = True) -> Path:
    """Build a journal DB at the expected probe path with the given head.

    real_schema=True runs the SAME _DDL_STATEMENTS init_database runs (so a
    head==6 DB is column-shape-valid); False leaves the tables empty."""
    from lecturecast.heygen_journal import _DDL_STATEMENTS

    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    db = runtime / "heygen-operations.db"
    conn = sqlite3.connect(str(db))
    if real_schema:
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
    conn.executescript(f"PRAGMA user_version = {head};")
    conn.close()
    return db


def _probe_runner(args: Any) -> Any:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="1.0.0", stderr="")


def _section(
    *,
    key: str | None = "key",
    adapter_ok: bool = True,
    classification: str = "current",
    head: int | None = 6,
) -> dict:
    """Build a HeyGen doctor section with injected probes (no filesystem)."""
    env = {HEYGEN_API_KEY_ENV: key} if key else {}
    return build_heygen_doctor_section(
        env=env,
        adapter_probe=lambda: adapter_ok,
        journal_diagnostic={
            "classification": classification,
            "head": head,
            "writable": True,
        },
    )


# ----- D3: three journal head states distinguished -----

def test_journal_diagnostic_behind(tmp_path: Path) -> None:
    """D-T3a: head < _SCHEMA_VERSION -> 'behind' (needs migration; WARN)."""
    _make_db(tmp_path, head=5)
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "behind"
    assert diag["head"] == 5


def test_journal_diagnostic_current(tmp_path: Path) -> None:
    """D-T3b: head == _SCHEMA_VERSION + full schema -> 'current' (servable)."""
    _make_db(tmp_path, head=6)
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "current"
    assert diag["head"] == 6
    assert diag["writable"] is True


def test_journal_diagnostic_ahead(tmp_path: Path) -> None:
    """D-T3c: head > _SCHEMA_VERSION -> 'ahead' (refuse-downgrade; BLOCKER)."""
    _make_db(tmp_path, head=7)
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "ahead"
    assert diag["head"] == 7


# ----- D2: read-only (the diagnostic never writes / migrates) -----

def test_journal_diagnostic_readonly_no_migration(tmp_path: Path) -> None:
    """D-T2: a behind (head=5) DB is NOT migrated by the diagnostic. The
    on-disk user_version stays 5 — the diagnostic opens a mode=ro URI and
    never writes (constraint b: doctor/canary 只读不写)."""
    db = _make_db(tmp_path, head=5)
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "behind"
    # Fresh connection (the diagnostic's is closed) — assert disk unchanged.
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
    finally:
        conn.close()


def test_journal_diagnostic_never_raises_on_corrupt_db(tmp_path: Path) -> None:
    """The diagnostic's top-level backstop: a non-SQLite file cannot raise into
    the doctor health path — it returns 'unreadable' (BLOCKER downstream)."""
    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "heygen-operations.db").write_bytes(b"\x00not a sqlite database\x00")
    diag = default_heygen_journal_diagnostic(tmp_path)
    assert diag["classification"] == "unreadable"
    assert diag["head"] is None


# ----- D4: BLOCKER/WARN decision matrix -----

def test_doctor_section_all_pass_configured() -> None:
    """D4: key + adapter + journal current -> configured, no blockers/warnings,
    operations + features populated from the SAME locked surface as the payload."""
    section = _section()
    assert section["configured"] is True
    assert section["blockers"] == []
    assert section["warnings"] == []
    assert section["operations"] == [
        "direct_asset_upload",
        "photo_avatar",
        "prerecorded_audio_lipsync",
        "asset_delete",
        "video_delete",
    ]
    assert section["features"] == ["idempotency_24h", "title_query", "read_only_auth_check"]


def test_doctor_section_key_missing_blocker() -> None:
    """D4: no key -> key_missing BLOCKER. With adapter + journal otherwise OK,
    key_missing is the sole blocker (adapter_importable=True and journal=current
    are still ASSESSED for diagnostic richness, but add no blocker)."""
    section = _section(key=None)
    assert section["configured"] is False
    assert section["key_present"] is False
    assert "key_missing" in section["blockers"]
    # adapter ok + journal current -> no extra blockers.
    assert "adapter_unimportable" not in section["blockers"]
    assert all(not b.startswith("journal_") for b in section["blockers"])


def test_doctor_section_routes_all_blockers_independently() -> None:
    """D4 (round-1 fix): blockers are routed INDEPENDENTLY — a missing key must
    NOT suppress adapter/journal blockers. key missing + adapter broken +
    journal ahead -> ALL THREE surfaced in one doctor pass (the complete fix
    list), not just the first. configured is still False (AND of all three)."""
    section = _section(key=None, adapter_ok=False, classification="ahead", head=7)
    assert section["configured"] is False
    assert "key_missing" in section["blockers"]
    assert "adapter_unimportable" in section["blockers"]
    assert "journal_ahead" in section["blockers"]


def test_doctor_section_adapter_unimportable_blocker() -> None:
    """D4: key present but adapter_probe False -> adapter_unimportable BLOCKER."""
    section = _section(adapter_ok=False)
    assert section["configured"] is False
    assert section["adapter_importable"] is False
    assert "adapter_unimportable" in section["blockers"]


def test_doctor_section_journal_ahead_blocker() -> None:
    """D4: key+adapter OK, journal ahead -> journal_ahead BLOCKER (client older
    than the on-disk journal; refuse-downgrade)."""
    section = _section(classification="ahead", head=7)
    assert section["configured"] is False
    assert "journal_ahead" in section["blockers"]


def test_doctor_section_journal_behind_warning_not_blocker() -> None:
    """D4: key+adapter OK, journal behind -> journal_behind_head WARN (needs
    migration), NOT a blocker (a legit upgrade path exists via §5.5e5d)."""
    section = _section(classification="behind", head=5)
    assert section["configured"] is False
    assert "journal_behind_head" in section["warnings"]
    assert all(not b.startswith("journal_") for b in section["blockers"])


# ----- D1: LIVE probes win over a stale stored cache -----

def test_doctor_section_ignores_stored_cache(tmp_path: Path) -> None:
    """D1: build_heygen_doctor_section computes from LIVE probes. A stale
    client-capabilities.json on disk claiming NO HeyGen does NOT sway the
    section when the live key + adapter + a fresh journal say configured — the
    cache file is never read for the configured decision."""
    lecturecast_dir = tmp_path / ".lecturecast"
    lecturecast_dir.mkdir()
    # Stale snapshot: unconfigured (empty third_party_processors).
    (lecturecast_dir / "client-capabilities.json").write_text(
        json.dumps({"schema_version": "1.1", "third_party_processors": []})
    )
    section = build_heygen_doctor_section(
        env={HEYGEN_API_KEY_ENV: "live_key"},
        project_root=tmp_path,
        adapter_probe=lambda: True,
    )
    # No journal DB + no prior-use signal -> 'fresh' (first init OK) ->
    # journal_ready True. Live probes win; configured is True despite the
    # stale unconfigured cache snapshot on disk.
    assert section["journal"]["classification"] == "fresh"
    assert section["configured"] is True
    assert section["blockers"] == []


# ----- D5: no API key value leak -----

def test_doctor_section_no_key_value_leak() -> None:
    """D5: the API key value never appears in the section — only the
    `key_present` bool. Doctor output is safe to log / show the user."""
    secret = "sk_super_secret_value_12345"
    section = build_heygen_doctor_section(
        env={HEYGEN_API_KEY_ENV: secret},
        adapter_probe=lambda: True,
        journal_diagnostic={"classification": "current", "head": 6, "writable": True},
    )
    assert secret not in json.dumps(section)
    assert section["key_present"] is True


# ----- doctor_report integration: v1.1 third_party section -----

def test_doctor_report_v1_1_includes_third_party_section() -> None:
    """doctor_report includes the HeyGen section under `third_party` for a v1.1
    payload. top-level `ready` stays M1-runtime-only (M1-independence: a user
    without HeyGen is still `ready` for the base-video path)."""
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        adapter_probe=lambda: True,
        journal_probe=lambda: True,
        env={HEYGEN_API_KEY_ENV: "key"},
    )
    section = build_heygen_doctor_section(
        env={HEYGEN_API_KEY_ENV: "key"},
        adapter_probe=lambda: True,
        journal_diagnostic={"classification": "current", "head": 6, "writable": True},
    )
    report = doctor_report(caps, heygen_section=section)
    assert report["third_party"] == section
    assert isinstance(report["ready"], bool)
    # configured in the section agrees with the payload's third_party_processors
    # (both fresh, both True).
    assert report["third_party"]["configured"] is True
