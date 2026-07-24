from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .assets import local_asset_errors
from .capabilities import load_component_catalog
from .errors import LectureCastError
from .manifest import PublicKeyRing, VerificationResult, verify_manifest
from .protocol import ClientCapabilities, ProductionManifest, canonical_digest
from .timing import narration_timing_issues


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    passed: bool
    message: str


@dataclass(frozen=True)
class PreflightResult:
    passed: bool
    manifest_digest: str
    capability_digest: str
    verification: VerificationResult
    checks: tuple[PreflightCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "manifest_digest": self.manifest_digest,
            "capability_digest": self.capability_digest,
            "verification": self.verification.to_dict(),
            "checks": [asdict(check) for check in self.checks],
        }


def run_preflight(
    manifest: ProductionManifest | dict[str, Any],
    capabilities: ClientCapabilities | dict[str, Any],
    *,
    keyring: PublicKeyRing | None = None,
    project_root: Path | None = None,
) -> PreflightResult:
    document = (
        manifest if isinstance(manifest, ProductionManifest) else ProductionManifest.model_validate(manifest)
    )
    client = (
        capabilities
        if isinstance(capabilities, ClientCapabilities)
        else ClientCapabilities.model_validate(capabilities)
    )
    payload = document.model_dump()
    available = client.model_dump()
    verification = verify_manifest(document, keyring=keyring)
    checks: list[PreflightCheck] = []
    catalog, local_catalog_digest = load_component_catalog()
    catalog_entries = {item["component_id"]: item for item in catalog["components"]}

    def check(check_id: str, passed: bool, message: str) -> None:
        checks.append(PreflightCheck(check_id=check_id, passed=passed, message=message))

    check(
        "manifest_version",
        payload["schema_version"] in available["supported_manifest_versions"],
        "客户端支持 Manifest schema_version。",
    )
    check(
        "capability_digest",
        payload["capability_digest"] == canonical_digest(client),
        "Manifest 绑定当前 ClientCapabilities。",
    )
    check(
        "component_catalog",
        payload["component_catalog_digest"]
        == available["component_catalog_digest"]
        == local_catalog_digest,
        "组件目录 digest 一致。",
    )
    component_ids = {scene["component_id"] for scene in payload["scenes"]}
    check(
        "components",
        component_ids.issubset(set(available["components"]))
        and component_ids.issubset(catalog_entries),
        "所有 Scene 组件均已安装。",
    )
    video_aspects = {
        output["aspect_ratio"] for output in payload["outputs"] if output["kind"] == "video"
    }
    contract_errors: list[str] = []
    for scene in payload["scenes"]:
        entry = catalog_entries.get(scene["component_id"])
        if entry is None:
            contract_errors.append(f"{scene['scene_id']}:unknown_component")
            continue
        validator = Draft202012Validator(entry["props_schema"])
        if next(validator.iter_errors(scene["props"]), None) is not None:
            contract_errors.append(f"{scene['scene_id']}:invalid_props")
        supported = set(entry["supported_aspect_ratios"])
        if not video_aspects.issubset(supported):
            contract_errors.append(f"{scene['scene_id']}:unsupported_aspect")
        requirements = entry["asset_requirements"]
        if len(scene["assets"]) < requirements["min_assets"]:
            contract_errors.append(f"{scene['scene_id']}:missing_asset")
        allowed_media = set(requirements["allowed_media_types"])
        if allowed_media and any(
            asset["media_type"] not in allowed_media for asset in scene["assets"]
        ):
            contract_errors.append(f"{scene['scene_id']}:unsupported_asset")
    check(
        "component_contracts",
        not contract_errors,
        "Scene Props、比例和素材需求满足本地组件契约。"
        + (f" 失败：{', '.join(contract_errors)}" if contract_errors else ""),
    )
    if project_root is not None:
        asset_errors = local_asset_errors(payload, project_root)
        check(
            "local_assets",
            not asset_errors,
            "所有必需的 asset:// 素材均存在于本地项目素材目录。"
            + (f" 失败：{', '.join(asset_errors)}" if asset_errors else ""),
        )
    check(
        "aspect_ratios",
        all(output["aspect_ratio"] in available["aspect_ratios"] for output in payload["outputs"]),
        "客户端支持所有输出比例。",
    )
    check(
        "output_formats",
        all(output["format"] in available["output_formats"] for output in payload["outputs"]),
        "客户端支持所有输出格式。",
    )
    check(
        "voice_engine",
        payload["voice"]["engine"] in available["tts_engines"],
        "客户端支持 Manifest 指定的旁白引擎。",
    )
    timing_issues = narration_timing_issues(document)
    check(
        "narration_timing",
        not timing_issues,
        "旁白文本密度与 Manifest 时间线一致。"
        + (
            " 失败："
            + ", ".join(
                f"{issue.section_id or issue.scope}:{issue.code}:"
                f"{issue.units_per_minute:.1f}/min"
                for issue in timing_issues
            )
            if timing_issues
            else ""
        ),
    )
    check(
        "local_runtime",
        bool(available["runtime"]["can_render_locally"]),
        "Node、Remotion 与 ffmpeg 本地渲染链路可用。",
    )
    check(
        "subtitle_runtime",
        not payload["subtitles"]["burn_in"] or bool(available["runtime"]["has_libass"]),
        "烧录字幕需要的 ffmpeg libass 可用。",
    )
    passed = all(item.passed for item in checks)
    result = PreflightResult(
        passed=passed,
        manifest_digest=canonical_digest(document),
        capability_digest=canonical_digest(client),
        verification=verification,
        checks=tuple(checks),
    )
    if not passed:
        failed = ", ".join(item.check_id for item in checks if not item.passed)
        raise LectureCastError(
            code="manifest_incompatible",
            message=f"Manifest preflight 未通过：{failed}。",
            next_action="升级本地组件或重新提交最新 ClientCapabilities 后生成 Manifest。",
        )
    return result
