from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.errors import LectureCastError


FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()


@pytest.fixture(autouse=True)
def allow_commercial_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.manifest.require_commercial_access", lambda: None
    )


def test_project_init_and_resume_are_machine_readable(tmp_path: Path) -> None:
    created = runner.invoke(
        app,
        ["project", "init", str(tmp_path), "--name", "CLI handoff", "--json"],
    )
    assert created.exit_code == 0, created.output
    initial = json.loads(created.stdout)

    resumed = runner.invoke(app, ["project", "resume", str(tmp_path), "--json"])
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout) == initial


@pytest.mark.parametrize("command", ["init", "resume"])
def test_project_commands_fail_closed_without_commercial_access(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny() -> None:
        raise LectureCastError(
            code="monthly_pass_required",
            message="需要有效的 AgentMesh360 月度通行证。",
            next_action="运行 lecturecast onboard --json。",
        )

    monkeypatch.setattr("lecturecast.commands.project.require_commercial_access", deny)
    result = runner.invoke(app, ["project", command, str(tmp_path), "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "monthly_pass_required"
    assert not (tmp_path / ".lecturecast").exists()


def test_manifest_verify_and_preflight_fixture() -> None:
    manifest = FIXTURE_DIR / "production-manifest-v1.json"
    capabilities = FIXTURE_DIR / "client-capabilities-v1.json"

    verified = runner.invoke(app, ["manifest", "verify", str(manifest), "--json"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["valid"] is True

    preflight = runner.invoke(
        app,
        [
            "manifest",
            "preflight",
            str(manifest),
            "--capabilities",
            str(capabilities),
            "--json",
        ],
    )
    assert preflight.exit_code == 0, preflight.output
    assert json.loads(preflight.stdout)["passed"] is True


def test_auth_login_only_accepts_hidden_prompt() -> None:
    result = runner.invoke(app, ["auth", "login", "--help"])

    assert result.exit_code == 0
    assert "API_KEY" not in result.output
    assert "--key" not in result.output
