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


def _not_available() -> bool:
    """Default fail-closed probe: no F5 runtime / HeyGen adapter+journal is
    shipped yet (landed in §5.5e). Production never claims an unexecutable
    capability; tests inject a probe that returns True."""
    return False


def default_heygen_adapter_probe() -> bool:
    """Real HeyGen adapter probe (§5.5e5c): the three shipped adapter modules
    import cleanly, expose their key classes, each class exposes the CALLABLE
    methods backing the reported operations, AND the journal-backed processor
    / orchestrator module imports. Presence-only — proves the adapter + executor
    code is importable + structurally intact on this host, not that the network
    authenticates or that a key is configured (the key is gated separately in
    heygen_processor). Used by production capture call sites (director generate
    + project capabilities); tests inject their own probe.

    Fail-closed on ANY import-time failure (ImportError AND RuntimeError /
    OSError / binary-dependency init errors) — `except Exception`, not just
    `except ImportError`, so a broken adapter install omits HeyGen instead of
    raising. A raise here would propagate out of the shared v1.1 capture path
    and could operationally block the M1 base delivery that does not even
    depend on HeyGen (Codex round-1 qualification on M1 independence)."""
    import importlib

    # class -> required CALLABLE public methods (§5.5e5b0c3c locked primitives).
    # Class resolution alone is NOT enough, and neither is `is not None`: a
    # partial / mixed-version install could expose the class without the backing
    # method, OR set the attribute to a non-callable. Either way the server
    # would bill an operation the client cannot serve (e.g. HeyGenVideosAdapter
    # present but delete_video stripped, or set to a non-callable value).
    required = (
        ("lecturecast.heygen_http", "HeyGenHttpTransport",
         ("request_json", "request_multipart_file")),
        ("lecturecast.heygen_videos_adapter", "HeyGenVideosAdapter",
         ("submit_video", "query_videos_by_title", "delete_video")),
        ("lecturecast.heygen_asset_adapter", "HeyGenAssetAdapter",
         ("upload_asset", "get_asset", "delete_asset")),
    )
    for module_name, attr, methods in required:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            return False
        cls = getattr(module, attr, None)
        if cls is None:
            return False
        for method in methods:
            if not callable(getattr(cls, method, None)):
                return False
    # The journal-backed processors + orchestrator that actually EXECUTE the
    # reported operations (AssetUploadProcessor, DeleteProcessor,
    # AssetDeletionProcessor, ReconcileProcessor, OperationRepository) live in
    # operation_repository. A mixed install could ship the adapters without it;
    # verify the module imports.
    try:
        importlib.import_module("lecturecast.operation_repository")
    except Exception:
        return False
    return True


# The full v6 journal schema (heygen_journal CREATE TABLE set). The probe
# requires ALL of these for a head==_SCHEMA_VERSION DB — version alone does
# not prove the tables exist (manual PRAGMA / partial copy / corruption).
_V6_JOURNAL_TABLES = frozenset({
    "heygen_operations",
    "heygen_consent_receipts",
    "heygen_remote_resources",
    "heygen_resource_operation_refs",
    "heygen_asset_uploads",
})


def _prior_heygen_use_detected(lecturecast_dir: Path) -> bool:
    """Stable prior-use signal for the missing-journal case (round-2 gap B3):
    read client-capabilities.json (stored by ProjectStore at .lecturecast/,
    OUTSIDE runtime/, so it survives deletion of the whole runtime dir). If
    the project ever reported configured HeyGen, a now-missing journal is data
    loss, not a fresh project. Read-only; any read failure is treated as
    prior-use (fail-closed)."""
    import json

    caps_path = lecturecast_dir / "client-capabilities.json"
    if not caps_path.exists():
        return False
    try:
        data = json.loads(caps_path.read_text(encoding="utf-8"))
    except Exception:
        return True  # unreadable prior caps + missing journal -> conservative
    return any(
        processor.get("provider") == "heygen" and processor.get("configured")
        for processor in (data.get("third_party_processors") or [])
    )


def default_heygen_journal_probe(project_root: Path | str) -> bool:
    """Real HeyGen journal probe (§5.5e5c, round-3 hardened), READ-ONLY: is the
    journal DB in a state the current client can actually serve? Mirrors the
    readiness path of init_database WITHOUT ever creating, writing, or
    migrating. Six fail-closed guards:

    1. Symlink mirror: if .lecturecast / runtime / db is itself a symlink ->
       False (init_database rejects symlinks at heygen_journal:406).
    2. Writability: init creates runtime/ + WAL + BEGIN IMMEDIATE, so a path
       that is readable but NOT writable would pass a read-only probe yet fail
       every operation. Require W_OK|X_OK on the relevant parent.
    3. Prior-use vs fresh (missing db): ready only if NEVER used. runtime/
       exists (db gone) OR the stored client-capabilities.json once reported
       configured HeyGen (the caps file lives outside runtime/ and survives its
       deletion) -> prior-use data loss -> False.
    4. Refuse-downgrade: PRAGMA user_version > _SCHEMA_VERSION -> False.
    5. Refuse-mixed-version: user_version < _SCHEMA_VERSION -> False. A legit
       prior-version DB CAN be migrated, but the probe cannot cheaply verify
       the prior-version schema is complete enough to migrate without failing
       (a partial old schema can advance user_version yet leave an unusable
       DB). Fail-closed; the doctor / canary path (§5.5e5d) provides explicit
       migration so the billing path never depends on an upgrade succeeding.
    6. Schema shape: head == _SCHEMA_VERSION requires ALL v6 tables in
       sqlite_master (not just user_version / one core table).

    Opens `file:<escaped>?mode=ro` (URI) so project paths containing '?', '#',
    or spaces are not mis-parsed. resolve() is wrapped (OSError / symlink-loop
    guarded). Safe for doctor/canary (read-only constraint)."""
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
            return False

    if not db_path.exists():
        # Guard 2 (writability, missing-db branch): first-op init must create
        # runtime/ under .lecturecast/.
        if not os.access(lecturecast_dir, os.W_OK | os.X_OK):
            return False
        # Guard 3: fresh vs prior-use. runtime/ exists (db gone) OR stored caps
        # once claimed configured HeyGen -> prior-use data loss.
        if runtime_dir.exists() or _prior_heygen_use_detected(lecturecast_dir):
            return False
        return True

    # Guard 2 (writability, db-exists branch): every processor writes (WAL +
    # BEGIN IMMEDIATE + journal rows) under runtime/.
    if not os.access(runtime_dir, os.W_OK | os.X_OK):
        return False

    # Guards 4+5+6: open read-only and validate version + full schema shape.
    try:
        resolved = db_path.resolve().as_posix()
        uri = "file:" + urllib.parse.quote(resolved) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return False
    try:
        head = conn.execute("PRAGMA user_version").fetchone()[0]
        if head != _SCHEMA_VERSION:
            # Guard 4 (head too new) + Guard 5 (head too old).
            return False
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return _V6_JOURNAL_TABLES <= tables
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


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
        # Operations reflect §5.5e5b0c3c locked primitives (asset/video delete
        # adapters + DeletionCoordinator now shipped). avatar_delete OMITTED —
        # no dedicated primitive (ephemeral portrait_photo reuses the
        # asset_delete path; reusable_avatar is a future dashboard-revoked
        # lifecycle, structurally inert today — _asset_retention_mode always
        # 'ephemeral'). Declaring it would be a false capability claim.
        "operations": [
            "direct_asset_upload",
            "photo_avatar",
            "prerecorded_audio_lipsync",
            "asset_delete",
            "video_delete",
        ],
        # idempotency_24h: 24h reconcile + asset-upload windows (op_repo:70,3120).
        # title_query: HeyGenVideosAdapter.query_videos_by_title (videos_adapter:204).
        # read_only_auth_check: HeyGenAssetAdapter.get_asset docstring designates
        # it "Used for doctor / manual reconciliation only" — a GET on a known
        # id distinguishes 401/403 (auth_failed) from 200/404 (key valid).
        "features": ["idempotency_24h", "title_query", "read_only_auth_check"],
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


def doctor_report(capabilities: ClientCapabilities) -> dict[str, Any]:
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
    return {
        "ready": runtime["can_render_locally"] and runtime["has_libass"],
        "missing": missing,
        "next_actions": next_actions,
        "capabilities": payload,
    }
