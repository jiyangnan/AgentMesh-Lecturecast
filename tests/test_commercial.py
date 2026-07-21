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
        balance: tuple[int, dict[str, Any]] = (200, {"balance": 42, "tier": "pro"}),
        subscription: tuple[int, dict[str, Any]] = (
            200,
            {
                "tier": "pro",
                "status": "active",
                "current_period_end": "2026-08-21T00:00:00Z",
            },
        ),
    ) -> None:
        self.balance = balance
        self.subscription = subscription
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if "/credits/balance" in url:
            return self.balance
        return self.subscription


def test_paid_account_with_enough_shared_credits_is_usable() -> None:
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
    assert transport.calls[0]["url"].endswith(
        "/v1/credits/balance?product=lecturecast"
    )
    assert all(call["headers"]["Authorization"] == f"Bearer {secret}" for call in transport.calls)
    assert secret not in json.dumps(access.to_dict())


@pytest.mark.parametrize(
    ("transport", "reason", "paid_pass_required"),
    [
        (
            FakeTransport(
                balance=(200, {"balance": 50, "tier": "free"}),
                subscription=(200, {"tier": "free", "status": "active"}),
            ),
            "paid_subscription_required",
            True,
        ),
        (
            FakeTransport(balance=(200, {"balance": 9, "tier": "pro"})),
            "insufficient_credits",
            False,
        ),
    ],
)
def test_commercial_access_fails_closed(
    transport: FakeTransport,
    reason: str,
    paid_pass_required: bool,
) -> None:
    access = CommercialClient(
        api_key="am_live_commercial_secret",
        core_url="https://core.example.test",
        transport=transport,
    ).access()

    assert access.usable is False
    assert access.reason == reason
    assert access.paid_pass_required is paid_pass_required
    assert access.next_suggested.endswith("#pricing")


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


def test_core_url_requires_a_safe_origin() -> None:
    assert normalize_core_url("http://localhost:9000/") == "http://localhost:9000"
    with pytest.raises(LectureCastError):
        normalize_core_url("http://core.example.test")
    with pytest.raises(LectureCastError):
        normalize_core_url("https://user:pass@core.example.test/path")
