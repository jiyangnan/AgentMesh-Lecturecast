from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .errors import LectureCastError
from .manifest import PublicKeyRing, VerificationResult, verify_manifest
from .protocol import ClientCapabilities, ProductionManifest, canonical_digest


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
        payload["component_catalog_digest"] == available["component_catalog_digest"],
        "组件目录 digest 一致。",
    )
    component_ids = {scene["component_id"] for scene in payload["scenes"]}
    check(
        "components",
        component_ids.issubset(set(available["components"])),
        "所有 Scene 组件均已安装。",
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

