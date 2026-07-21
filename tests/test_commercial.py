from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from lecturecast.commercial import CommercialClient, normalize_core_url
from lecturecast.errors import LectureCastError


class FakeTransport:
    def __init__(
        self,
        *,
        balance: tuple[int, dict[str, Any]] = (
            200,
            {
                "balance": 42,
                "tier": "free",
                "unlimited": False,
                "source": "monthly_pass",
                "expires_at": "2026-08-21T00:00:00Z",
            },
        ),
    ) -> None:
        self.balance = balance
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        return self.balance


def test_active_monthly_pass_with_enough_shared_credits_is_usable() -> None:
    secret = "am_live_commercial_secret"
    transport = FakeTransport()

    access = CommercialClient(
        api_key=secret,
        core_url="https://core.example.test",
        transport=transport,
    ).access()

    assert access.valid is True
    assert access.usable is True
    assert access.reason == "ready"
    assert access.required_credits == 10
    assert access.credit == 42
    assert access.legacy_tier == "free"
    assert access.source == "monthly_pass"
    assert access.pass_status == "active"
    assert access.expires_at == "2026-08-21T00:00:00Z"
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/v1/credits/balance?product=lecturecast")
    assert all(call["headers"]["Authorization"] == f"Bearer {secret}" for call in transport.calls)
    assert secret not in json.dumps(access.to_dict())


def test_active_monthly_pass_is_usable_even_when_legacy_subscription_is_free() -> None:
    transport = FakeTransport(
        balance=(
            200,
            {
                "balance": 1000,
                "tier": "free",
                "source": "monthly_pass",
                "expires_at": "2026-08-20 12:00:00",
            },
        ),
    )

    access = CommercialClient(
        api_key="am_live_monthly_pass",
        core_url="https://core.example.test",
        transport=transport,
    ).access()

    assert access.usable is True
    assert access.reason == "ready"
    assert access.source == "monthly_pass"
    assert access.expires_at == "2026-08-20 12:00:00"
    assert access.legacy_tier == "free"
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("balance", "reason", "paid_pass_required", "status"),
    [
        (
            {"balance": 0, "tier": "free", "source": "none", "expires_at": None},
            "monthly_pass_required",
            True,
            "inactive",
        ),
        (
            {
                "balance": 50,
                "tier": "free",
                "source": "signup_trial",
                "expires_at": "2026-07-28T00:00:00Z",
            },
            "monthly_pass_required",
            True,
            "signup_trial",
        ),
        (
            {"balance": 0, "tier": "free", "source": "none", "expires_at": None},
            "monthly_pass_required",
            True,
            "inactive",
        ),
        (
            {
                "balance": 9,
                "tier": "free",
                "source": "monthly_pass",
                "expires_at": "2026-08-21T00:00:00Z",
            },
            "insufficient_credits",
            False,
            "active",
        ),
    ],
    ids=["free", "signup-trial", "expired-pass", "insufficient-paid-pass"],
)
def test_commercial_access_fails_closed(
    balance: dict[str, Any],
    reason: str,
    paid_pass_required: bool,
    status: str,
) -> None:
    transport = FakeTransport(balance=(200, balance))
    access = CommercialClient(
        api_key="am_live_commercial_secret",
        core_url="https://core.example.test",
        transport=transport,
    ).access()

    assert access.usable is False
    assert access.reason == reason
    assert access.paid_pass_required is paid_pass_required
    assert access.pass_status == status
    assert access.next_suggested.endswith("#pricing")
    assert len(transport.calls) == 1


def test_invalid_key_is_rejected_without_echoing_secret() -> None:
    secret = "am_live_never_echo_this"
    transport = FakeTransport(
        balance=(401, {"detail": {"code": "invalid_key"}}),
    )

    with pytest.raises(LectureCastError) as captured:
        CommercialClient(
            api_key=secret,
            core_url="https://core.example.test",
            transport=transport,
        ).access()

    assert captured.value.code == "invalid_key"
    assert secret not in json.dumps(captured.value.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "balance",
    [
        {"balance": 10, "tier": "free", "expires_at": None},
        {"balance": 10, "tier": "free", "source": "legacy", "expires_at": None},
        {"balance": -1, "tier": "free", "source": "monthly_pass", "expires_at": None},
    ],
)
def test_unknown_or_incomplete_balance_contract_fails_closed(
    balance: dict[str, Any],
) -> None:
    with pytest.raises(LectureCastError) as captured:
        CommercialClient(
            api_key="am_live_commercial_secret",
            core_url="https://core.example.test",
            transport=FakeTransport(balance=(200, balance)),
        ).access()

    assert captured.value.code == "core_unavailable"


def test_core_url_requires_a_safe_origin() -> None:
    assert normalize_core_url("http://localhost:9000/") == "http://localhost:9000"
    with pytest.raises(LectureCastError):
        normalize_core_url("http://core.example.test")
    with pytest.raises(LectureCastError):
        normalize_core_url("https://user:pass@core.example.test/path")
