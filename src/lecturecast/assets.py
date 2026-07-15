from __future__ import annotations

import copy
import shutil
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import LectureCastError


def local_asset_relative_path(uri: str) -> Path | None:
    """Return the project-relative path for an asset:// URI."""
    parsed = urlparse(uri)
    if parsed.scheme != "asset":
        return None
    decoded = "/".join(part for part in (unquote(parsed.netloc), unquote(parsed.path.lstrip("/"))) if part)
    logical = PurePosixPath(decoded)
    if (
        not decoded
        or "\\" in decoded
        or "\x00" in decoded
        or logical.is_absolute()
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise LectureCastError(
            code="manifest_incompatible",
            message="Manifest 包含不安全的本地素材引用。",
            next_action="重新生成只使用 asset://namespace/path 的 Manifest。",
        )
    return Path(*logical.parts)


def _source_for(project_root: Path, relative: Path) -> Path:
    assets_root = (project_root / ".lecturecast" / "assets").resolve()
    candidate = assets_root / relative
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return candidate
    if not resolved.is_relative_to(assets_root):
        raise LectureCastError(
            code="manifest_incompatible",
            message="本地素材引用越过了项目素材目录。",
            next_action="把素材复制到 .lecturecast/assets 后重新绑定。",
        )
    return resolved


def local_asset_errors(manifest: dict[str, Any], project_root: Path) -> list[str]:
    errors: list[str] = []
    for scene in manifest["scenes"]:
        for asset in scene["assets"]:
            relative = local_asset_relative_path(asset["uri"])
            if relative is None:
                continue
            source = _source_for(project_root, relative)
            if asset["required"] and (not source.is_file() or source.is_symlink()):
                errors.append(f"{scene['scene_id']}:{asset['asset_id']}:missing_local_asset")
    return errors


def materialize_manifest_assets(
    manifest: dict[str, Any],
    *,
    project_root: Path,
    public_root: Path,
) -> dict[str, Any]:
    """Copy project-local assets into Remotion public/ and rewrite only render props."""
    document = copy.deepcopy(manifest)
    manifest_id = str(document["manifest_id"])
    destination_root = public_root / "director" / "assets" / manifest_id
    for scene in document["scenes"]:
        rendered_assets: list[dict[str, Any]] = []
        for asset in scene["assets"]:
            relative = local_asset_relative_path(asset["uri"])
            if relative is None:
                rendered_assets.append(asset)
                continue
            source = _source_for(project_root, relative)
            if not source.is_file() or source.is_symlink():
                if asset["required"]:
                    raise LectureCastError(
                        code="manifest_incompatible",
                        message=f"缺少必需的本地素材：{asset['asset_id']}。",
                        next_action=f"请把素材放到 .lecturecast/assets/{relative.as_posix()} 后重试。",
                    )
                continue
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            rendered = dict(asset)
            rendered["uri"] = (
                f"director/assets/{manifest_id}/{relative.as_posix()}"
            )
            rendered_assets.append(rendered)
        scene["assets"] = rendered_assets
    return document
