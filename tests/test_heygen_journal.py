"""HeyGen journal SQLite schema contract tests (§5.5e1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lecturecast.heygen_journal import init_database, _SCHEMA_VERSION


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A minimal project dir with .lecturecast structure."""
    (tmp_path / ".lecturecast").mkdir(parents=True)
    return tmp_path


def _open(project: Path) -> sqlite3.Connection:
    return init_database(project)


# ---- initialization ----

def test_init_creates_db_and_runtime_dir(project: Path):
    conn = _open(project)
    db_path = project / ".lecturecast" / "runtime" / "heygen-operations.db"
    assert db_path.is_file()
    assert (project / ".lecturecast" / "runtime").is_dir()
    conn.close()


def test_init_sets_user_version(project: Path):
    conn = _open(project)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION
    conn.close()


def test_init_enables_wal(project: Path):
    conn = _open(project)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()


def test_init_enables_foreign_keys(project: Path):
    conn = _open(project)
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


def test_init_sets_busy_timeout(project: Path):
    conn = _open(project)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 5000
    conn.close()


def test_init_is_idempotent(project: Path):
    conn1 = _open(project)
    conn1.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "lecturecast:op_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn1.commit()
    conn1.close()
    # Re-init must not destroy data.
    conn2 = _open(project)
    row = conn2.execute(
        "SELECT operation_id FROM heygen_operations WHERE operation_id = ?", ("op_1",)
    ).fetchone()
    assert row is not None
    assert row[0] == "op_1"
    conn2.close()


def test_init_rejects_future_user_version(project: Path):
    conn = _open(project)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        _open(project)


# ---- four tables exist ----

@pytest.mark.parametrize("table", [
    "heygen_operations",
    "heygen_consent_receipts",
    "heygen_remote_resources",
    "heygen_resource_operation_refs",
])
def test_table_exists(project: Path, table: str):
    conn = _open(project)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchall()
    assert len(rows) == 1
    conn.close()


# ---- CHECK constraints ----

def test_operations_status_check_rejects_invalid(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
            "manifest_digest, request_digest, idempotency_key, heygen_title, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
             "idem_1", "t", "bogus_status", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_operations_download_status_check(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
            "manifest_digest, request_digest, idempotency_key, heygen_title, download_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
             "idem_1", "t", "bogus", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_operations_negative_attempts_rejected(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
            "manifest_digest, request_digest, idempotency_key, heygen_title, submit_attempts, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
             "idem_1", "t", -1, "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_consent_status_check(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "t", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id, "
            "disclosure_version, generation_id, provider, operation_kind, "
            "disclosed_assets_json, data_categories_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("rd_1", "op_1", "v1", "gen_1", "heygen", "video",
             "[]", "[]", "bogus", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_remote_resource_kind_check(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (resource_id, resource_kind, remote_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("res_1", "bogus_kind", "rem_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_remote_resource_deletion_status_check(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (resource_id, resource_kind, remote_id, "
            "deletion_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("res_1", "video", "rem_1", "bogus", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


# ---- UNIQUE + FK constraints ----

def test_operations_idempotency_key_unique(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:0", "sha256:r0",
         "same_key", "lecturecast:op_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
            "manifest_digest, request_digest, idempotency_key, heygen_title, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op_3", "video", "/v3/videos", "gen_1", "sha256:z", "sha256:rz",
             "same_key", "lecturecast:op_3", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_remote_resource_unique_triplet(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_remote_resources (resource_id, credential_profile_id, "
        "resource_kind, remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("res_1", "cp_1", "video", "rem_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (resource_id, credential_profile_id, "
            "resource_kind, remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("res_2", "cp_1", "video", "rem_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_consent_receipt_unique_operation(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "t", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id, "
        "disclosure_version, generation_id, provider, operation_kind, "
        "disclosed_assets_json, data_categories_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("rd_1", "op_1", "v1", "gen_1", "heygen", "video", "[]", "[]", "2026-07-29T00:00:00Z"),
    )
    # Second receipt for same operation → UNIQUE violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id, "
            "disclosure_version, generation_id, provider, operation_kind, "
            "disclosed_assets_json, data_categories_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("rd_2", "op_1", "v1", "gen_1", "heygen", "video", "[]", "[]", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_refs_composite_pk(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "t", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_remote_resources (resource_id, resource_kind, remote_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("res_1", "video", "rem_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
        "VALUES (?, ?, ?)",
        ("res_1", "op_1", "2026-07-29T00:00:00Z"),
    )
    # Duplicate ref → composite PK violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
            "VALUES (?, ?, ?)",
            ("res_1", "op_1", "2026-07-29T00:00:00Z"),
        )
    conn.close()


def test_operation_delete_restricted_by_receipt(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "t", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id, "
        "disclosure_version, generation_id, provider, operation_kind, "
        "disclosed_assets_json, data_categories_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("rd_1", "op_1", "v1", "gen_1", "heygen", "video", "[]", "[]", "2026-07-29T00:00:00Z"),
    )
    # Deleting operation with existing receipt → RESTRICT.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM heygen_operations WHERE operation_id = 'op_1'")
    conn.close()


def test_operation_delete_sets_resource_null_and_cascades_refs(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("op_1", "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         "idem_1", "t", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_remote_resources (resource_id, resource_kind, remote_id, "
        "created_by_operation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("res_1", "video", "rem_1", "op_1", "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
        "VALUES (?, ?, ?)",
        ("res_1", "op_1", "2026-07-29T00:00:00Z"),
    )
    # Delete operation (no receipt → no RESTRICT) → resource.created_by_op NULL, ref CASCADE.
    conn.execute("DELETE FROM heygen_operations WHERE operation_id = 'op_1'")
    resource = conn.execute(
        "SELECT created_by_operation_id FROM heygen_remote_resources WHERE resource_id = 'res_1'"
    ).fetchone()
    assert resource[0] is None
    refs = conn.execute(
        "SELECT COUNT(*) FROM heygen_resource_operation_refs WHERE operation_id = 'op_1'"
    ).fetchone()
    assert refs[0] == 0
    conn.close()


# ---- second connection ----

def test_second_connection_has_fk_and_wal(project: Path):
    _open(project).close()
    db_path = project / ".lecturecast" / "runtime" / "heygen-operations.db"
    conn2 = sqlite3.connect(str(db_path))
    conn2.execute("PRAGMA foreign_keys = ON")
    fk = conn2.execute("PRAGMA foreign_keys").fetchone()[0]
    mode = conn2.execute("PRAGMA journal_mode").fetchone()[0]
    assert fk == 1
    assert mode.lower() == "wal"
    conn2.close()


# ---- symlink rejection ----

def test_symlink_db_rejected(project: Path):
    import os
    db_path = project / ".lecturecast" / "runtime" / "heygen-operations.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    real_db = project / ".lecturecast" / "real.db"
    real_db.write_bytes(b"")
    os.symlink(real_db, db_path)
    with pytest.raises((ValueError, RuntimeError)):
        _open(project)


def test_symlink_runtime_dir_rejected(project: Path, tmp_path: Path):
    import os
    fake_runtime = tmp_path / "fake_runtime"
    fake_runtime.mkdir()
    real_runtime = project / ".lecturecast" / "runtime"
    real_runtime.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(fake_runtime, real_runtime)
    with pytest.raises((ValueError, RuntimeError)):
        _open(project)
