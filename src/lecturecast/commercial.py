from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode, urlparse

from .config import (
    ACCOUNT_URL,
    CLIENT_VERSION,
    CORE_URL_ENV,
    DEFAULT_CORE_URL,
    MANIFEST_CREDIT_COST,
    PRICING_URL,
)
from .errors import LectureCastError


MAX_RESPONSE_BYTES = 1024 * 1024


class CommercialTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]: ...


class UrlLibCommercialTransport:
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            body = error.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LectureCastError(
                code="core_unavailable",
                message="暂时无法连接 AgentMesh360 账户服务。",
                next_action="检查网络后重新运行 lecturecast onboard --json。",
                retryable=True,
            ) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise LectureCastError(
                code="core_unavailable",
                message="AgentMesh360 账户服务响应超过安全大小限制。",
                next_action="稍后重试，不要绕过响应限制。",
                retryable=True,
            )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LectureCastError(
                code="core_unavailable",
                message="AgentMesh360 账户服务返回了无效响应。",
                next_action="稍后重新运行 lecturecast onboard --json。",
                retryable=status >= 500,
            ) from None
        if not isinstance(document, dict):
            raise LectureCastError(
                code="core_unavailable",
                message="AgentMesh360 账户服务返回了无效响应。",
                next_action="稍后重新运行 lecturecast onboard --json。",
                retryable=status >= 500,
            )
        return status, document


def normalize_core_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    parsed = urlparse(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise LectureCastError(
            code="core_unavailable",
            message="AgentMesh360 Core URL 无效。",
            next_action="使用不含路径、凭证、query 或 fragment 的 HTTPS origin。",
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LectureCastError(
            code="core_unavailable",
            message="远程 AgentMesh360 Core 必须使用 HTTPS。",
            next_action="改用 HTTPS；本机开发环境可以使用 localhost HTTP。",
        )
    return clean


def resolve_core_url(environment: Mapping[str, str] | None = None) -> str:
    value = (environment or os.environ).get(CORE_URL_ENV, DEFAULT_CORE_URL)
    return normalize_core_url(value)


@dataclass(frozen=True)
class CommercialAccess:
    valid: bool
    usable: bool
    reason: str
    tier: str | None
    subscription_status: str | None
    credit: int | None
    source: str
    expires_at: str | None
    required_credits: int
    paid_pass_required: bool | None
    account_url: str
    pricing_url: str
    next_suggested: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommercialClient:
    def __init__(
        self,
        *,
        api_key: str,
        core_url: str | None = None,
        transport: CommercialTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.core_url = normalize_core_url(core_url or resolve_core_url())
        self.transport = transport or UrlLibCommercialTransport()
        self.timeout = timeout

    def _get(self, path: str, query: Mapping[str, str] | None = None) -> dict[str, Any]:
        suffix = f"?{urlencode(query)}" if query else ""
        status, document = self.transport.request(
            url=f"{self.core_url}{path}{suffix}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "X-LectureCast-Client-Version": CLIENT_VERSION,
            },
            timeout=self.timeout,
        )
        if status in {401, 403}:
            detail = document.get("detail")
            code = detail.get("code") if isinstance(detail, dict) else None
            raise LectureCastError(
                code=str(code or "invalid_api_key"),
                message="AgentMesh360 API Key 无效、已撤销、已过期或无 LectureCast 权限。",
                next_action=f"前往 {ACCOUNT_URL} 创建通用 API Key，然后运行 lecturecast auth login。",
            )
        if status >= 400:
            raise LectureCastError(
                code="core_unavailable",
                message="AgentMesh360 账户服务请求失败。",
                next_action="稍后重新运行 lecturecast onboard --json。",
                retryable=status >= 500,
            )
        return document

    def access(self) -> CommercialAccess:
        balance = self._get("/v1/credits/balance", {"product": "lecturecast"})
        subscription = self._get("/v1/subscriptions/current")
        try:
            credit = int(balance["balance"])
            tier = str(subscription.get("tier") or balance["tier"])
            subscription_status = str(subscription.get("status") or "active")
        except (KeyError, TypeError, ValueError):
            raise LectureCastError(
                code="core_unavailable",
                message="AgentMesh360 账户服务返回了不完整的商业状态。",
                next_action="稍后重新运行 lecturecast onboard --json。",
                retryable=True,
            ) from None
        paid = tier.lower() not in {"", "free"} and subscription_status == "active"
        enough_credit = credit >= MANIFEST_CREDIT_COST
        usable = paid and enough_credit
        reason = (
            "ready"
            if usable
            else "paid_subscription_required"
            if not paid
            else "insufficient_credits"
        )
        return CommercialAccess(
            valid=True,
            usable=usable,
            reason=reason,
            tier=tier,
            subscription_status=subscription_status,
            credit=credit,
            source="agentmesh_shared_credits",
            expires_at=(
                str(subscription["current_period_end"])
                if subscription.get("current_period_end") is not None
                else None
            ),
            required_credits=MANIFEST_CREDIT_COST,
            paid_pass_required=not paid,
            account_url=ACCOUNT_URL,
            pricing_url=PRICING_URL,
            next_suggested=(
                "lecturecast doctor --json" if usable else PRICING_URL
            ),
        )


def missing_commercial_access() -> CommercialAccess:
    return CommercialAccess(
        valid=False,
        usable=False,
        reason="api_key_required",
        tier=None,
        subscription_status=None,
        credit=None,
        source="none",
        expires_at=None,
        required_credits=MANIFEST_CREDIT_COST,
        paid_pass_required=None,
        account_url=ACCOUNT_URL,
        pricing_url=PRICING_URL,
        next_suggested="lecturecast auth login",
    )


def require_commercial_access() -> CommercialAccess:
    from .auth import require_api_key

    access = CommercialClient(api_key=require_api_key()).access()
    if access.usable:
        return access
    if access.reason == "paid_subscription_required":
        message = "当前 AgentMesh360 账户没有有效的付费 LectureCast 权限。"
    else:
        message = "当前 AgentMesh360 账户不足 10 credits，无法继续 LectureCast。"
    raise LectureCastError(
        code=access.reason,
        message=message,
        next_action=f"前往 {PRICING_URL} 开通或补充 credits，然后重新运行 lecturecast onboard --json。",
    )
