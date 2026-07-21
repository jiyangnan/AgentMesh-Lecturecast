from __future__ import annotations

from lecturecast.auth import AuthStatus
from lecturecast.commercial import CommercialAccess
from lecturecast.commands import onboard as onboard_module


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
        reason="ready" if usable else "paid_access_required",
        tier="pro" if usable else "free",
        subscription_status="active",
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

    result = onboard_module.onboarding_status()

    assert result["ok"] is False
    assert result["requires_user_action"] is True
    assert result["workflow"]["blocked_by"] == ["api_key_required"]
    assert result["next_suggested"] == "https://agentmesh360.com/app/"
    assert "lecturecast auth login" in result["user_prompt"]


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

    result = onboard_module.onboarding_status()

    assert result["ok"] is True
    assert result["workflow"]["ready"] is True
    assert result["workflow"]["blocked_by"] == []
    assert result["account"]["tier"] == "pro"
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

    result = onboard_module.onboarding_status()

    assert result["ok"] is False
    assert result["workflow"]["blocked_by"] == ["paid_access_required"]
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

    result = onboard_module.onboarding_status()

    assert result["ok"] is False
    assert result["environment_healthy"] is False
    assert result["workflow"]["blocked_by"] == ["director_unavailable"]
    assert result["next_suggested"] == "lecturecast onboard --json"
