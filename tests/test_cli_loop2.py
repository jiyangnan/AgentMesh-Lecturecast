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


def test_manifest_full_script_requires_explicit_digest_bound_approval(tmp_path: Path) -> None:
    created = runner.invoke(
        app,
        ["project", "init", str(tmp_path), "--name", "Script review", "--json"],
    )
    assert created.exit_code == 0, created.output

    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    state = store.load()
    store.save_manifest(
        json.loads((FIXTURE_DIR / "production-manifest-v1.json").read_text()),
        expected_revision=state.revision,
    )

    review = runner.invoke(app, ["manifest", "review", str(tmp_path), "--json"])
    assert review.exit_code == 0, review.output
    review_payload = json.loads(review.stdout)
    assert review_payload["script"][0]["narration"]
    assert review_payload["approval"]["approved"] is False

    blocked = runner.invoke(app, ["manifest", "approval", str(tmp_path), "--json"])
    assert blocked.exit_code == 1
    assert json.loads(blocked.stderr)["code"] == "brief_not_ready"

    approved = runner.invoke(
        app,
        [
            "manifest",
            "approve",
            str(tmp_path),
            "--confirm-reviewed-script",
            "--json",
        ],
    )
    assert approved.exit_code == 0, approved.output

    ready = runner.invoke(app, ["manifest", "approval", str(tmp_path), "--json"])
    assert ready.exit_code == 0, ready.output
    assert json.loads(ready.stdout)["approved"] is True


def test_manifest_review_stays_read_only_but_approval_fails_closed_without_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = runner.invoke(
        app,
        ["project", "init", str(tmp_path), "--name", "Commercial approval", "--json"],
    )
    assert created.exit_code == 0, created.output

    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    state = store.load()
    store.save_manifest(
        json.loads((FIXTURE_DIR / "production-manifest-v1.json").read_text()),
        expected_revision=state.revision,
    )

    def deny() -> None:
        raise LectureCastError(
            code="monthly_pass_required",
            message="需要有效的 AgentMesh360 月度通行证。",
            next_action="运行 lecturecast onboard --json。",
        )

    monkeypatch.setattr("lecturecast.commands.manifest.require_commercial_access", deny)
    review = runner.invoke(app, ["manifest", "review", str(tmp_path), "--json"])
    assert review.exit_code == 0, review.output

    blocked = runner.invoke(
        app,
        [
            "manifest",
            "approve",
            str(tmp_path),
            "--confirm-reviewed-script",
            "--json",
        ],
    )
    assert blocked.exit_code == 1
    assert json.loads(blocked.stderr)["code"] == "monthly_pass_required"
    assert not store.manifest_approval_path.exists()


def test_auth_login_only_accepts_hidden_prompt() -> None:
    result = runner.invoke(app, ["auth", "login", "--help"])

    assert result.exit_code == 0
    assert "API_KEY" not in result.output
    assert "--key" not in result.output
