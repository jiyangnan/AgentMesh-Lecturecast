from __future__ import annotations

import re

from typer.testing import CliRunner

from lecturecast.cli import app


runner = CliRunner()


def test_version_command_reports_package_name() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "lecturecast" in result.stdout


def test_workflow_requires_commercial_onboarding() -> None:
    result = runner.invoke(app, ["workflow"])
    output = " ".join(re.sub(r"[╭╮╰╯│─]+", " ", result.stdout).split())

    assert result.exit_code == 0
    assert "paid AgentMesh360 account" in output
    assert "lecturecast onboard --json" in output
    assert "workflow.ready" in output
    assert "Edge/MiniMax TTS" in output
    assert "Remotion render" in output


def test_public_cli_does_not_expose_release_evidence_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "dogfood" not in result.stdout.lower()
