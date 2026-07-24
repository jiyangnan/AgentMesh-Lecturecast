from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.errors import LectureCastError
from lecturecast.host_agent import HostWorkflowStore
from lecturecast.project import ProjectStore


FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()
HOST_ARGS = ["--adapter", "codex", "--host-contract", "1.0.0"]


def _denied() -> None:
    raise LectureCastError(
        code="monthly_pass_required",
        message="monthly pass required",
        next_action="https://agentmesh360.com/app/#pricing",
    )


def _assert_denied(result) -> None:
    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["code"] == "monthly_pass_required"


def test_project_init_is_hard_gated_before_local_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", _denied
    )

    result = runner.invoke(
        app,
        [
            "project",
            "init",
            str(tmp_path),
            "--name",
            "blocked",
            *HOST_ARGS,
            "--json",
        ],
    )

    _assert_denied(result)
    assert not (tmp_path / ".lecturecast").exists()


def test_project_resume_and_capabilities_are_gated_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path)
    store.init(name="existing")
    HostWorkflowStore(tmp_path).bind(adapter="codex", contract_version="1.0.0")
    project_before = store.project_path.read_bytes()
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", _denied
    )

    resumed = runner.invoke(
        app, ["project", "resume", str(tmp_path), *HOST_ARGS, "--json"]
    )
    capabilities = runner.invoke(
        app, ["project", "capabilities", str(tmp_path), "--json"]
    )

    _assert_denied(resumed)
    _assert_denied(capabilities)
    assert store.project_path.read_bytes() == project_before
    assert not store.capabilities_path.exists()


def test_manifest_preflight_is_hard_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.manifest.require_commercial_access", _denied
    )

    result = runner.invoke(
        app,
        [
            "manifest",
            "preflight",
            str(FIXTURE_DIR / "production-manifest-v1.json"),
            "--capabilities",
            str(FIXTURE_DIR / "client-capabilities-v1.json"),
            "--json",
        ],
    )

    _assert_denied(result)


def test_director_resume_is_gated_before_local_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.director.require_commercial_access", _denied
    )

    result = runner.invoke(
        app,
        [
            "director",
            "resume",
            str(tmp_path),
            "--adapter",
            "codex",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )

    _assert_denied(result)
    assert not (tmp_path / ".lecturecast").exists()


def test_read_only_status_inspect_and_verify_remain_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ProjectStore(tmp_path).init(name="read-only")

    def unexpected_gate() -> None:
        raise AssertionError("read-only commands must not invoke the commercial gate")

    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", unexpected_gate
    )
    monkeypatch.setattr(
        "lecturecast.commands.manifest.require_commercial_access", unexpected_gate
    )
    manifest = FIXTURE_DIR / "production-manifest-v1.json"

    status = runner.invoke(app, ["project", "status", str(tmp_path), "--json"])
    inspected = runner.invoke(app, ["manifest", "inspect", str(manifest), "--json"])
    verified = runner.invoke(app, ["manifest", "verify", str(manifest), "--json"])

    assert status.exit_code == 0, status.output
    assert inspected.exit_code == 0, inspected.output
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["valid"] is True
