from __future__ import annotations

from lecturecast.auth import AuthStatus
from lecturecast.commercial import CommercialAccess
from lecturecast.commands import onboard as onboard_module
from lecturecast.errors import LectureCastError


def _renderer(ready: bool) -> dict[str, object]:
    return {
        "ready": ready,
        "missing": [] if ready else ["ffmpeg_libass"],
        "next_actions": [] if ready else ["install ffmpeg-full"],
    }


def _access(*, usable: bool = True) -> CommercialAccess:
    return CommercialAccess(
        valid=True,
        usable=usable,
        reason="ready" if usable else "monthly_pass_required",
        legacy_tier="free",
        pass_status="active" if usable else "inactive",
        credit=50,
        source="monthly_pass" if usable else "none",
        expires_at="2026-08-21T00:00:00Z",
        required_credits=10,
        paid_pass_required=not usable,
        account_url="https://agentmesh360.com/app/",
        pricing_url="https://agentmesh360.com/app/#pricing",
        next_suggested=(
            "lecturecast doctor --json"
            if usable
            else "https://agentmesh360.com/app/#pricing"
        ),
    )


def _director(reachable: bool = True) -> dict[str, object]:
    return {
        "reachable": reachable,
        "url": "https://api.lecturecast.agentmesh360.com/v1",
        "status": "ok" if reachable else "unavailable",
    }


def test_missing_key_returns_agent_readable_user_action(monkeypatch) -> None:
    monkeypatch.setattr(
        onboard_module,
        "auth_status",
        lambda: AuthStatus(False, None, False),
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: None)
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(True))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director())

    result = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )

    assert result["ok"] is False
    assert result["requires_user_action"] is True
    assert result["workflow"]["blocked_by"] == ["api_key_required"]
    assert result["next_suggested"] == "https://agentmesh360.com/app/"
    assert "lecturecast auth login" in result["user_prompt"]


def test_unavailable_keychain_is_a_recoverable_onboarding_state(monkeypatch) -> None:
    def unavailable_keychain() -> AuthStatus:
        raise LectureCastError(
            code="missing_credential",
            message="无法读取系统凭证存储。",
            next_action="请设置 LECTURECAST_API_KEY 环境变量后重试。",
            cause="KeyringError",
        )

    monkeypatch.setattr(onboard_module, "auth_status", unavailable_keychain)
    monkeypatch.setattr(
        onboard_module,
        "get_api_key",
        lambda: (_ for _ in ()).throw(AssertionError("must not read twice")),
    )
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(True))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director())

    result = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )

    assert result["ok"] is False
    assert result["workflow"]["blocked_by"] == ["api_key_required"]
    assert result["auth"]["error"]["code"] == "missing_credential"
    assert result["auth"]["error"]["cause"] == "KeyringError"
    assert result["user_prompt"] == "无法读取系统凭证存储。"
    assert result["next_suggested"].startswith("请设置 LECTURECAST_API_KEY")


def test_paid_account_and_renderer_make_workflow_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        onboard_module,
        "auth_status",
        lambda: AuthStatus(True, "keyring", False),
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: "am_live_key")
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(True))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director())

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            assert api_key == "am_live_key"

        def access(self) -> CommercialAccess:
            return _access()

    monkeypatch.setattr(onboard_module, "CommercialClient", FakeClient)

    result = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )

    assert result["ok"] is True
    assert result["workflow"]["ready"] is True
    assert result["workflow"]["blocked_by"] == []
    assert result["account"]["legacy_tier"] == "free"
    assert result["account"]["pass_status"] == "active"
    assert result["cloud_access"]["source"] == "monthly_pass"
    assert result["cloud_access"]["required_credits"] == 10
    assert result["director"]["reachable"] is True
    assert result["next_suggested"].startswith("lecturecast project init")


def test_no_paid_access_never_falls_back_to_local_render(monkeypatch) -> None:
    monkeypatch.setattr(
        onboard_module,
        "auth_status",
        lambda: AuthStatus(True, "keyring", False),
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: "am_live_key")
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(True))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director())

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            pass

        def access(self) -> CommercialAccess:
            return _access(usable=False)

    monkeypatch.setattr(onboard_module, "CommercialClient", FakeClient)

    result = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )

    assert result["ok"] is False
    assert result["workflow"]["blocked_by"] == ["monthly_pass_required"]
    assert result["next_suggested"].endswith("#pricing")
    assert "project init" not in result["next_suggested"]


def test_unavailable_director_blocks_otherwise_ready_workflow(monkeypatch) -> None:
    monkeypatch.setattr(
        onboard_module,
        "auth_status",
        lambda: AuthStatus(True, "keyring", False),
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: "am_live_key")
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(True))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director(False))

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            pass

        def access(self) -> CommercialAccess:
            return _access()

    monkeypatch.setattr(onboard_module, "CommercialClient", FakeClient)

    result = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )

    assert result["ok"] is False
    assert result["environment_healthy"] is False
    assert result["workflow"]["blocked_by"] == ["director_unavailable"]
    assert result["next_suggested"] == "lecturecast onboard --json"


def test_project_specific_renderer_setup_is_a_staged_machine_action(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        onboard_module,
        "auth_status",
        lambda: AuthStatus(True, "keyring", False),
    )
    monkeypatch.setattr(onboard_module, "get_api_key", lambda: "am_live_key")
    monkeypatch.setattr(onboard_module, "_renderer", lambda _root=None: _renderer(False))
    monkeypatch.setattr(onboard_module, "_director", lambda: _director())

    class FakeClient:
        def __init__(self, *, api_key: str) -> None:
            pass

        def access(self) -> CommercialAccess:
            return _access()

    monkeypatch.setattr(onboard_module, "CommercialClient", FakeClient)

    before_project = onboard_module.onboarding_status(
        adapter="codex", host_contract="1.0.0"
    )
    in_project = onboard_module.onboarding_status(
        tmp_path,
        adapter="codex",
        host_contract="1.0.0",
    )

    assert before_project["ok"] is True
    assert before_project["renderer"]["ready"] is False
    assert before_project["workflow"]["next_action"]["id"] == "project.init"
    assert in_project["ok"] is False
    assert in_project["workflow"]["blocked_by"] == ["renderer_not_ready"]
    assert in_project["workflow"]["next_action"]["id"] == "renderer.setup"
    assert in_project["workflow"]["next_action"]["steps"] == [
        "install ffmpeg-full"
    ]
