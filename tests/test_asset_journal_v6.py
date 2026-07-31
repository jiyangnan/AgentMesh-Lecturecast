"""Journal v6 migration tests (§5.5e5b0c3b — asset cleanup status).

v6 extends heygen_asset_uploads.status with cleanup_required + deleted. SQLite
cannot ALTER a CHECK, so the migration rebuilds the table. These tests verify
the rebuild preserves data, the CHECK is extended, fresh installs use the
latest DDL directly (no double rebuild), and a mid-rebuild failure rolls back
to v5.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from lecturecast.heygen_journal import (
    init_database, _SCHEMA_VERSION, _migrate, _ASSET_UPLOADS_DDL,
)


# The v5-era DDL: the asset_uploads CHECK WITHOUT cleanup_required/deleted.
# Used to build a real v5 table for the upgrade test.
_V5_ASSET_DDL = """
    CREATE TABLE heygen_asset_uploads (
        upload_id TEXT PRIMARY KEY NOT NULL,
        parent_operation_id TEXT NOT NULL,
        asset_role TEXT NOT NULL
            CHECK (asset_role IN ('portrait_photo', 'synthetic_narration_audio')),
        content_digest TEXT NOT NULL,
        local_ref TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
        provider_filename TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'upload_pending'
            CHECK (status IN (
                'upload_pending', 'uploading', 'uploaded',
                'reconciliation_required', 'manual_reconciliation_required',
                'failed', 'cancelled'
            )),
        remote_resource_id INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        next_retry_at TEXT,
        last_error_code TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
        attempt_started_at TEXT,
        maybe_sent_at TEXT,
        idempotency_expires_at TEXT,
        uploaded_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(parent_operation_id, asset_role),
        FOREIGN KEY (parent_operation_id)
            REFERENCES heygen_operations(operation_id) ON DELETE CASCADE,
        FOREIGN KEY (remote_resource_id)
            REFERENCES heygen_remote_resources(resource_id) ON DELETE SET NULL
    )
    """


def _fresh():
    td = tempfile.mkdtemp()
    conn = init_database(Path(td))
    conn.row_factory = sqlite3.Row
    return conn, td


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_six():
    assert _SCHEMA_VERSION == 6


def test_fresh_install_admits_cleanup_and_deleted_status():
    conn, _ = _fresh()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        for st in ("cleanup_required", "deleted"):
            # insert a parent op first to satisfy FK + UNIQUE
            conn.execute(
                "INSERT INTO heygen_operations (operation_id, kind, endpoint, "
                "generation_id, manifest_digest, request_digest, idempotency_key, "
                "heygen_title, credential_profile_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"op_{st}", "video", "/v3/videos", "g", "sha256:m", "sha256:r",
                 f"i_{st}", f"lc:op_{st}", "heygen_env_default", "t", "t"))
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, "
                "asset_role, content_digest, local_ref, content_type, size_bytes, "
                "provider_filename, idempotency_key, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"u_{st}", f"op_{st}", "portrait_photo", "sha256:d", "r/p",
                 "image/png", 10, "portrait.png", f"k_{st}", st, "t", "t"))
        # a bogus status is still rejected
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, "
                "asset_role, content_digest, local_ref, content_type, size_bytes, "
                "provider_filename, idempotency_key, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("u_bad", "op_cleanup_required", "portrait_photo", "sha256:d",
                 "r/p", "image/png", 10, "portrait.png", "k_bad", "bogus", "t", "t"))
    finally:
        conn.close()


def test_populated_v5_upgrades_to_v6_data_preserved():
    conn, _ = _fresh()
    try:
        # Rewind to a REAL v5 asset_uploads table (old CHECK) with a row.
        conn.execute("DROP TABLE heygen_asset_uploads")
        conn.execute(_V5_ASSET_DDL)
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, "
            "generation_id, manifest_digest, request_digest, idempotency_key, "
            "heygen_title, credential_id_x, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)".replace("credential_id_x", "credential_profile_id"),
            ("op_x", "video", "/v3/videos", "g", "sha256:m", "sha256:r", "i_x",
             "lc:op_x", "heygen_env_default", "t", "t"))
        conn.execute(
            "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, "
            "asset_role, content_digest, local_ref, content_type, size_bytes, "
            "provider_filename, idempotency_key, status, attempts, lease_fence, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'t','t')",
            ("u_keep", "op_x", "portrait_photo", "sha256:d", "r/p", "image/png",
             10, "portrait.png", "k_keep", "uploaded", 3, 2))
        conn.commit()
        conn.execute("PRAGMA user_version = 5")

        _migrate(conn, 5)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "heygen_asset_uploads" in _tables(conn)
        # Row + its data preserved across the rebuild.
        row = conn.execute(
            "SELECT upload_id, status, attempts, lease_fence, content_digest "
            "FROM heygen_asset_uploads WHERE upload_id=?", ("u_keep",)).fetchone()
        assert row["upload_id"] == "u_keep"
        assert row["status"] == "uploaded"
        assert row["attempts"] == 3 and row["lease_fence"] == 2
        # CHECK extended: cleanup_required now admissible.
        conn.execute(
            "UPDATE heygen_asset_uploads SET status='cleanup_required' "
            "WHERE upload_id=?", ("u_keep",))
        assert conn.execute(
            "SELECT status FROM heygen_asset_uploads WHERE upload_id=?",
            ("u_keep",)).fetchone()[0] == "cleanup_required"
        # No leftover _new table.
        assert "heygen_asset_uploads_new" not in _tables(conn)
    finally:
        conn.close()


def test_rebuild_failure_rolls_back_to_v5():
    import lecturecast.heygen_journal as journal
    calls = {"n": 0}

    def _partial(c):
        # Create the _new table then fail BEFORE the swap → must roll back.
        c.execute(_ASSET_UPLOADS_DDL.replace(
            "CREATE TABLE IF NOT EXISTS heygen_asset_uploads",
            "CREATE TABLE heygen_asset_uploads_new"))
        raise RuntimeError("simulated mid-rebuild failure")

    conn, _ = _fresh()
    # rewind to v5 table
    conn.execute("DROP TABLE heygen_asset_uploads")
    conn.execute(_V5_ASSET_DDL)
    conn.commit()
    conn.execute("PRAGMA user_version = 5")
    orig = journal._migrate_v5_to_v6
    journal._migrate_v5_to_v6 = _partial
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            journal._migrate(conn, 5)
        # full rollback: version still 5, original table intact, no _new leftover
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "heygen_asset_uploads" in _tables(conn)
        assert "heygen_asset_uploads_new" not in _tables(conn)
    finally:
        journal._migrate_v5_to_v6 = orig
        conn.close()
