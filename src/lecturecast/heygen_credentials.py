from __future__ import annotations

import importlib
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from .config import HEYGEN_KEYRING_USERNAME, KEYRING_SERVICE
from .errors import LectureCastError


HEYGEN_API_KEY_ENV = "HEYGEN_API_KEY"


class CredentialBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class HeyGenCredentialStatus:
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
    sources = os.environ if environment is None else environment
    value = sources.get(HEYGEN_API_KEY_ENV)
    if value is None or not value.strip():
        return None
    return value.strip()


def _stored_key(backend: CredentialBackend | None) -> str | None:
    if backend is None:
        return None
    try:
        value = backend.get_password(KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME)
    except Exception:
        # Provider credentials are optional until a photo presenter is chosen.
        # A locked/unavailable OS keyring is therefore reported as unconfigured;
        # the interactive configure command will surface a safe storage error.
        return None
    return value.strip() if value and value.strip() else None


def get_heygen_api_key(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> str | None:
    environment_value = _environment_key(environment)
    if environment_value is not None:
        return environment_value
    selected = backend if backend is not None else _load_keyring()
    return _stored_key(selected)


def heygen_credential_status(
    *,
    environment: Mapping[str, str] | None = None,
    backend: CredentialBackend | None = None,
) -> HeyGenCredentialStatus:
    if _environment_key(environment) is not None:
        return HeyGenCredentialStatus(
            configured=True,
            source="environment",
            environment_override=True,
        )
    selected = backend if backend is not None else _load_keyring()
    stored = _stored_key(selected)
    return HeyGenCredentialStatus(
        configured=stored is not None,
        source="system_credential_store" if stored is not None else None,
        environment_override=False,
    )


def save_heygen_api_key(
    api_key: str, *, backend: CredentialBackend | None = None
) -> HeyGenCredentialStatus:
    value = api_key.strip()
    if len(value) < 8 or any(character.isspace() for character in value):
        raise LectureCastError(
            code="invalid_api_key",
            message="HeyGen API Key 格式无效。",
            next_action=(
                "请从 HeyGen Settings > API > API token 重新获取完整 Key，"
                "并在本机隐藏输入提示中重试。"
            ),
        )
    selected = backend if backend is not None else _load_keyring()
    if selected is None:
        raise LectureCastError(
            code="missing_credential",
            message="当前系统没有可用的安全凭证存储。",
            next_action=(
                "请在受支持的 macOS Keychain 或 Windows Credential Manager 环境中运行 "
                "lecturecast presenter configure；不要把 Key 写入项目文件。"
            ),
        )
    try:
        selected.set_password(KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME, value)
    except Exception as exc:
        raise LectureCastError(
            code="missing_credential",
            message="无法把 HeyGen API Key 保存到系统凭证存储。",
            next_action=("请检查系统凭证存储权限后重试；不要把 Key 粘贴到聊天、参数或日志。"),
            cause=type(exc).__name__,
        ) from None
    return HeyGenCredentialStatus(
        configured=True,
        source="system_credential_store",
        environment_override=False,
    )


def delete_stored_heygen_api_key(*, backend: CredentialBackend | None = None) -> None:
    selected = backend if backend is not None else _load_keyring()
    if selected is None:
        return
    try:
        selected.delete_password(KEYRING_SERVICE, HEYGEN_KEYRING_USERNAME)
    except Exception as exc:
        if type(exc).__name__ in {"PasswordDeleteError", "KeyError"}:
            return
        raise LectureCastError(
            code="missing_credential",
            message="无法从系统凭证存储中删除 HeyGen 凭证。",
            next_action=("请在系统密码管理器中删除 agentmesh-lecturecast / heygen-api-key 条目。"),
            cause=type(exc).__name__,
        ) from None
