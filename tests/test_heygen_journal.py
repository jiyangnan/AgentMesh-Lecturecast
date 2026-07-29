"""HeyGen journal SQLite schema contract tests (§5.5e1)."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from lecturecast.heygen_journal import init_database, _SCHEMA_VERSION

T = "2026-07-29T00:00:00Z"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A minimal project dir with a real .lecturecast structure."""
    (tmp_path / ".lecturecast").mkdir(parents=True)
    return tmp_path


def _open(project: Path) -> sqlite3.Connection:
    return init_database(project)


def _insert_op(conn, op_id="op_1", idem="idem_1", title="lecturecast:op_1") -> None:
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
        "manifest_digest, request_digest, idempotency_key, heygen_title, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (op_id, "video", "/v3/videos", "gen_1", "sha256:x", "sha256:r",
         idem, title, T, T),
    )


def _insert_receipt(conn, *, receipt_id="rd_1", op_id="op_1", assets="[]",
                    cats="[]", status=None) -> None:
    cols = ["receipt_digest", "operation_id", "disclosure_version", "generation_id",
            "provider", "operation_kind", "disclosed_assets_json",
            "data_categories_json", "created_at"]
    vals = [receipt_id, op_id, "v1", "gen_1", "heygen", "video", assets, cats, T]
    if status is not None:
        cols.append("status")
        vals.append(status)
    placeholders = ",".join(["?"] * len(cols))
    conn.execute(
        f"INSERT INTO heygen_consent_receipts ({','.join(cols)}) VALUES ({placeholders})",
        vals,
    )


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
    _insert_op(conn1, op_id="op_1")
    conn1.close()
    # Re-init must not destroy data.
    conn2 = _open(project)
    row = conn2.execute(
        "SELECT operation_id FROM heygen_operations WHERE operation_id = ?", ("op_1",)
    ).fetchone()
    assert row is not None
    assert row[0] == "op_1"
    conn2.close()


def test_reopen_via_init_reconfigures_pragmas(project: Path):
    """Every init-returned connection must have FK/WAL/busy_timeout configured,
    not rely on the caller setting PRAGMAs. FK and busy_timeout are
    per-connection, so they must be re-applied on each open."""
    _open(project).close()
    conn = _open(project)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_init_rejects_future_user_version(project: Path):
    conn = _open(project)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        _open(project)


# ---- file permissions ----

def test_init_sets_runtime_dir_mode_0700(project: Path):
    _open(project).close()
    runtime = project / ".lecturecast" / "runtime"
    assert stat.S_IMODE(os.stat(runtime).st_mode) == 0o700


def test_init_sets_db_and_sidecar_modes_0600(project: Path):
    conn = _open(project)
    runtime = project / ".lecturecast" / "runtime"
    db = runtime / "heygen-operations.db"
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    # WAL/SHM materialize once the connection writes; tighten + check if present.
    for sidecar in ("heygen-operations.db-wal", "heygen-operations.db-shm"):
        p = runtime / sidecar
        if p.exists():
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    conn.close()


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
             "idem_1", "t", "bogus_status", T, T),
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
             "idem_1", "t", "bogus", T, T),
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
             "idem_1", "t", -1, T, T),
        )
    conn.close()


def test_consent_status_check(project: Path):
    conn = _open(project)
    _insert_op(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, status="bogus")
    conn.close()


def test_consent_assets_json_must_be_valid(project: Path):
    conn = _open(project)
    _insert_op(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, assets="not valid json")
    conn.close()


def test_consent_assets_json_must_be_array(project: Path):
    conn = _open(project)
    _insert_op(conn)
    # Valid JSON but an object, not an array → rejected.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, assets='{"a": 1}')
    conn.close()


def test_consent_data_categories_json_must_be_array(project: Path):
    conn = _open(project)
    _insert_op(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, cats='"a-string"')
    conn.close()


def test_remote_resource_kind_check(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
            "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("cp_1", "bogus_kind", "rem_1", T, T),
        )
    conn.close()


def test_remote_resource_deletion_status_check(project: Path):
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
            "remote_id, deletion_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("cp_1", "video", "rem_1", "bogus", T, T),
        )
    conn.close()


def test_remote_resource_credential_profile_not_null(project: Path):
    """credential_profile_id is NOT NULL: SQLite UNIQUE does not dedupe NULLs,
    so a nullable profile column would let the same remote resource be inserted
    twice with NULL profile. NOT NULL closes that hole."""
    conn = _open(project)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (resource_kind, remote_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("video", "rem_1", T, T),
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
         "same_key", "lecturecast:op_1", T, T),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id, "
            "manifest_digest, request_digest, idempotency_key, heygen_title, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("op_3", "video", "/v3/videos", "gen_1", "sha256:z", "sha256:rz",
             "same_key", "lecturecast:op_3", T, T),
        )
    conn.close()


def test_remote_resource_unique_triplet(project: Path):
    conn = _open(project)
    conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("cp_1", "video", "rem_1", T, T),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
            "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("cp_1", "video", "rem_1", T, T),
        )
    conn.close()


def test_remote_resource_id_autoincrements(project: Path):
    conn = _open(project)
    cur1 = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("cp_1", "video", "rem_1", T, T),
    )
    cur2 = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("cp_1", "audio_asset", "rem_2", T, T),
    )
    assert cur1.lastrowid == 1
    assert cur2.lastrowid == 2
    conn.close()


def test_consent_receipt_unique_operation(project: Path):
    conn = _open(project)
    _insert_op(conn)
    _insert_receipt(conn, receipt_id="rd_1")
    # Second receipt for same operation → UNIQUE violation.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_receipt(conn, receipt_id="rd_2")
    conn.close()


def test_refs_composite_pk(project: Path):
    conn = _open(project)
    _insert_op(conn)
    cur = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("cp_1", "video", "rem_1", T, T),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
        "VALUES (?, ?, ?)",
        (rid, "op_1", T),
    )
    # Duplicate ref → composite PK violation.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
            "VALUES (?, ?, ?)",
            (rid, "op_1", T),
        )
    conn.close()


def test_operation_delete_restricted_by_receipt(project: Path):
    conn = _open(project)
    _insert_op(conn)
    _insert_receipt(conn)
    # Deleting operation with existing receipt → RESTRICT.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM heygen_operations WHERE operation_id = 'op_1'")
    conn.close()


def test_operation_delete_sets_resource_null_and_cascades_refs(project: Path):
    conn = _open(project)
    _insert_op(conn)
    cur = conn.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_by_operation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("cp_1", "video", "rem_1", "op_1", T, T),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
        "VALUES (?, ?, ?)",
        (rid, "op_1", T),
    )
    # Delete operation (no receipt → no RESTRICT) → resource.created_by_op NULL, ref CASCADE.
    conn.execute("DELETE FROM heygen_operations WHERE operation_id = 'op_1'")
    resource = conn.execute(
        "SELECT created_by_operation_id FROM heygen_remote_resources WHERE resource_id = ?",
        (rid,),
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


# ---- atomic migration ----

def test_migration_is_atomic_on_failure(tmp_path: Path, monkeypatch):
    """If the migration fails partway, the transaction rolls back: no partial
    schema and user_version stays 0. A later init can retry cleanly."""
    import lecturecast.heygen_journal as hj

    # First statement creates heygen_operations; the second is invalid SQL that
    # forces a mid-migration failure inside the same BEGIN/COMMIT.
    monkeypatch.setattr(hj, "_DDL_STATEMENTS", [
        hj._DDL_STATEMENTS[0],
        "THIS IS INTENTIONALLY INVALID SQL TO TRIGGER ROLLBACK",
    ])

    project = tmp_path  # no pre-existing .lecturecast
    with pytest.raises(sqlite3.OperationalError):
        hj.init_database(project)

    db = project / ".lecturecast" / "runtime" / "heygen-operations.db"
    assert db.exists()  # file created, but…
    raw = sqlite3.connect(str(db))
    tables = raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    assert tables == []  # …nothing committed
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    raw.close()


# ---- symlink rejection ----

def test_symlink_db_rejected(project: Path):
    db_path = project / ".lecturecast" / "runtime" / "heygen-operations.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    real_db = project / ".lecturecast" / "real.db"
    real_db.write_bytes(b"")
    os.symlink(real_db, db_path)
    with pytest.raises((ValueError, RuntimeError)):
        _open(project)


def test_symlink_runtime_dir_rejected(project: Path, tmp_path: Path):
    fake_runtime = tmp_path / "fake_runtime"
    fake_runtime.mkdir()
    real_runtime = project / ".lecturecast" / "runtime"
    real_runtime.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(fake_runtime, real_runtime)
    with pytest.raises((ValueError, RuntimeError)):
        _open(project)


def test_symlink_lecturecast_dir_rejected_and_target_untouched(tmp_path: Path):
    """A symlinked .lecturecast must be rejected BEFORE mkdir/chmod run, so the
    symlink target's permissions and contents are never modified."""
    project = tmp_path / "proj"
    project.mkdir()
    real_target = tmp_path / "real_lecturecast"
    real_target.mkdir()
    real_target.chmod(0o755)
    (real_target / "sentinel").write_text("keep-me")
    os.symlink(real_target, project / ".lecturecast")

    with pytest.raises((ValueError, RuntimeError)):
        init_database(project)

    # Target untouched: content preserved, mode unchanged, no runtime created.
    assert (real_target / "sentinel").read_text() == "keep-me"
    assert stat.S_IMODE(os.stat(real_target).st_mode) == 0o755
    assert not (real_target / "runtime").exists()
