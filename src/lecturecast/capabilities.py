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
    import cleanly, expose their key classes, AND each class exposes the
    methods backing the reported operations. Presence-only — proves the adapter
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

    # class -> required public methods (§5.5e5b0c3c locked primitives). Class
    # resolution alone is NOT enough: a partial / mixed-version install could
    # expose the class without the backing method, and the server would bill
    # an operation the client cannot serve (e.g. HeyGenVideosAdapter present
    # but delete_video / query_videos_by_title stripped).
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
            if getattr(cls, method, None) is None:
                return False
    return True


def default_heygen_journal_probe(project_root: Path | str) -> bool:
    """Real HeyGen journal probe (§5.5e5c, round-2 hardened), READ-ONLY: is the
    journal DB in a state the current client can actually serve? Mirrors the
    readiness path of init_database (heygen_journal.init_database) WITHOUT ever
    creating, writing, or migrating. Four fail-closed guards:

    1. Symlink mirror: if .lecturecast / runtime / db is itself a symlink ->
       False (init_database rejects symlinks at heygen_journal:406; reporting
       ready would over-claim against an init that will raise).
    2. Prior-use vs fresh: a MISSING db is ready only if the runtime dir was
       never created. runtime/ exists but the db is gone means the journal was
       initialized before and has since been deleted -> prior remote resources
       are unrecoverable -> False (do not over-report idempotency_24h / delete
       capability on a state whose history is lost).
    3. Refuse-downgrade: PRAGMA user_version > _SCHEMA_VERSION -> False
       (init_database raises; genuinely incompatible).
    4. Schema shape, not just version: a db whose user_version was set without
       the tables (manual PRAGMA, partial copy, corruption) would pass a
       version-only probe but the first op would fail "no such table". Verify
       the core resource table exists in sqlite_master.

    Opens `file:<escaped>?mode=ro` (URI) so project paths containing '?', '#',
    or spaces are not mis-parsed. Safe for doctor/canary (read-only constraint)."""
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
        # Guard 2: fresh-missing (ready) vs deleted-after-prior-use (fail-closed).
        # runtime_dir is created only by init_database; its presence means a prior
        # init ran, so a missing db is data loss, not a fresh project.
        return not runtime_dir.exists()

    # Guards 3+4: open read-only and validate version + schema shape.
    uri = "file:" + urllib.parse.quote(db_path.resolve().as_posix()) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        head = conn.execute("PRAGMA user_version").fetchone()[0]
        if head > _SCHEMA_VERSION:
            return False
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return "heygen_remote_resources" in tables
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
