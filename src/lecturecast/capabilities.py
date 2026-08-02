from __future__ import annotations

import json
import platform
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import CLIENT_VERSION
from .protocol import ClientCapabilities, ClientCapabilitiesV1_1, canonical_digest


RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_SEMVER = re.compile(r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)")
COMPONENT_CATALOG_PATH = Path(__file__).with_name("component-catalog.json")
COMPONENT_CATALOG_LOCK_PATH = Path(__file__).with_name("component-catalog.lock")

# v1.1 local-capability probes (§5.5b). Detection is presence-only and NEVER
# uploads credentials, model paths, or network-verification results — the server
# cannot independently verify third-party state without crossing the media
# boundary. `configured` is a capability gate (M2 compatibility), not a
# preflight-passed claim.
F5_MODEL_PATH_ENV = "LECTURECAST_F5_MODEL_PATH"
HEYGEN_API_KEY_ENV = "HEYGEN_API_KEY"

# §5.5e5c locked capability surface — the SINGLE source of truth for both the
# reported third_party_processors payload (heygen_processor) and the doctor
# decomposition (build_heygen_doctor_section, §5.5e5d). Drift between the
# reported payload and the doctor section would be a contradiction the server
# could bill against.
# Operations reflect §5.5e5b0c3c locked primitives (asset/video delete adapters
# + DeletionCoordinator now shipped). avatar_delete OMITTED — no dedicated
# primitive (ephemeral portrait_photo reuses the asset_delete path;
# reusable_avatar is a future dashboard-revoked lifecycle, structurally inert
# today — _asset_retention_mode always 'ephemeral'). Declaring it would be a
# false capability claim.
_HEYGEN_OPERATIONS = (
    "direct_asset_upload",
    "photo_avatar",
    "prerecorded_audio_lipsync",
    "asset_delete",
    "video_delete",
)
# idempotency_24h: 24h reconcile + asset-upload windows (op_repo:70,3120).
# title_query: HeyGenVideosAdapter.query_videos_by_title (videos_adapter:204).
# read_only_auth_check: HeyGenAssetAdapter.get_asset docstring designates it
# "Used for doctor / manual reconciliation only" — a GET on a known id
# distinguishes 401/403 (auth_failed) from 200/404 (key valid).
_HEYGEN_FEATURES = ("idempotency_24h", "title_query", "read_only_auth_check")


def _not_available() -> bool:
    """Default fail-closed probe: no F5 runtime / HeyGen adapter+journal is
    shipped yet (landed in §5.5e). Production never claims an unexecutable
    capability; tests inject a probe that returns True."""
    return False


def default_heygen_adapter_probe() -> bool:
    """Real HeyGen adapter probe (§5.5e5c): the three shipped adapter modules
    import cleanly, expose their key classes, each class exposes the CALLABLE
    methods backing the reported operations, AND the journal-backed processor
    / orchestrator module imports WITH every required processor / coordinator
    class present. Presence-only — proves the adapter + executor code is
    importable + structurally intact on this host, not that the network
    authenticates or that a key is configured (the key is gated separately in
    heygen_processor). Used by production capture call sites (director generate
    + project capabilities); tests inject their own probe.

    Fail-closed on ANY failure (round-2 B2: `except Exception`, not just
    `except ImportError`; round-4 R3-4: a top-level backstop so even a raising
    descriptor or an unexpected filesystem error returns False instead of
    escaping the shared v1.1 capture path and operationally blocking the M1
    base delivery that does not even depend on HeyGen)."""
    import importlib

    try:
        # class -> required CALLABLE public methods (§5.5e5b0c3c locked primitives).
        # Class resolution alone is NOT enough, and neither is `is not None`: a
        # partial / mixed-version install could expose the class without the backing
        # method, OR set the attribute to a non-callable. Either way the server
        # would bill an operation the client cannot serve (e.g. HeyGenVideosAdapter
        # present but delete_video stripped, or set to a non-callable value).
        required = (
            ("lecturecast.heygen_http", "HeyGenHttpTransport",
             ("request_json", "request_multipart_file")),
            # round-6: poll_video added — it is a PUBLIC adapter entry method
            # called by PollProcessor.poll_once (op_repo:3534) AND
            # DownloadProcessor.download_once (op_repo:3644). A mixed install
            # stripping it passed round-5's check but made polling + download
            # unservable. The tuple is now the FULL public surface of each
            # adapter class (verified by ast + cross-referenced against every
            # adapter.* call site in operation_repository.py).
            ("lecturecast.heygen_videos_adapter", "HeyGenVideosAdapter",
             ("submit_video", "poll_video", "query_videos_by_title", "delete_video")),
            ("lecturecast.heygen_asset_adapter", "HeyGenAssetAdapter",
             ("upload_asset", "get_asset", "delete_asset")),
        )
        for module_name, attr, methods in required:
            module = importlib.import_module(module_name)
            cls = getattr(module, attr, None)
            if cls is None:
                return False
            for method in methods:
                if not callable(getattr(cls, method, None)):
                    return False

        # The journal-backed processors + orchestrator that actually EXECUTE the
        # reported operations live in operation_repository. A mixed install could
        # ship the adapters without it, OR ship a partial module that IMPORTS but
        # is missing processor / coordinator classes a reported operation needs
        # (round-3 R3-6), OR ship a real but METHOD-STRIPPED class — e.g. a
        # version that renamed/removed an entry method (round-4 R4-6: a stub
        # class passes isinstance(type) yet the operation fails at runtime).
        # Verify the module imports, every required class resolves as a real
        # type, AND each class's entry methods are CALLABLE (parallel to the
        # adapter method check above; entry methods are the public execution
        # surface backing the 5 reported operations + idempotency_24h, verified
        # against operation_repository.py source). The single wheel ships all of
        # them; a refactor that moves/renames them requires updating this set
        # (the probe detects that execution surface by design).
        required_orchestrator = (
            ("OperationRepository", (
                "claim_submit_in_tx",
                "apply_submit_outcome_in_tx",
                "claim_asset_upload_in_tx",
                "resolve_deletion_plan_in_tx",
                "apply_deletion_outcome_in_tx",
                "find_reconciliation_candidates",
            )),
            ("AssetUploadProcessor", ("upload_once",)),
            ("SubmitProcessor", ("record_submit_outcome",)),
            ("SubmitCoordinator", ("claim_for_submit",)),
            ("PollProcessor", ("poll_once",)),
            ("ReconcileProcessor", ("reconcile_once",)),
            ("DownloadProcessor", ("download_once",)),
            ("DeleteProcessor", ("delete_once",)),
            ("AssetDeletionProcessor", ("delete_once",)),
            ("DeletionCoordinator", ("delete_pass_for_operation", "recover_deletions")),
        )
        op_repo = importlib.import_module("lecturecast.operation_repository")
        for cls_name, entry_methods in required_orchestrator:
            cls = getattr(op_repo, cls_name, None)
            if not isinstance(cls, type):
                return False
            for method in entry_methods:
                if not callable(getattr(cls, method, None)):
                    return False
        return True
    except Exception:
        return False


# The full v6 journal schema (heygen_journal CREATE TABLE set). The probe
# requires ALL of these for a head==_SCHEMA_VERSION DB — version alone does
# not prove the tables exist (manual PRAGMA / partial copy / corruption), and
# neither do table NAMES alone (round-4 R3-1/R3-2): a stub DB with the five
# names but only `(id INTEGER PRIMARY KEY)` columns passes a name-only check
# yet fails every operation. The probe compares each table's full COLUMN set
# against the canonical schema derived from the SAME _DDL_STATEMENTS
# init_database runs (DRY — no hand-maintained column list that could drift
# from the DDL), so any column drift fails closed.
_V6_JOURNAL_TABLES = frozenset({
    "heygen_operations",
    "heygen_consent_receipts",
    "heygen_remote_resources",
    "heygen_resource_operation_refs",
    "heygen_asset_uploads",
})


def _canonical_v6_columns() -> dict[str, frozenset[str]] | None:
    """Build the canonical v6 column set per table by running the SAME
    _DDL_STATEMENTS init_database executes against a fresh in-memory DB. The
    in-memory DB is the single source of truth, so the probe never carries a
    duplicated column list. Returns None on ANY failure (broken import / DDL)
    so the caller fails closed rather than comparing against an unknown shape.
    Built lazily (not at module load) to preserve the deferred-import boundary
    that keeps a broken heygen_journal from crashing capabilities import."""
    try:
        import sqlite3

        from .heygen_journal import _DDL_STATEMENTS
        canon = sqlite3.connect(":memory:")
        try:
            for stmt in _DDL_STATEMENTS:
                canon.execute(stmt)
            return {
                name: frozenset(
                    row[1] for row in canon.execute(f"PRAGMA table_info({name})")
                )
                for name in _V6_JOURNAL_TABLES
            }
        finally:
            canon.close()
    except Exception:
        return None


def _prior_heygen_use_detected(lecturecast_dir: Path) -> bool:
    """Stable prior-use signal for the missing-journal case (round-2 gap B3 /
    round-4 R3-3). EITHER of two artifacts proves HeyGen was ever used here,
    making a now-missing journal data loss rather than a fresh project:

    1. The durable sentinel `.lecturecast/heygen.used` (written by
       init_database). Lives OUTSIDE runtime/ so it survives wholesale
       deletion of runtime/ AND any later overwrite of client-capabilities.json
       (a mutable snapshot, therefore non-monotonic — the failure mode Codex
       round-3 R3-3 identified: recapture after deletion erases the caps
       marker, after which a missing journal wrongly looks fresh).
    2. client-capabilities.json once reported a configured HeyGen processor
       (stored by ProjectStore at .lecturecast/, also outside runtime/).

    Read-only; ANY read failure or malformed shape is treated as prior-use
    (fail-closed). Top-level backstop (round-4 R3-4): the predicate never
    raises — an unexpected error means prior-use cannot be ruled out."""
    import json
    import os

    try:
        from .heygen_journal import _PRIOR_USE_SENTINEL
    except Exception:
        return True  # cannot establish the sentinel contract -> conservative

    try:
        # Signal 1: durable init-time sentinel. ANY path entry (file, directory,
        # broken symlink) counts as prior-use — round-4 R4-3: if the sentinel
        # path exists as a non-file (e.g. heygen.used was made a directory, so
        # init's best-effort touch raised IsADirectoryError and was swallowed),
        # isfile would miss it and a later runtime/ deletion would over-report.
        # lexists is the conservative read (can only cause safe under-report,
        # never over-report).
        if os.path.lexists(lecturecast_dir / _PRIOR_USE_SENTINEL):
            return True
        # Signal 2: stored capability snapshot once claimed configured HeyGen.
        caps_path = lecturecast_dir / "client-capabilities.json"
        if not caps_path.exists():
            return False
        try:
            data = json.loads(caps_path.read_text(encoding="utf-8"))
        except Exception:
            return True  # unreadable prior caps + missing journal -> conservative
        if not isinstance(data, dict):
            return True  # malformed (non-object) caps + missing journal -> conservative
        return any(
            isinstance(processor, dict)
            and processor.get("provider") == "heygen"
            and processor.get("configured")
            for processor in (data.get("third_party_processors") or [])
        )
    except Exception:
        return True  # backstop: cannot rule out prior-use -> fail-closed


# Classifications that mean "the journal can serve every reported operation" —
# the locked gate probe returns True iff _journal_state yields one of these.
# 'fresh' = no DB yet but the parent is writable AND there is no prior-use
# signal (first init will succeed); 'current' = DB at _SCHEMA_VERSION with the
# full canonical column shape AND writable. Every other classification is a
# fail-closed state (see _journal_state).
_JOURNAL_READY = frozenset({"fresh", "current"})


def _journal_state(project_root: Path | str) -> dict[str, Any]:
    """Shared read-only journal guard logic (§5.5e5d refactor): the SINGLE
    source of truth consumed by both the locked gate probe (bool, §5.5e5c) and
    the doctor diagnostic (structured, §5.5e5d). Mirrors the readiness path of
    init_database WITHOUT ever creating, writing, or migrating. Returns a
    classification dict so callers can collapse to a bool (probe) OR decompose
    WHY (doctor). Fail-closed guards (any violation -> a non-ready class):

    1. Symlink mirror: if .lecturecast / runtime / db is itself a symlink ->
       'symlink' (init_database rejects symlinks at heygen_journal:406).
    2a. Writability (missing-db branch): init creates runtime/ under
        .lecturecast/, so the parent must be W_OK|X_OK -> else 'parent_unwritable'.
    2b. Writability (db-exists branch): every processor writes (WAL + BEGIN
        IMMEDIATE + journal rows) under runtime/, AND the DB file itself must
        be writable — chmod 0400 passes a directory-only check but fails every
        write (round-4 R3-7) -> else 'runtime_unwritable' / 'db_readonly'.
    3. Prior-use vs fresh (missing db): ready only if NEVER used. runtime/
       exists (db gone) OR the durable sentinel OR stored caps once reported
       configured HeyGen -> 'missing_prior_use' (prior-use data loss).
       Otherwise 'fresh' (first init will succeed).
    4. Refuse-downgrade: PRAGMA user_version > _SCHEMA_VERSION -> 'ahead'
       (client older than the on-disk journal; BLOCKER for doctor).
    5. Refuse-mixed-version: user_version < _SCHEMA_VERSION -> 'behind' (a
       legit prior-version DB CAN be migrated, but the probe cannot cheaply
       verify the prior-version schema is complete enough to migrate without
       failing; WARN for doctor — explicit migration via §5.5e5d).
    6. Schema shape: head == _SCHEMA_VERSION requires ALL v6 tables in
       sqlite_master AND each table's full COLUMN set must equal the canonical
       v6 schema (round-4 R3-1/R3-2: name-only is not enough) -> else
       'shape_mismatch' / 'canonical_unavailable'.

    Returns {'classification', 'head', 'writable'}. 'head' is the PRAGMA
    user_version when the DB is readable, else None. Opens
    `file:<escaped>?mode=ro` (URI) so project paths containing '?', '#', or
    spaces are not mis-parsed. May raise on truly unexpected errors; callers
    (probe / diagnostic) wrap in their own top-level backstop."""
    import os
    import urllib.parse

    from .heygen_journal import (
        _DB_NAME, _RUNTIME_DIR_NAME, _SCHEMA_VERSION, _is_symlink,
    )

    lecturecast_dir = Path(project_root) / ".lecturecast"
    runtime_dir = lecturecast_dir / _RUNTIME_DIR_NAME
    db_path = runtime_dir / _DB_NAME

    # Guard 1: mirror init_database's symlink rejection.
    for component in (lecturecast_dir, runtime_dir, db_path):
        if _is_symlink(component):
            return {"classification": "symlink", "head": None, "writable": False}

    if not db_path.exists():
        # Guard 2a: first-op init must create runtime/ under .lecturecast/.
        if not os.access(lecturecast_dir, os.W_OK | os.X_OK):
            return {"classification": "parent_unwritable", "head": None, "writable": False}
        # Guard 3: fresh vs prior-use. runtime/ exists (db gone) OR a durable
        # prior-use signal -> data loss.
        if runtime_dir.exists() or _prior_heygen_use_detected(lecturecast_dir):
            return {"classification": "missing_prior_use", "head": None, "writable": True}
        return {"classification": "fresh", "head": None, "writable": True}

    # Guard 2b (writability): runtime/ writable AND the DB file writable.
    if not os.access(runtime_dir, os.W_OK | os.X_OK):
        return {"classification": "runtime_unwritable", "head": None, "writable": False}
    if not os.access(db_path, os.W_OK):
        return {"classification": "db_readonly", "head": None, "writable": False}

    # Guards 4+5+6: open read-only and validate version + full column shape.
    try:
        resolved = db_path.resolve().as_posix()
        uri = "file:" + urllib.parse.quote(resolved) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return {"classification": "unreadable", "head": None, "writable": True}
    try:
        head = conn.execute("PRAGMA user_version").fetchone()[0]
        # Guard 4 (head too new) + Guard 5 (head too old) — distinguished so
        # doctor can route ahead=BLOCKER vs behind=WARN.
        if head > _SCHEMA_VERSION:
            return {"classification": "ahead", "head": head, "writable": True}
        if head < _SCHEMA_VERSION:
            return {"classification": "behind", "head": head, "writable": True}
        # Guard 6: full column-shape match. Table NAMES alone are not enough
        # (R3-1/R3-2 stub-table repro); compare each table's column set against
        # the canonical schema from the SAME DDL init_database runs. Cannot
        # establish canonical -> fail-closed.
        canonical = _canonical_v6_columns()
        if canonical is None:
            return {"classification": "canonical_unavailable", "head": head, "writable": True}
        live_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not (_V6_JOURNAL_TABLES <= live_tables):
            return {"classification": "shape_mismatch", "head": head, "writable": True}
        for table_name, expected_cols in canonical.items():
            live_cols = frozenset(
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table_name})")
            )
            if live_cols != expected_cols:
                return {"classification": "shape_mismatch", "head": head, "writable": True}
        return {"classification": "current", "head": head, "writable": True}
    except sqlite3.Error:
        return {"classification": "unreadable", "head": None, "writable": True}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def default_heygen_journal_probe(project_root: Path | str) -> bool:
    """Locked §5.5e5c gate probe (bool): True iff the journal is in a servable
    state — classification 'fresh' (first init will succeed) or 'current' (DB
    at _SCHEMA_VERSION with the full canonical column shape AND writable).
    Delegates to `_journal_state` (the single source of truth for the guard
    set, rounds 1-6 hardened — see there for the full rationale). NEVER raises:
    a top-level fail-closed backstop (round-4 R3-4) guarantees the predicate
    cannot escape the shared v1.1 capture path and block M1. Read-only
    (mode=ro URI); creates / migrates / writes nothing. Safe for doctor/canary."""
    try:
        return _journal_state(project_root)["classification"] in _JOURNAL_READY
    except Exception:
        # Top-level fail-closed backstop: any uncaught error means the journal
        # state cannot be established, so configured must not be reported.
        return False


def default_heygen_journal_diagnostic(
    project_root: Path | str,
) -> dict[str, Any]:
    """§5.5e5d read-only diagnostic (sibling to the locked gate probe): returns
    the STRUCTURED journal state so doctor can decompose WHY configured is
    false. The gate probe collapses to a bool; doctor must instead distinguish
    'ahead' (BLOCKER — client older than the on-disk journal) from 'behind'
    (WARN — needs explicit migration) from 'shape_mismatch' /
    'missing_prior_use' / etc. (BLOCKER). Delegates to the SAME `_journal_state`
    as the gate probe, so the diagnostic and the gate can never disagree on the
    classification. NEVER raises — a top-level backstop returns 'unreadable'.
    Read-only (mode=ro URI); creates / migrates / writes nothing.

    Returns {'classification', 'head', 'writable'} (see _journal_state)."""
    try:
        return _journal_state(project_root)
    except Exception:
        # Top-level fail-closed backstop (mirrors the gate probe): an
        # unclassifiable journal state is reported as 'unreadable' so doctor
        # surfaces a BLOCKER rather than crashing the health path.
        return {"classification": "unreadable", "head": None, "writable": False}


def _default_readable(path: str) -> bool:
    """Default readable probe: checks os.access(path, R_OK)."""
    import os
    return os.access(path, os.R_OK)


def f5_available(
    *,
    env: dict[str, str] | None = None,
    path_probe: Callable[[str], Path] = Path,
    runtime_probe: Callable[[], bool] = _not_available,
    readable_probe: Callable[[str], bool] = _default_readable,
) -> bool:
    """F5 local voice-cloning is available iff (a) a model file path is
    configured and exists as a file, (b) the file is readable (R_OK),
    AND (c) the F5 runtime + client adapter are executable (runtime_probe).
    The default runtime_probe fails closed — no F5 adapter is shipped yet.
    No path content / model bytes are uploaded."""
    import os

    sources = env if env is not None else os.environ
    model_path = (sources.get(F5_MODEL_PATH_ENV) or "").strip()
    if not model_path:
        return False
    try:
        if not path_probe(model_path).is_file():
            return False
    except OSError:
        return False
    return readable_probe(model_path) and runtime_probe()


def heygen_processor(
    *,
    env: dict[str, str] | None = None,
    adapter_probe: Callable[[], bool] = _not_available,
    journal_probe: Callable[[], bool] = _not_available,
) -> dict[str, Any] | None:
    """HeyGen BYO processor capability iff (a) a non-empty API key is
    configured locally, AND (b) the HeyGen client adapter is installed
    (adapter_probe), AND (c) the SQLite operation journal / idempotency support
    is ready (journal_probe). Both probes default fail-closed — no adapter or
    journal is shipped yet (§5.5e). Returns the processor declaration (no key,
    no verified field); None when not fully configured."""
    import os

    sources = env if env is not None else os.environ
    if not (sources.get(HEYGEN_API_KEY_ENV) or "").strip():
        return None
    if not (adapter_probe() and journal_probe()):
        return None
    return {
        "provider": "heygen",
        "api_version": "v3",
        "configured": True,
        "credential_mode": "byo_local",
        # Operations/features sourced from the module-level locked surface
        # (_HEYGEN_OPERATIONS / _HEYGEN_FEATURES) — see there for the per-op
        # rationale + the avatar_delete omission.
        "operations": list(_HEYGEN_OPERATIONS),
        "features": list(_HEYGEN_FEATURES),
    }


def load_component_catalog() -> tuple[dict[str, Any], str]:
    catalog = json.loads(COMPONENT_CATALOG_PATH.read_text(encoding="utf-8"))
    lock = json.loads(COMPONENT_CATALOG_LOCK_PATH.read_text(encoding="utf-8"))
    actual_digest = canonical_digest(catalog)
    if lock.get("catalog_digest") != actual_digest:
        raise ValueError("component catalog lock does not match exact catalog bytes")
    if lock.get("component_count") != len(catalog.get("components", [])):
        raise ValueError("component catalog count does not match lock")
    return catalog, actual_digest


def _default_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # The first invocation of an Intel Homebrew ffmpeg under Rosetta can take
    # longer than five seconds on an otherwise healthy Apple Silicon machine.
    # Capability detection is read-only, so allow that one-time startup cost.
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)


def _version(command: Sequence[str], *, runner: RunCommand) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = runner(command)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _SEMVER.search(f"{result.stdout}\n{result.stderr}")
    return match.group("version") if match else None


def _package_version(package_path: Path) -> str | None:
    try:
        value = json.loads(package_path.read_text(encoding="utf-8"))["version"]
    except (FileNotFoundError, OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) and _SEMVER.fullmatch(value) else None


def _remotion_version(
    *,
    project_root: Path | None,
    repo_root: Path | None,
    runner: RunCommand,
) -> str | None:
    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(
            project_root.expanduser().resolve()
            / "remotion"
            / "node_modules"
            / "remotion"
            / "package.json"
        )
    if repo_root is not None:
        candidates.append(
            repo_root.expanduser().resolve()
            / "templates"
            / "remotion"
            / "node_modules"
            / "remotion"
            / "package.json"
        )
    for package_path in candidates:
        version = _package_version(package_path)
        if version is not None:
            return version
    return _version(["remotion", "--version"], runner=runner)


def capture_capabilities(
    *,
    adapter_kind: str = "text",
    adapter_version: str = "1.0.0",
    components: list[str] | None = None,
    component_catalog_digest: str | None = None,
    project_root: Path | None = None,
    repo_root: Path | None = None,
    runner: RunCommand = _default_run,
) -> ClientCapabilities:
    node_version = _version(["node", "--version"], runner=runner)
    ffmpeg_version = _version(["ffmpeg", "-version"], runner=runner)
    remotion_version = _remotion_version(
        project_root=project_root,
        repo_root=repo_root,
        runner=runner,
    )
    has_libass = False
    if ffmpeg_version is not None:
        try:
            build = runner(["ffmpeg", "-buildconf"])
            filters = runner(["ffmpeg", "-hide_banner", "-filters"])
            filter_output = f"{filters.stdout}\n{filters.stderr}"
            has_libass = (
                "--enable-libass" in f"{build.stdout}\n{build.stderr}"
                and re.search(r"(?m)^\s*\S+\s+(?:ass|subtitles)\s", filter_output) is not None
            )
        except (OSError, subprocess.SubprocessError):
            has_libass = False
    catalog, locked_digest = load_component_catalog()
    catalog_components = [item["component_id"] for item in catalog["components"]]
    installed_components = sorted(set(catalog_components if components is None else components))
    catalog_digest = component_catalog_digest or locked_digest
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return ClientCapabilities.model_validate(
        {
            "schema_version": "1.0",
            "capabilities_id": f"caps_{uuid.uuid4().hex}",
            "client": {"name": "agentmesh-lecturecast", "version": CLIENT_VERSION},
            "adapter": {"kind": adapter_kind, "version": adapter_version},
            "supported_manifest_versions": ["1.0"],
            "component_catalog_digest": catalog_digest,
            "components": installed_components,
            "aspect_ratios": ["16:9", "9:16", "3:4"],
            "output_formats": ["mp4", "png"],
            "tts_engines": ["edge", "minimax"],
            "runtime": {
                "python_version": platform.python_version(),
                "node_version": node_version,
                "remotion_version": remotion_version,
                "ffmpeg_version": ffmpeg_version,
                "has_libass": has_libass,
                "can_render_locally": all(
                    value is not None for value in (node_version, remotion_version, ffmpeg_version)
                ),
            },
            "captured_at": now,
        }
    )


def capture_capabilities_v1_1(
    *,
    adapter_kind: str = "text",
    adapter_version: str = "1.0.0",
    components: list[str] | None = None,
    component_catalog_digest: str | None = None,
    project_root: Path | None = None,
    repo_root: Path | None = None,
    runner: RunCommand = _default_run,
    env: dict[str, str] | None = None,
    path_probe: Callable[[str], Path] = Path,
    runtime_probe: Callable[[], bool] = _not_available,
    adapter_probe: Callable[[], bool] = _not_available,
    journal_probe: Callable[[], bool] = _not_available,
) -> ClientCapabilitiesV1_1:
    """Capture v1.1 capabilities: the v1.0 base plus per-artifact version
    negotiation, the local F5 TTS engine (when a model is present), and the
    HeyGen BYO processor (when a key is configured). Detection is presence-only
    and uploads no credentials, paths, or verification results. F5/HeyGen probes
    default fail-closed until the adapters + journal ship in §5.5e."""
    base = capture_capabilities(
        adapter_kind=adapter_kind, adapter_version=adapter_version,
        components=components, component_catalog_digest=component_catalog_digest,
        project_root=project_root, repo_root=repo_root, runner=runner,
    ).model_dump()
    tts_engines = list(base["tts_engines"])
    if f5_available(env=env, path_probe=path_probe, runtime_probe=runtime_probe) and "f5" not in tts_engines:
        tts_engines.append("f5")
    processor = heygen_processor(env=env, adapter_probe=adapter_probe, journal_probe=journal_probe)
    payload = dict(base)
    payload["schema_version"] = "1.1"
    payload["tts_engines"] = tts_engines
    # supported_manifest_versions stays ["1.0"] (the manifest schema is frozen);
    # per-artifact v1.1 negotiation goes through supported_artifact_versions.
    payload["supported_artifact_versions"] = {
        "creative_brief": ["1.0", "1.1"],
        "production_manifest": ["1.0"],
        "presenter_plan": ["1.1"],
        "orchestration_plan": ["1.1"],
    }
    if processor is not None:
        payload["third_party_processors"] = [processor]
    return ClientCapabilitiesV1_1.model_validate(payload)


def build_heygen_doctor_section(
    *,
    env: dict[str, str] | None = None,
    project_root: Path | str | None = None,
    adapter_probe: Callable[[], bool] = default_heygen_adapter_probe,
    journal_diagnostic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§5.5e5d: decompose the HeyGen capability into a doctor-facing section with
    BLOCKER/WARN decision logic (mirrors f5-capability-check.md). Read-only; no
    key value is leaked (only `key_present` bool). The gate probe
    (default_heygen_journal_probe) collapses the journal state to a bool, but
    doctor must distinguish WHY configured is false: key_missing /
    adapter_unimportable / journal_ahead (BLOCKER) vs journal_behind_head
    (WARN). Returns the SAME operations/features surface heygen_processor
    reports (sourced from _HEYGEN_OPERATIONS / _HEYGEN_FEATURES) when
    configured, else empty lists — so doctor and the reported payload cannot
    disagree on the surface.

    `adapter_probe` and `journal_diagnostic` are injectable for tests; the
    diagnostic is computed fresh from `project_root` if not supplied."""
    import os

    sources = env if env is not None else os.environ
    key_present = bool((sources.get(HEYGEN_API_KEY_ENV) or "").strip())
    try:
        adapter_ok = bool(adapter_probe())
    except Exception:
        adapter_ok = False
    if journal_diagnostic is None:
        journal_diagnostic = (
            default_heygen_journal_diagnostic(project_root)
            if project_root is not None
            else {"classification": "unreadable", "head": None, "writable": False}
        )
    classification = journal_diagnostic.get("classification", "unreadable")
    journal_ready = classification in _JOURNAL_READY
    configured = key_present and adapter_ok and journal_ready

    blockers: list[str] = []
    warnings: list[str] = []
    if not key_present:
        blockers.append("key_missing")
    # Adapter + journal failure modes are only meaningful once a key exists;
    # without one they are redundant (key_missing already blocks).
    if key_present and not adapter_ok:
        blockers.append("adapter_unimportable")
    if key_present and adapter_ok:
        if classification == "ahead":
            blockers.append("journal_ahead")
        elif classification == "behind":
            warnings.append("journal_behind_head")
        elif classification not in _JOURNAL_READY:
            # Every other non-ready classification (missing_prior_use /
            # shape_mismatch / canonical_unavailable / db_readonly /
            # runtime_unwritable / unreadable / symlink / parent_unwritable)
            # is a hard BLOCKER — the journal cannot serve reported operations.
            blockers.append(f"journal_{classification}")

    return {
        "provider": "heygen",
        "configured": configured,
        "key_present": key_present,
        "adapter_importable": adapter_ok,
        "journal": {
            "classification": classification,
            "head": journal_diagnostic.get("head"),
            "writable": bool(journal_diagnostic.get("writable", False)),
        },
        "operations": list(_HEYGEN_OPERATIONS) if configured else [],
        "features": list(_HEYGEN_FEATURES) if configured else [],
        "blockers": blockers,
        "warnings": warnings,
    }


def doctor_report(
    capabilities: ClientCapabilities,
    *,
    heygen_section: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = capabilities.model_dump()
    runtime = payload["runtime"]
    missing = [
        name
        for name, value in (
            ("node", runtime["node_version"]),
            ("remotion", runtime["remotion_version"]),
            ("ffmpeg", runtime["ffmpeg_version"]),
        )
        if value is None
    ]
    if not runtime["has_libass"]:
        missing.append("ffmpeg-libass")
    next_actions: list[str] = []
    if runtime["node_version"] is None:
        next_actions.append("安装 Node.js 20+ LTS，并确认 node 与 npm 在当前 PATH")
    if runtime["remotion_version"] is None:
        next_actions.append(
            "在 LectureCast 项目中复制 remotion 模板并运行：cd remotion && npm install"
        )
    if runtime["ffmpeg_version"] is None:
        next_actions.append(
            "安装带 libass 的 ffmpeg；macOS 使用 ffmpeg-full，并只在当前 shell "
            "将其 bin 放到 PATH 最前面"
        )
    elif not runtime["has_libass"]:
        next_actions.append(
            "当前 ffmpeg 缺少 libass；macOS 可运行：brew install ffmpeg-full，"
            "再将 $(brew --prefix ffmpeg-full)/bin 放到本次 PATH 最前面"
        )
    report: dict[str, Any] = {
        "ready": runtime["can_render_locally"] and runtime["has_libass"],
        "missing": missing,
        "next_actions": next_actions,
        "capabilities": payload,
    }
    # §5.5e5d: v1.1 additive HeyGen doctor section (BLOCKER/WARN decomposition).
    # top-level `ready` stays M1-runtime-only — a user without a HeyGen key is
    # still `ready` for the M1 base-video path (M1-independence, spec line 489);
    # HeyGen readiness is reported separately under `third_party`.
    if payload.get("schema_version") == "1.1" and heygen_section is not None:
        report["third_party"] = heygen_section
    return report
