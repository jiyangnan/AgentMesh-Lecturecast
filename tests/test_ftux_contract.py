from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_client_version_matches_package_version() -> None:
    from lecturecast.config import CLIENT_VERSION

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert CLIENT_VERSION == project["project"]["version"]
BANNED_TEMPLATE_TERMS = ("扒", "私信", "领取", "暗号", "起底", "爬虫", "爬取", "关注我")


def test_commercial_install_includes_auth_and_manifest_verification() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert any(dependency.startswith("cryptography") for dependency in dependencies)
    assert any(dependency.startswith("edge-tts") for dependency in dependencies)
    assert any(dependency.startswith("keyring") for dependency in dependencies)


def test_installers_run_the_machine_readable_commercial_gate() -> None:
    macos = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert '"$VENV/bin/lecturecast" onboard --json' in macos
    assert "& $LectureCastExe onboard --json" in windows


def test_repeat_install_can_reuse_current_editable_package_without_network() -> None:
    macos = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "lecturecast package already current" in macos
    assert "INSTALLED_VERSION" in macos
    assert "lecturecast package already current" in windows
    assert "$InstalledVersion" in windows
    assert "read_text(encoding='utf-8')" in windows
    assert "m.distributions()" in windows


def test_official_remotion_templates_pass_the_mandatory_compliance_terms() -> None:
    matches: list[str] = []
    for path in (ROOT / "templates" / "remotion" / "src").rglob("*.tsx"):
        content = path.read_text(encoding="utf-8")
        for term in BANNED_TEMPLATE_TERMS:
            if term in content:
                matches.append(f"{path.relative_to(ROOT)}: {term}")

    assert matches == []
