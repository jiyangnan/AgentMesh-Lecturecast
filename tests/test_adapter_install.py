from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "manage_adapters.sh"
BASH_ONLY = pytest.mark.skipif(os.name == "nt", reason="macOS Bash adapter contract")


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


@BASH_ONLY
def test_adapter_install_upgrades_directory_skill_and_preserves_backup(
    tmp_path: Path,
) -> None:
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
    assert "legacy adapter backed up" in first.stdout
    assert "adapter upgraded" in first.stdout
    assert "adapter already registered" in second.stdout
    assert custom.is_symlink()
    assert custom.resolve() == ROOT / "skills" / "codex"
    backups = list(codex.glob("lecturecast.backup-*"))
    assert len(backups) == 1
    backup_marker = backups[0] / "SKILL.md"
    assert backup_marker.read_text(encoding="utf-8") == "custom user skill\n"
    installed = claude / "lecturecast"
    assert installed.is_symlink()
    assert installed.resolve() == ROOT / "skills" / "claude-code"
    assert not (tmp_path / ".openclaw").exists()

    removed = _run(tmp_path, "uninstall")
    assert removed.returncode == 0
    assert not installed.exists()
    assert not custom.exists()
    assert backup_marker.exists()


@BASH_ONLY
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


@BASH_ONLY
def test_missing_agent_directories_are_never_created(tmp_path: Path) -> None:
    result = _run(tmp_path, "install")

    assert result.returncode == 0
    assert list(tmp_path.iterdir()) == []
    assert "Codex adapter skipped: host not detected" in result.stdout
    assert "Claude Code adapter skipped: host not detected" in result.stdout
    assert "OpenClaw adapter skipped: host not detected" in result.stdout


@BASH_ONLY
def test_existing_codex_host_gets_skills_directory_and_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / ".codex").mkdir()

    result = _run(tmp_path, "install")

    assert result.returncode == 0
    assert "Codex skills directory created" in result.stdout
    installed = tmp_path / ".codex" / "skills" / "lecturecast"
    assert installed.is_symlink()
    assert installed.resolve() == ROOT / "skills" / "codex"


def test_three_host_skills_reference_one_shared_director_workflow() -> None:
    shared = ROOT / "skills" / "shared" / "director-workflow.md"
    assert shared.exists()
    for host in ("codex", "claude-code", "openclaw"):
        content = (ROOT / "skills" / host / "SKILL.md").read_text(encoding="utf-8")
        assert content.startswith("---\nname: lecturecast\n")
        assert "../shared/director-workflow.md" in content
        assert "option_id" in content

    shared_content = shared.read_text(encoding="utf-8")
    assert "Do not record, export, or transmit outcome evidence automatically" in shared_content
    assert "share-anonymous-outcome" in shared_content
    assert "Never upload the file" in shared_content
