from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BANNED_TEMPLATE_TERMS = ("扒", "私信", "领取", "暗号", "起底", "爬虫", "爬取", "关注我")


def test_community_install_does_not_require_director_crypto() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert not any(
        dependency.startswith("cryptography")
        for dependency in project["project"]["dependencies"]
    )
    assert any(
        dependency.startswith("cryptography")
        for dependency in project["project"]["optional-dependencies"]["director"]
    )


def test_official_remotion_templates_pass_the_mandatory_compliance_terms() -> None:
    matches: list[str] = []
    for path in (ROOT / "templates" / "remotion" / "src").rglob("*.tsx"):
        content = path.read_text(encoding="utf-8")
        for term in BANNED_TEMPLATE_TERMS:
            if term in content:
                matches.append(f"{path.relative_to(ROOT)}: {term}")

    assert matches == []
