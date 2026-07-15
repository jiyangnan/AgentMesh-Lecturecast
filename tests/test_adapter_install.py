from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "manage_adapters.sh"


def _run(home: Path, action: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "HOME": str(home),
        "LECTURECAST_DIR": str(ROOT),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), action],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_adapter_install_is_idempotent_and_preserves_custom_skill(tmp_path: Path) -> None:
    codex = tmp_path / ".codex" / "skills"
    claude = tmp_path / ".claude" / "skills"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    custom = codex / "lecturecast"
    custom.mkdir()
    marker = custom / "SKILL.md"
    marker.write_text("custom user skill\n", encoding="utf-8")

    first = _run(tmp_path, "install")
    second = _run(tmp_path, "install")

    assert first.returncode == second.returncode == 0
    assert marker.read_text(encoding="utf-8") == "custom user skill\n"
    assert not custom.is_symlink()
    installed = claude / "lecturecast"
    assert installed.is_symlink()
    assert installed.resolve() == ROOT / "skills" / "claude-code"
    assert not (tmp_path / ".openclaw").exists()

    removed = _run(tmp_path, "uninstall")
    assert removed.returncode == 0
    assert not installed.exists()
    assert marker.exists()


def test_openclaw_uses_existing_global_then_workspace_fallback(tmp_path: Path) -> None:
    workspace_skills = tmp_path / ".openclaw" / "workspace" / "skills"
    workspace_skills.mkdir(parents=True)

    fallback = _run(tmp_path, "install")
    assert fallback.returncode == 0
    fallback_link = workspace_skills / "lecturecast"
    assert fallback_link.is_symlink()
    assert fallback_link.resolve() == ROOT / "skills" / "openclaw"
    assert _run(tmp_path, "uninstall").returncode == 0

    global_skills = tmp_path / ".openclaw" / "skills"
    global_skills.mkdir(parents=True)
    preferred = _run(tmp_path, "install")
    assert preferred.returncode == 0
    assert (global_skills / "lecturecast").is_symlink()
    assert not fallback_link.exists()


def test_missing_agent_directories_are_never_created(tmp_path: Path) -> None:
    result = _run(tmp_path, "install")

    assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_three_host_skills_reference_one_shared_director_workflow() -> None:
    shared = ROOT / "skills" / "shared" / "director-workflow.md"
    assert shared.exists()
    for host in ("codex", "claude-code", "openclaw"):
        content = (ROOT / "skills" / host / "SKILL.md").read_text(encoding="utf-8")
        assert content.startswith("---\nname: lecturecast\n")
        assert "../shared/director-workflow.md" in content
        assert "option_id" in content
