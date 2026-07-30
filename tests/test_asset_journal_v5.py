"""Journal v5 migration tests (§5.5e5b0c1 — asset upload table foundation).

Verifies the new heygen_asset_uploads table is created on fresh init and on
upgrade from v4, and that its closed-vocabulary / uniqueness / lease-fence
constraints enforce. The asset lifecycle is decoupled from the video operation
status machine (heygen_operations.status CHECK does not admit asset states).
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from lecturecast.heygen_journal import (
    init_database, _SCHEMA_VERSION, _migrate_v4_to_v5, _ASSET_UPLOADS_DDL,
)


def _fresh_conn():
    td = tempfile.mkdtemp()
    conn = init_database(Path(td))
    conn.row_factory = sqlite3.Row
    return conn, td


def test_schema_version_is_five():
    assert _SCHEMA_VERSION == 5


def test_fresh_init_creates_asset_uploads_table():
    conn, _ = _fresh_conn()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "heygen_asset_uploads" in tables
        cols = {c[1] for c in conn.execute(
            "PRAGMA table_info(heygen_asset_uploads)")}
        # Key fields per Codex e5b0c design.
        for col in ("upload_id", "parent_operation_id", "asset_role",
                    "content_digest", "idempotency_key", "status",
                    "remote_resource_id", "maybe_sent_at",
                    "idempotency_expires_at", "lease_owner", "lease_fence"):
            assert col in cols, f"missing column {col}"
    finally:
        conn.close()


def _insert_parent_op(conn, op_id="op_parent"):
    conn.execute(
        "INSERT INTO heygen_operations ("
        "  operation_id, kind, endpoint, generation_id,"
        "  manifest_digest, request_digest, idempotency_key, heygen_title,"
        "  created_at, updated_at) VALUES ("
        "  ?, 'video', '/v3/videos', 'gen', 'sha256:m', 'sha256:r', ?, ?, 't','t')",
        (op_id, f"idem-{op_id}", f"lc:{op_id}"),
    )


def _valid_upload_row(conn, upload_id="u1", parent="op_parent",
                      role="portrait_photo", idem="idem-1"):
    conn.execute(
        "INSERT INTO heygen_asset_uploads ("
        "  upload_id, parent_operation_id, asset_role, content_digest,"
        "  local_ref, content_type, size_bytes, provider_filename,"
        "  idempotency_key, status, created_at, updated_at"
        ") VALUES ("
        "  ?, ?, ?, 'sha256:abc', 'r/portrait.png', 'image/png', 1024,"
        "  'portrait.png', ?, 'upload_pending', 't', 't')",
        (upload_id, parent, role, idem),
    )


def test_valid_row_inserts():
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn)
        _valid_upload_row(conn)
        row = conn.execute(
            "SELECT upload_id, asset_role, status FROM heygen_asset_uploads"
        ).fetchone()
        assert row["upload_id"] == "u1"
        assert row["asset_role"] == "portrait_photo"
        assert row["status"] == "upload_pending"
    finally:
        conn.close()


@pytest.mark.parametrize("bad_role", ["portrait", "audio", "", "video"])
def test_bad_asset_role_rejected(bad_role):
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
                " asset_role, content_digest, local_ref, content_type, size_bytes,"
                " provider_filename, idempotency_key, status, created_at, updated_at)"
                " VALUES ('u','op_parent',?, 'd','r','c',1,'f','k','upload_pending','t','t')",
                (bad_role,),
            )
    finally:
        conn.close()


@pytest.mark.parametrize("bad_status", ["submit_pending", "completed", "uploaded_ok", ""])
def test_bad_status_rejected(bad_status):
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
                " asset_role, content_digest, local_ref, content_type, size_bytes,"
                " provider_filename, idempotency_key, status, created_at, updated_at)"
                " VALUES ('u','op_parent','portrait_photo','d','r','c',1,'f','k',?,'t','t')",
                (bad_status,),
            )
    finally:
        conn.close()


@pytest.mark.parametrize("bad_size", [-1, 0])
def test_non_positive_size_rejected(bad_size):
    # size_bytes CHECK > 0 — matches the adapter's non-empty-file constraint.
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id,"
                " asset_role, content_digest, local_ref, content_type, size_bytes,"
                " provider_filename, idempotency_key, status, created_at, updated_at)"
                " VALUES ('u','op_parent','portrait_photo','d','r','c',?,'f','k',"
                "         'upload_pending','t','t')",
                (bad_size,),
            )
    finally:
        conn.close()


def test_unique_parent_and_role():
    # A parent operation can have at most one of each asset_role.
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn)
        _valid_upload_row(conn, upload_id="u1", idem="k1")
        with pytest.raises(sqlite3.IntegrityError):
            _valid_upload_row(conn, upload_id="u2", idem="k2")  # same parent+role
    finally:
        conn.close()


def test_idempotency_key_unique():
    conn, _ = _fresh_conn()
    try:
        _insert_parent_op(conn, "op_a")
        _insert_parent_op(conn, "op_b")
        _valid_upload_row(conn, upload_id="u1", parent="op_a", idem="shared-key")
        with pytest.raises(sqlite3.IntegrityError):
            _valid_upload_row(conn, upload_id="u2", parent="op_b", idem="shared-key")
    finally:
        conn.close()


def test_migrate_v4_to_v5_creates_table_and_is_idempotent():
    # Simulate a v4 DB: build the v4-era tables, leave asset_uploads absent,
    # then run the v4→v5 step directly.
    conn, _ = _fresh_conn()
    try:
        conn.execute("DROP TABLE heygen_asset_uploads")
        assert "heygen_asset_uploads" not in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _migrate_v4_to_v5(conn)   # creates
        assert "heygen_asset_uploads" in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _migrate_v4_to_v5(conn)   # idempotent no-op
        assert "heygen_asset_uploads" in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_asset_uploads_ddl_is_create_if_not_exists():
    # Guard against accidentally turning the additive migration into a rebuild
    # that would drop rows on upgrade.
    assert "CREATE TABLE IF NOT EXISTS heygen_asset_uploads" in _ASSET_UPLOADS_DDL


def _make_v4_db():
    """A fresh v5 DB rewound to v4: v4-era tables populated, asset_uploads
    absent, user_version=4. Simulates an upgrade from a real v4 database."""
    conn, _ = _fresh_conn()
    # populate v4-era tables before rewinding
    _insert_parent_op(conn, "op_pop")
    conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind,"
        " remote_id, retention_mode, created_by_operation_id, created_at, updated_at) "
        "VALUES ('heygen_env_default','audio_asset','aid','ephemeral','op_pop','t','t')"
    )
    conn.commit()
    conn.execute("DROP TABLE heygen_asset_uploads")
    conn.execute("PRAGMA user_version = 4")
    return conn


def test_populated_v4_migrates_to_v5_with_data_preserved():
    import lecturecast.heygen_journal as journal
    conn = _make_v4_db()
    try:
        assert "heygen_asset_uploads" not in _tables(conn)
        journal._migrate(conn, 4)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "heygen_asset_uploads" in _tables(conn)
        # v4-era data preserved across the upgrade.
        assert conn.execute(
            "SELECT count(*) FROM heygen_operations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM heygen_remote_resources").fetchone()[0] == 1
    finally:
        conn.close()


def test_mid_migration_failure_rolls_back_fully():
    # A failure after the table is created but before user_version bump must
    # roll the whole migration back: user_version stays 4, no half table.
    import lecturecast.heygen_journal as journal

    def _create_then_fail(c):
        c.execute(_ASSET_UPLOADS_DDL)  # table created mid-transaction
        raise RuntimeError("simulated mid-migration failure")

    conn = _make_v4_db()
    orig = journal._migrate_v4_to_v5
    journal._migrate_v4_to_v5 = _create_then_fail
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            journal._migrate(conn, 4)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "heygen_asset_uploads" not in _tables(conn)  # rolled back
    finally:
        journal._migrate_v4_to_v5 = orig
        conn.close()


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
