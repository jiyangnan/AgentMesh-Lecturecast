from __future__ import annotations

import re

from typer.testing import CliRunner

from lecturecast.cli import app


runner = CliRunner()


def test_version_command_reports_package_name() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "lecturecast" in result.stdout


def test_workflow_preserves_local_community_contract() -> None:
    result = runner.invoke(app, ["workflow"])
    output = " ".join(re.sub(r"[╭╮╰╯│─]+", " ", result.stdout).split())

    assert result.exit_code == 0
    assert "fully local" in output
    assert "no cloud" in output
    assert "no account" in output
    assert "no API key" in output
    assert "Edge/MiniMax TTS" in output
    assert "Remotion render" in output
