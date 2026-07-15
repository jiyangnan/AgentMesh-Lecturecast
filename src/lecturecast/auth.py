from __future__ import annotations

import importlib
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from .config import API_KEY_ENV, KEYRING_SERVICE, KEYRING_USERNAME
from .errors import LectureCastError


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class AuthStatus:
    configured: bool
    source: str | None
    environment_override: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_keyring() -> CredentialBackend | None:
    try:
        module = importlib.import_module("keyring")
    except ImportError:
        return None
    return module  # type: ignore[return-value]


def _environment_key(environment: Mapping[str, str] | None = None) -> str | None:
    value = (environment or os.environ).get(API_KEY_ENV)
    if value is None or not value.strip():
        return None
    return value.strip()


def _stored_key(backend: CredentialBackend | None) -> str | None:
    if backend is None:
        return None
    try:
        value = backend.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception as exc:
        raise LectureCastError(
            code="missing_credential",
            message="无法读取系统凭证存储。",
            next_action=f"请设置 {API_KEY_ENV} 环境变量后重试。",
            cause=type(exc).__name__,
        ) from None
    return value.strip() if value and value.strip() else None


def get_api_key(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> str | None:
    environment_value = _environment_key(environment)
    if environment_value is not None:
        return environment_value
    return _stored_key(backend if backend is not None else _load_keyring())


def require_api_key(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> str:
    value = get_api_key(environment=environment, backend=backend)
    if value is None:
        raise LectureCastError(
            code="missing_credential",
            message="尚未配置 LectureCast API Key。",
            next_action=f"运行 lecturecast auth login，或设置 {API_KEY_ENV}。",
        )
    return value


def auth_status(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> AuthStatus:
    if _environment_key(environment) is not None:
        return AuthStatus(configured=True, source="environment", environment_override=True)
    stored = _stored_key(backend if backend is not None else _load_keyring())
    return AuthStatus(
        configured=stored is not None,
        source="keyring" if stored is not None else None,
        environment_override=False,
    )


def save_api_key(api_key: str, *, backend: CredentialBackend | None = None) -> AuthStatus:
    value = api_key.strip()
    if len(value) < 16:
        raise LectureCastError(
            code="invalid_api_key",
            message="API Key 格式无效。",
            next_action="请重新复制完整的 LectureCast API Key。",
        )
    selected = backend if backend is not None else _load_keyring()
    if selected is None:
        raise LectureCastError(
            code="missing_credential",
            message="当前系统没有可用的安全凭证存储。",
            next_action=f"请使用 {API_KEY_ENV} 环境变量；不会把明文 Key 写入项目。",
        )
    try:
        selected.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
    except Exception as exc:
        raise LectureCastError(
            code="missing_credential",
            message="无法保存到系统凭证存储。",
            next_action=f"请改用 {API_KEY_ENV} 环境变量。",
            cause=type(exc).__name__,
        ) from None
    return AuthStatus(configured=True, source="keyring", environment_override=False)


def delete_stored_api_key(*, backend: CredentialBackend | None = None) -> None:
    selected = backend if backend is not None else _load_keyring()
    if selected is None:
        return
    try:
        selected.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception as exc:
        if type(exc).__name__ in {"PasswordDeleteError", "KeyError"}:
            return
        raise LectureCastError(
            code="missing_credential",
            message="无法从系统凭证存储中删除凭证。",
            next_action="请在系统密码管理器中删除 agentmesh-lecturecast 条目。",
            cause=type(exc).__name__,
        ) from None

