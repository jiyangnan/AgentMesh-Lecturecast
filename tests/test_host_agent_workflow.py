from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.host_agent import (
    HOST_WORKFLOW_CONTRACT_VERSION,
    HostWorkflowStore,
    host_adapter_status,
)
from lecturecast.project import ProjectStore


runner = CliRunner()
HOST_ARGS = [
    "--adapter",
    "codex",
    "--host-contract",
    HOST_WORKFLOW_CONTRACT_VERSION,
]


def test_current_installer_owned_skill_attests_loaded_contract() -> None:
    status = host_adapter_status("codex", HOST_WORKFLOW_CONTRACT_VERSION)

    assert status["ready"] is True
    assert status["installed"] is True
    assert status["installer_owned"] is True
    assert status["content_current"] is True
    assert status["expected_skill_digest"] == status["installed_skill_digest"]


def test_same_skill_without_loaded_contract_requires_a_new_host_task() -> None:
    status = host_adapter_status("codex", None)

    assert status["ready"] is False
    assert status["reason"] == "host_session_restart_required"
    assert status["bootstrap_argv"] == [
        "lecturecast",
        "onboard",
        "--adapter",
        "codex",
        "--host-contract",
        HOST_WORKFLOW_CONTRACT_VERSION,
        "--json",
    ]


def test_legacy_directory_skill_cannot_attest_even_if_it_copies_contract_text(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".codex" / "skills" / "lecturecast"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "old manual workflow with host-contract 1.0.0\n",
        encoding="utf-8",
    )

    status = host_adapter_status(
        "codex",
        HOST_WORKFLOW_CONTRACT_VERSION,
        home=tmp_path,
    )

    assert status["ready"] is False
    assert status["reason"] == "host_adapter_not_installer_owned"


def test_project_init_binds_skill_digest_and_agent_status_returns_one_next_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )
    created = runner.invoke(
        app,
        [
            "project",
            "init",
            str(tmp_path),
            "--name",
            "Host contract",
            *HOST_ARGS,
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    receipt = json.loads(
        (tmp_path / ".lecturecast" / "host-workflow.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["host_workflow"] == receipt
    assert receipt["adapter"]["skill_digest"].startswith("sha256:")

    from lecturecast.commands import agent as agent_module

    monkeypatch.setattr(
        agent_module,
        "onboarding_status",
        lambda *_args, **_kwargs: {
            "ok": True,
            "host_agent": {"ready": True},
            "workflow": {"next_action": {"id": "project.init"}},
            "user_prompt": None,
        },
    )
    status = runner.invoke(
        app,
        ["agent", "status", str(tmp_path), *HOST_ARGS, "--json"],
    )
    assert status.exit_code == 0, status.output
    workflow = json.loads(status.stdout)["workflow"]
    assert workflow["policy"] == "execute_only_returned_next_action"
    assert workflow["phase"] == "source_summary_required"
    assert workflow["next_action"]["id"] == "source.prepare"


def test_replacing_bound_skill_blocks_project_mutation_before_commercial_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectStore(tmp_path)
    project.init(name="Bound project")
    HostWorkflowStore(tmp_path).bind(
        adapter="codex",
        contract_version=HOST_WORKFLOW_CONTRACT_VERSION,
    )
    target = Path(os.environ["HOME"]) / ".codex" / "skills" / "lecturecast"
    target.unlink()
    target.mkdir()
    (target / "SKILL.md").write_text("legacy manual skill\n", encoding="utf-8")

    def unexpected() -> None:
        raise AssertionError("commercial/network gate must not run after host drift")

    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", unexpected
    )
    result = runner.invoke(
        app,
        ["project", "capabilities", str(tmp_path), "--json"],
    )

    assert result.exit_code == 1
    error = json.loads(result.stderr)
    assert error["code"] == "client_upgrade_required"
    assert error["cause"] == "host_adapter_not_installer_owned"
    assert not project.capabilities_path.exists()


def test_director_start_without_project_host_receipt_fails_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ProjectStore(tmp_path).init(name="Old project")
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "source_type": "topic",
                "title": "Host workflow",
                "summary": "这是一段已经核对的具体素材摘要，用于确认旧项目不能绕过宿主工作流门禁。",
                "language": "zh-CN",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def unexpected(_url: str):
        raise AssertionError("Director network must not run without host receipt")

    monkeypatch.setattr("lecturecast.commands.director._make_client", unexpected)
    result = runner.invoke(
        app,
        [
            "director",
            "start",
            str(tmp_path),
            "--source",
            str(source),
            "--adapter",
            "codex",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "client_upgrade_required"
    assert not (tmp_path / ".lecturecast" / "director-state.json").exists()
