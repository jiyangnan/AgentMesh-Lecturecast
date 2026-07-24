from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import CLIENT_VERSION
from .protocol import ClientCapabilities, canonical_digest


RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
_SEMVER = re.compile(r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)")
COMPONENT_CATALOG_PATH = Path(__file__).with_name("component-catalog.json")
COMPONENT_CATALOG_LOCK_PATH = Path(__file__).with_name("component-catalog.lock")


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
