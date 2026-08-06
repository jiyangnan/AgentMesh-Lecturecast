from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lecturecast.auth import AuthStatus
from lecturecast.cli import app
from lecturecast.commercial import CommercialAccess
from lecturecast.commands import onboard as onboard_module
from lecturecast.config import PROTOCOL_VERSION_ENV
from lecturecast.director import DirectorClient
from lecturecast.director import DirectorStateStore


runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


class SessionTransport:
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = session
        self.create_payload: dict[str, Any] | None = None

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        del timeout
        assert headers["Authorization"].startswith("Bearer ")
        assert method == "POST"
        assert url.endswith("/v1/director/sessions")
        assert payload is not None
        self.create_payload = payload
        return 201, self.session


def _commercial_access() -> CommercialAccess:
    return CommercialAccess(
        valid=True,
        usable=True,
        reason="ready",
        legacy_tier="free",
        pass_status="active",
        credit=100,
        source="monthly_pass",
        expires_at="2026-09-06T00:00:00Z",
        required_credits=10,
        paid_pass_required=False,
        account_url="https://agentmesh360.com/app/",
        pricing_url="https://agentmesh360.com/app/#pricing",
        next_suggested="lecturecast doctor --json",
    )


def test_fresh_official_workflow_defaults_to_v1_1_and_surfaces_avatar_bgm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROTOCOL_VERSION_ENV, raising=False)
    monkeypatch.setattr(
        onboard_module, "auth_status", lambda: AuthStatus(True, "fixture", False)
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: "fixture_key")
    monkeypatch.setattr(
        onboard_module,
        "_director",
        lambda: {
            "reachable": True,
            "url": "https://director.example.test/v1",
            "status": "ok",
        },
    )

    class CommercialClientFixture:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "fixture_key"

        def access(self) -> CommercialAccess:
            return _commercial_access()

    monkeypatch.setattr(onboard_module, "CommercialClient", CommercialClientFixture)
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.director.require_commercial_access", lambda: None
    )

    onboard = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )
    assert onboard["ok"] is True
    assert onboard["renderer"]["capabilities"]["schema_version"] == "1.1"
    assert onboard["renderer"]["capabilities"]["adapter"] == {
        "kind": "codex",
        "version": "1.0.0",
    }
    assert onboard["contracts"] == {
        "host_workflow": {
            "version": "1.0.0",
            "purpose": "installer_owned_skill_attestation",
        },
        "director_protocol": {
            "version": "1.1",
            "purpose": "cloud_director_session",
            "locked_at": "session_creation",
        },
    }
    assert onboard["workflow"]["next_action"]["id"] == "project.init"

    project_root = tmp_path / "customer-project"
    argv = list(onboard["workflow"]["next_action"]["argv"])
    argv[argv.index("<project-path>")] = str(project_root)
    argv[argv.index("<name>")] = "Customer v1.1"
    created = runner.invoke(app, argv[1:])
    assert created.exit_code == 0, created.output
    created_payload = json.loads(created.stdout)
    assert created_payload["contracts"]["director_protocol"]["version"] == "1.1"
    start_argv = created_payload["workflow"]["next_action"]["then_argv"]

    capabilities = runner.invoke(
        app,
        [
            "project",
            "capabilities",
            str(project_root),
            "--adapter",
            "codex",
            "--adapter-version",
            "1.0.0",
            "--json",
        ],
    )
    assert capabilities.exit_code == 0, capabilities.output
    assert json.loads(capabilities.stdout)["capabilities"]["schema_version"] == "1.1"

    source = project_root / "source-summary.json"
    source.write_text(
        json.dumps(
            {
                "source_type": "topic",
                "title": "客户默认协议验收",
                "summary": "这是经过用户确认的有界事实摘要，用来验证最新版默认展示数字人与背景音乐决策。",
                "language": "zh-CN",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    start_argv[start_argv.index("<source-summary.json>")] = str(source)
    start_argv.extend(["--server", "https://director.example.test"])

    session = json.loads(
        (FIXTURES / "customer-default-v1_1-session.json").read_text(encoding="utf-8")
    )
    transport = SessionTransport(session)
    monkeypatch.setattr(
        "lecturecast.commands.director._make_client",
        lambda url: DirectorClient(url, api_key="fixture_key", transport=transport),
    )
    started = runner.invoke(app, start_argv[1:])
    assert started.exit_code == 0, started.output
    payload = json.loads(started.stdout)

    assert transport.create_payload is not None
    assert transport.create_payload["protocol_version"] == "1.1"
    assert payload["director"]["protocol_version"] == "1.1"
    assert payload["contracts"]["host_workflow"]["version"] == "1.0.0"
    assert payload["contracts"]["director_protocol"]["version"] == "1.1"
    questions = {
        question["question_id"]: question
        for question in payload["decision_card_set"]["questions"]
    }
    assert set(questions) == {"presenter", "voice_mode", "bgm"}
    assert {item["option_id"] for item in questions["presenter"]["options"]} == {
        "none",
        "photo",
    }
    assert {item["option_id"] for item in questions["bgm"]["options"]} == {
        "none",
        "light_tech",
        "bright_launch",
    }
    assert payload["workflow"]["next_action"]["id"] == "director.answer"


def test_existing_v1_0_project_keeps_locked_protocol_after_client_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROTOCOL_VERSION_ENV, raising=False)
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
            "Locked v1.0",
            "--adapter",
            "codex",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    DirectorStateStore(tmp_path).create(
        server_url="https://director.example.test/v1",
        session={
            "session_id": "session_locked_v1_0",
            "status": "collecting_decisions",
            "brief_version": 0,
            "catalog_version": "2026-07-16.1",
            "updated_at": "2026-08-06T00:00:00Z",
        },
        adapter_kind="codex",
        adapter_version="1.0.0",
        protocol_version="1.0",
    )

    resumed = runner.invoke(
        app,
        [
            "project",
            "resume",
            str(tmp_path),
            "--adapter",
            "codex",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["contracts"]["director_protocol"]["version"] == "1.0"

    capabilities = runner.invoke(
        app,
        [
            "project",
            "capabilities",
            str(tmp_path),
            "--adapter",
            "codex",
            "--adapter-version",
            "1.0.0",
            "--json",
        ],
    )
    assert capabilities.exit_code == 0, capabilities.output
    capability_payload = json.loads(capabilities.stdout)
    assert capability_payload["contracts"]["director_protocol"]["version"] == "1.0"
    assert capability_payload["capabilities"]["schema_version"] == "1.0"
    assert DirectorStateStore(tmp_path).load().protocol_version == "1.0"
