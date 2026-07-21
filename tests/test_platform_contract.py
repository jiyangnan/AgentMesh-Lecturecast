from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE_PAGES = (
    ROOT / "site" / "index.html",
    ROOT / "site" / "en" / "index.html",
    ROOT / "site" / "ja" / "index.html",
    ROOT / "site" / "ko" / "index.html",
)


def _json_ld(path: Path) -> list[dict[str, object]]:
    content = path.read_text(encoding="utf-8")
    payloads = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
        content,
        flags=re.DOTALL,
    )
    documents = [json.loads(payload) for payload in payloads]
    nodes: list[dict[str, object]] = []
    for document in documents:
        graph = document.get("@graph")
        if isinstance(graph, list):
            nodes.extend(node for node in graph if isinstance(node, dict))
        else:
            nodes.append(document)
    return nodes


def test_public_platform_contract_is_macos_and_windows_only() -> None:
    for page in SITE_PAGES:
        software_apps = [
            payload
            for payload in _json_ld(page)
            if payload.get("@type") == "SoftwareApplication"
        ]
        assert len(software_apps) == 1
        assert software_apps[0]["operatingSystem"] == "macOS, Windows"

        content = page.read_text(encoding="utf-8")
        assert "install.ps1" in content
        assert "Linux, Windows" not in content
        assert "Windows, Linux" not in content


def test_readmes_publish_native_installers_and_reject_linux_support() -> None:
    for filename in ("README.md", "README.zh.md"):
        content = (ROOT / filename).read_text(encoding="utf-8")
        assert "scripts/install.sh" in content
        assert "scripts/install.ps1" in content
        assert "macOS / Linux" not in content
        assert "Linux distributions and WSL are" in content or "不支持 Linux" in content


def test_platform_runbook_is_canonical_and_cross_linked() -> None:
    platform_doc = (ROOT / "docs" / "SUPPORTED-PLATFORMS.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "LOCAL-WORKFLOW.md").read_text(encoding="utf-8")

    assert "macOS" in platform_doc and "Windows" in platform_doc
    assert "Linux distributions and WSL are not supported" in platform_doc
    assert "SUPPORTED-PLATFORMS.md" in agents
    assert "SUPPORTED-PLATFORMS.md" in workflow
    assert "build_video.ps1" in workflow


def test_platform_specific_installers_fail_closed() -> None:
    macos_installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    windows_installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert '"$(uname -s)" != "Darwin"' in macos_installer
    assert "Linux and WSL are not supported" in macos_installer
    assert "unsupported mixed macOS architecture" in macos_installer
    assert 'PY_ARCH" != "$HOST_ARCH' in macos_installer
    assert "Win32NT" in windows_installer
    assert "Linux and WSL are not supported" in windows_installer


def test_windows_native_entrypoints_cover_install_and_both_render_routes() -> None:
    required = {
        "scripts/install.ps1": ("lecturecast.exe", "doctor --json", "manage_adapters.ps1"),
        "scripts/manage_adapters.ps1": ("Junction", "adapter conflict"),
        "scripts/uninstall.ps1": ("manage_adapters.ps1", "lecturecast.cmd"),
        "templates/shared/build_video.ps1": (
            "VideoVertical",
            "VideoLandscape",
            "subtitle_vertical.ass",
            "subtitle.ass",
        ),
        "templates/shared/build_manifest_video.ps1": (
            "manifest preflight",
            "DirectorVertical",
            "DirectorLandscape",
            "validate_manifest_outputs.py",
        ),
    }
    for relative, tokens in required.items():
        content = (ROOT / relative).read_text(encoding="utf-8")
        for token in tokens:
            assert token in content, f"{relative} is missing {token!r}"


def test_windows_adapter_upgrades_directory_skills_with_a_backup() -> None:
    script = (ROOT / "scripts" / "manage_adapters.ps1").read_text(encoding="utf-8")

    assert "Move-LegacyAdapterToBackup" in script
    assert "legacy adapter backed up" in script
    assert "adapter upgraded" in script


def test_windows_canary_reads_the_package_version_instead_of_pinning_a_release() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-contract.yml").read_text(
        encoding="utf-8"
    )

    assert "Select-String -Path pyproject.toml" in workflow
    assert '"lecturecast $expected"' in workflow
