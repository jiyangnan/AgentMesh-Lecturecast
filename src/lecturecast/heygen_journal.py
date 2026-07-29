"""HeyGen operation journal — SQLite versioned migration + four tables (§5.5e1/e2a).

A per-project local database at <project>/.lecturecast/runtime/heygen-operations.db
that provides idempotent third-party operation tracking. It NEVER touches the
shared Core, never crosses product boundaries, and never stores API keys,
media content, or absolute paths.

Four tables:
- heygen_operations: deterministic operation lifecycle (submit → reconcile → complete/cancel)
- heygen_consent_receipts: JIT consent proof (granted/declined/withdrawn). v2 binds each
  receipt directly to the request it authorizes (request_digest) and to the first-gate
  CreativeBrief consent (creative_brief_digest).
- heygen_remote_resources: remote HeyGen assets with retention + deletion tracking
- heygen_resource_operation_refs: many-to-many resource↔operation links

All timestamps are ISO-8601 UTC strings. All status fields use CHECK constraints.
Migrations are versioned and atomic: each user_version step (and the user_version bump)
commit in one BEGIN IMMEDIATE transaction, or nothing does.
"""
from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path


_SCHEMA_VERSION = 4
_RUNTIME_DIR_NAME = "runtime"
_DB_NAME = "heygen-operations.db"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _is_symlink(p: Path) -> bool:
    """True only if the path itself (lstat, not following) is a symlink.
    Missing paths are not symlinks."""
    try:
        return stat.S_ISLNK(p.lstat().st_mode)
    except (FileNotFoundError, OSError):
        return False


def _reject_symlink_components(components, *, context: str) -> None:
    """Raise ValueError if any path component is itself a symlink. Prevents
    symlink-swap and path-traversal: every component of the chain (.lecturecast,
    runtime, db) must be a real directory/file we control."""
    for p in components:
        if _is_symlink(p):
            raise ValueError(f"{context} must not be a symlink: {p}")


# --- DDL (latest schema snapshot / fresh install) ---------------------
# These statements create the current (v2) shape directly on a fresh install.
# Individual statements (not one script) so they execute inside one explicit
# BEGIN/COMMIT. sqlite3.executescript() issues an implicit COMMIT that would
# break transactional migration. Existing v1 databases are upgraded to this
# shape by _migrate_v1_to_v2.

_OPERATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS heygen_operations (
        operation_id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        segment_id TEXT,
        generation_id TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        orchestration_plan_digest TEXT,
        request_digest TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        heygen_title TEXT NOT NULL UNIQUE,
        credential_profile_id TEXT,
        consent_receipt_digest TEXT,
        status TEXT NOT NULL DEFAULT 'submit_pending'
            CHECK (status IN (
                'submit_pending', 'submitted', 'processing', 'completed',
                'failed', 'cancelled', 'reconciliation_required'
            )),
        provider_status TEXT,
        download_status TEXT NOT NULL DEFAULT 'not_started'
            CHECK (download_status IN (
                'not_started', 'downloading', 'downloaded', 'verified', 'failed'
            )),
        local_output_ref TEXT,
        local_output_digest TEXT,
        download_verified_at TEXT,
        submit_attempts INTEGER NOT NULL DEFAULT 0 CHECK (submit_attempts >= 0),
        reconcile_attempts INTEGER NOT NULL DEFAULT 0 CHECK (reconcile_attempts >= 0),
        download_attempts INTEGER NOT NULL DEFAULT 0 CHECK (download_attempts >= 0),
        next_retry_at TEXT,
        last_error_code TEXT,
        lease_owner TEXT,
        lease_expires_at TEXT,
        lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
        attempt_started_at TEXT,
        submitted_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """

# v2: receipts bind directly to the authorized request (request_digest) and to the
# first-gate CreativeBrief consent (creative_brief_digest). Both NOT NULL — a receipt
# must independently prove which request it authorizes, not just via the operation.
_RECEIPTS_DDL = """
    CREATE TABLE IF NOT EXISTS heygen_consent_receipts (
        receipt_digest TEXT PRIMARY KEY NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        disclosure_version TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        creative_brief_digest TEXT NOT NULL,
        provider TEXT NOT NULL,
        operation_kind TEXT NOT NULL,
        disclosed_assets_json TEXT NOT NULL
            CHECK (json_valid(disclosed_assets_json)
                   AND json_type(disclosed_assets_json) = 'array'),
        data_categories_json TEXT NOT NULL
            CHECK (json_valid(data_categories_json)
                   AND json_type(data_categories_json) = 'array'),
        provider_cost_disclosure TEXT NOT NULL,
        agentmesh_non_processor_disclosure TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'granted'
            CHECK (status IN ('granted', 'declined', 'withdrawn')),
        consented_at TEXT,
        withdrawn_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (operation_id)
            REFERENCES heygen_operations(operation_id)
            ON DELETE RESTRICT
    )
    """

_RESOURCES_DDL = """
    CREATE TABLE IF NOT EXISTS heygen_remote_resources (
        resource_id INTEGER PRIMARY KEY,
        credential_profile_id TEXT NOT NULL,
        resource_kind TEXT NOT NULL
            CHECK (resource_kind IN (
                'video', 'audio_asset', 'portrait_asset',
                'avatar_look', 'avatar_group'
            )),
        remote_id TEXT NOT NULL,
        retention_mode TEXT NOT NULL DEFAULT 'ephemeral'
            CHECK (retention_mode IN ('ephemeral', 'reusable_avatar')),
        created_by_operation_id TEXT,
        deletion_status TEXT NOT NULL DEFAULT 'not_started'
            CHECK (deletion_status IN (
                'not_started', 'deletion_pending', 'deleted', 'deletion_failed'
            )),
        deletion_attempts INTEGER NOT NULL DEFAULT 0 CHECK (deletion_attempts >= 0),
        deletion_next_retry_at TEXT,
        deletion_reason TEXT
            CHECK (deletion_reason IS NULL OR deletion_reason IN (
                'post_download', 'consent_withdrawal', 'manual_force'
            )),
        last_deletion_error TEXT,
        deleted_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(credential_profile_id, resource_kind, remote_id),
        FOREIGN KEY (created_by_operation_id)
            REFERENCES heygen_operations(operation_id)
            ON DELETE SET NULL
    )
    """

_REFS_DDL = """
    CREATE TABLE IF NOT EXISTS heygen_resource_operation_refs (
        resource_id INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (resource_id, operation_id),
        FOREIGN KEY (resource_id)
            REFERENCES heygen_remote_resources(resource_id)
            ON DELETE CASCADE,
        FOREIGN KEY (operation_id)
            REFERENCES heygen_operations(operation_id)
            ON DELETE CASCADE
    )
    """

_DDL_STATEMENTS = [
    _OPERATIONS_DDL,
    _RECEIPTS_DDL,
    _RESOURCES_DDL,
    _REFS_DDL,
    "CREATE INDEX IF NOT EXISTS idx_operations_generation ON heygen_operations(generation_id)",
    "CREATE INDEX IF NOT EXISTS idx_operations_status ON heygen_operations(status)",
    "CREATE INDEX IF NOT EXISTS idx_remote_resources_created_by_op ON heygen_remote_resources(created_by_operation_id)",
]


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: add request_digest + creative_brief_digest (NOT NULL) to
    heygen_consent_receipts. SQLite cannot ADD COLUMN NOT NULL without a
    default, so we rebuild the (empty) table. Fail-closed if rows exist —
    a populated v1 receipts table cannot be auto-migrated and must be handled
    deliberately. On a fresh DB the columns already exist and this is a no-op."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(heygen_consent_receipts)")}
    if {"request_digest", "creative_brief_digest"}.issubset(cols):
        return  # already v2-shaped
    existing = conn.execute(
        "SELECT COUNT(*) FROM heygen_consent_receipts"
    ).fetchone()[0]
    if existing:
        raise RuntimeError(
            f"heygen_consent_receipts has {existing} row(s) lacking request_digest/"
            f"creative_brief_digest; v2 migration is fail-closed on non-empty tables"
        )
    conn.execute("DROP TABLE heygen_consent_receipts")
    conn.execute(_RECEIPTS_DDL)


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: add operations.download_attempts (NOT NULL DEFAULT 0) and
    heygen_remote_resources.deletion_reason (nullable, closed vocabulary).
    ADD COLUMN with a CHECK-satisfying default is safe on a populated table."""
    op_cols = {row[1] for row in conn.execute("PRAGMA table_info(heygen_operations)")}
    if "download_attempts" not in op_cols:
        conn.execute(
            "ALTER TABLE heygen_operations ADD COLUMN download_attempts "
            "INTEGER NOT NULL DEFAULT 0 CHECK (download_attempts >= 0)"
        )
    res_cols = {row[1] for row in conn.execute("PRAGMA table_info(heygen_remote_resources)")}
    if "deletion_reason" not in res_cols:
        conn.execute(
            "ALTER TABLE heygen_remote_resources ADD COLUMN deletion_reason TEXT "
            "CHECK (deletion_reason IS NULL OR deletion_reason IN "
            "('post_download', 'consent_withdrawal', 'manual_force'))"
        )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v3 → v4: add heygen_remote_resources.deletion_next_retry_at (nullable)
    for per-resource deletion backoff, independent of the operation-level
    next_retry_at shared by poll/reconcile."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(heygen_remote_resources)")}
    if "deletion_next_retry_at" not in cols:
        conn.execute(
            "ALTER TABLE heygen_remote_resources ADD COLUMN deletion_next_retry_at TEXT"
        )


def _migrate(conn: sqlite3.Connection, current_version: int) -> None:
    """Run all version steps < _SCHEMA_VERSION, then bump user_version, in one
    BEGIN IMMEDIATE transaction. All-or-nothing: on any failure the whole
    migration rolls back, leaving the prior schema and user_version intact."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if current_version < 1:
            for stmt in _DDL_STATEMENTS:
                conn.execute(stmt)
        if current_version < 2:
            _migrate_v1_to_v2(conn)
        if current_version < 3:
            _migrate_v2_to_v3(conn)
        if current_version < 4:
            _migrate_v3_to_v4(conn)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _chmod_secure(db_path: Path) -> None:
    """Tighten the DB file and its WAL/SHM sidecars to 0600 when present.
    Sidecars only exist while a WAL connection is open, so callers re-invoke
    this after any write that may have created them."""
    for name in (db_path.name, db_path.name + "-wal", db_path.name + "-shm"):
        sidecar = db_path.with_name(name)
        try:
            sidecar.chmod(0o600)
        except (OSError, FileNotFoundError):
            pass


def init_database(project_dir: Path | str) -> sqlite3.Connection:
    """Open (or create + migrate) the HeyGen journal database for a project.

    Returns a configured connection (WAL, FK, busy_timeout, autocommit with
    explicit transactions for multi-statement atomicity). Idempotent —
    calling on an already-initialized DB is safe.
    """
    project = Path(project_dir)
    lecturecast_dir = project / ".lecturecast"
    runtime_dir = lecturecast_dir / _RUNTIME_DIR_NAME
    db_path = runtime_dir / _DB_NAME

    # Pre-create: reject any pre-existing symlink in the chain BEFORE we touch
    # the filesystem. This must happen before mkdir/chmod so we never modify a
    # symlink target's permissions or contents.
    _reject_symlink_components(
        [lecturecast_dir, runtime_dir, db_path], context="heygen path"
    )

    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Post-create: re-verify the ensured dirs are real (TOCTOU defense).
    _reject_symlink_components(
        [lecturecast_dir, runtime_dir], context="heygen path"
    )

    try:
        runtime_dir.chmod(0o700)
    except OSError:
        pass  # platforms without POSIX chmod

    # Containment: the resolved DB must stay under the project's .lecturecast.
    if not db_path.resolve().is_relative_to(lecturecast_dir.resolve()):
        raise ValueError("heygen DB must resolve inside the project .lecturecast directory")

    if db_path.exists() and _is_symlink(db_path):
        raise ValueError("heygen DB is a symlink — refusing to open")

    # isolation_level=None → autocommit; we drive multi-statement atomicity
    # ourselves via explicit BEGIN IMMEDIATE/COMMIT/ROLLBACK.
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")

        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(f"failed to enable WAL (got {mode})")

        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"heygen journal user_version={current_version} > supported "
                f"{_SCHEMA_VERSION}; refusing to downgrade"
            )
        if current_version < _SCHEMA_VERSION:
            _migrate(conn, current_version)
    except Exception:
        conn.close()
        raise

    _chmod_secure(db_path)
    return conn
