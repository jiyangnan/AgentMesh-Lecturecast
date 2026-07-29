"""HeyGen operation journal — SQLite migration + four tables (§5.5e1).

A per-project local database at <project>/.lecturecast/runtime/heygen-operations.db
that provides idempotent third-party operation tracking. It NEVER touches the
shared Core, never crosses product boundaries, and never stores API keys,
media content, or absolute paths.

Four tables:
- heygen_operations: deterministic operation lifecycle (submit → reconcile → complete/cancel)
- heygen_consent_receipts: JIT consent proof (granted/declined/withdrawn)
- heygen_remote_resources: remote HeyGen assets with retention + deletion tracking
- heygen_resource_operation_refs: many-to-many resource↔operation links

All timestamps are ISO-8601 UTC strings. All status fields use CHECK constraints.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


_SCHEMA_VERSION = 1
_RUNTIME_DIR_NAME = "runtime"
_DB_NAME = "heygen-operations.db"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_runtime_path(db_path: Path) -> None:
    """Reject symlinks in the DB path, runtime dir, or any parent under the
    project root. Prevents path-traversal and symlink-swap attacks."""
    resolved = db_path.resolve()
    if db_path.is_symlink():
        raise ValueError("heygen DB path must not be a symlink")
    # Check runtime dir and db
    for p in [db_path.parent, db_path]:
        if p.is_symlink():
            raise ValueError(f"heygen path component must not be a symlink: {p}")
    # Check the resolved path is under the project's .lecturecast dir
    project_lecturecast = db_path.parent.parent  # .lecturecast/runtime → .lecturecast
    if not resolved.is_relative_to(project_lecturecast.resolve()):
        raise ValueError("heygen DB must be inside the project .lecturecast directory")


def init_database(project_dir: Path | str) -> sqlite3.Connection:
    """Open (or create + migrate) the HeyGen journal database for a project.

    Returns a configured connection (WAL, FK, busy_timeout). Idempotent —
    calling on an already-initialized DB is safe.
    """
    project = Path(project_dir)
    runtime_dir = project / ".lecturecast" / _RUNTIME_DIR_NAME
    db_path = runtime_dir / _DB_NAME

    # Create runtime dir with restrictive permissions.
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        runtime_dir.chmod(0o700)
    except OSError:
        pass  # Windows

    _validate_runtime_path(db_path)

    # Open with URI to control flags; check symlink at open time.
    if db_path.exists() and db_path.is_symlink():
        raise ValueError("heygen DB is a symlink — refusing to open")
    conn = sqlite3.connect(
        str(db_path),
        timeout=10.0,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    # Verify WAL actually took effect.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != "wal":
        conn.close()
        raise RuntimeError(f"failed to enable WAL (got {mode})")

    # Migration guard: refuse unknown future versions.
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version > _SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"heygen journal user_version={current_version} > supported {_SCHEMA_VERSION}; "
            f"refusing to downgrade"
        )

    if current_version < _SCHEMA_VERSION:
        try:
            _create_tables(conn)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except Exception:
            raise

    # Tighten DB file permissions.
    try:
        db_path.chmod(0o600)
    except OSError:
        pass

    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create all four tables with CHECK constraints. Only called inside a
    migration transaction (user_version 0 → 1)."""
    conn.executescript("""
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
        );

        CREATE TABLE IF NOT EXISTS heygen_consent_receipts (
            receipt_digest TEXT PRIMARY KEY NOT NULL,
            operation_id TEXT NOT NULL UNIQUE,
            disclosure_version TEXT NOT NULL,
            generation_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            disclosed_assets_json TEXT NOT NULL,
            data_categories_json TEXT NOT NULL,
            provider_cost_disclosure TEXT,
            agentmesh_non_processor_disclosure TEXT,
            status TEXT NOT NULL DEFAULT 'granted'
                CHECK (status IN ('granted', 'declined', 'withdrawn')),
            consented_at TEXT,
            withdrawn_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (operation_id)
                REFERENCES heygen_operations(operation_id)
                ON DELETE RESTRICT
        );

        CREATE TABLE IF NOT EXISTS heygen_remote_resources (
            resource_id TEXT PRIMARY KEY NOT NULL,
            credential_profile_id TEXT,
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
            last_deletion_error TEXT,
            deleted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(credential_profile_id, resource_kind, remote_id),
            FOREIGN KEY (created_by_operation_id)
                REFERENCES heygen_operations(operation_id)
                ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS heygen_resource_operation_refs (
            resource_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (resource_id, operation_id),
            FOREIGN KEY (resource_id)
                REFERENCES heygen_remote_resources(resource_id)
                ON DELETE CASCADE,
            FOREIGN KEY (operation_id)
                REFERENCES heygen_operations(operation_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_operations_generation
            ON heygen_operations(generation_id);
        CREATE INDEX IF NOT EXISTS idx_operations_status
            ON heygen_operations(status);
        CREATE INDEX IF NOT EXISTS idx_remote_resources_created_by_op
            ON heygen_remote_resources(created_by_operation_id);
    """)
