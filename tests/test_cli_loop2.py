from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from lecturecast.cli import app


FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()


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

