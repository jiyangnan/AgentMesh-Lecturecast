from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from .auth import require_api_key
from .config import DIRECTOR_URL_ENV
from .errors import LectureCastError
from .project import ProjectStore, atomic_write_json
from .protocol import CreativeBrief, DecisionCardSet, ProductionManifest


DIRECTOR_STATE_SCHEMA_VERSION = "1.0"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024


def normalize_server_url(value: str) -> str:
    clean = value.strip().rstrip("/")
    parsed = urlparse(clean)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LectureCastError(
            code="core_unavailable",
            message="Director Server URL 无效。",
            next_action="使用不含凭证、query 或 fragment 的 HTTPS URL。",
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LectureCastError(
            code="core_unavailable",
            message="远程 Director Server 必须使用 HTTPS。",
            next_action="改用 HTTPS；本机 localhost 开发环境可以使用 HTTP。",
        )
    path = parsed.path.rstrip("/")
    if not path:
        return f"{clean}/v1"
    if not path.endswith("/v1"):
        raise LectureCastError(
            code="core_unavailable",
            message="Director Server URL 必须指向 v1 API。",
            next_action="使用以 /v1 结尾的 URL，或只提供 Server origin。",
        )
    return clean


def resolve_server_url(
    explicit: str | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    value = explicit or (environment or os.environ).get(DIRECTOR_URL_ENV)
    if value is None or not value.strip():
        raise LectureCastError(
            code="core_unavailable",
            message="尚未配置 Director Server URL。",
            next_action=f"设置 {DIRECTOR_URL_ENV}，或在 start 时传入 --server。",
        )
    return normalize_server_url(value)


class DirectorTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrlLibDirectorTransport:
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        request_headers = dict(headers)
        if payload is not None:
            data = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = int(error.code)
            body = error.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LectureCastError(
                code="core_unavailable",
                message="暂时无法连接 Director Server。",
                next_action="检查网络和 Server URL 后，用相同本地项目重试。",
                retryable=True,
            ) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise LectureCastError(
                code="core_unavailable",
                message="Director Server 响应超过安全大小限制。",
                next_action="稍后重试；不要绕过响应限制。",
                retryable=True,
            )
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LectureCastError(
                code="core_unavailable",
                message="Director Server 返回了无效响应。",
                next_action="稍后重试；本地项目状态未丢失。",
                retryable=status >= 500,
            ) from None
        if not isinstance(document, dict):
            raise LectureCastError(
                code="core_unavailable",
                message="Director Server 返回了无效响应。",
                next_action="稍后重试；本地项目状态未丢失。",
                retryable=status >= 500,
            )
        return status, document


class DirectorClient:
    def __init__(
        self,
        server_url: str,
        *,
        api_key: str | None = None,
        transport: DirectorTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.server_url = normalize_server_url(server_url)
        self._api_key = api_key or require_api_key()
        self.transport = transport or UrlLibDirectorTransport()
        self.timeout = timeout

    @staticmethod
    def _error(status: int, document: dict[str, Any]) -> LectureCastError:
        detail = document.get("detail")
        if not isinstance(detail, dict):
            return LectureCastError(
                code="core_unavailable",
                message="Director Server 请求失败。",
                next_action="稍后重试；本地状态会保留稳定 ID。",
                retryable=status >= 500,
            )
        return LectureCastError(
            code=str(detail.get("code") or "core_unavailable"),
            message=str(detail.get("message") or "Director Server 请求失败。"),
            next_action=str(detail.get("next_action") or "按 Server 提示修复后重试。"),
            retryable=bool(detail.get("retryable", False)),
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status, document = self.transport.request(
            method=method,
            url=f"{self.server_url}{path}",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
            },
            payload=payload,
            timeout=self.timeout,
        )
        if status >= 400:
            raise self._error(status, document)
        return document

    @staticmethod
    def _invalid_response(cause: str) -> LectureCastError:
        return LectureCastError(
            code="manifest_incompatible",
            message="Director Server 响应不符合公开协议。",
            next_action="升级客户端或稍后重试；不要根据未验证字段继续。",
            cause=cause,
        )

    @classmethod
    def _session(cls, document: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(document["session_id"], str):
                raise TypeError("session_id")
            if document["status"] not in {
                "collecting_decisions",
                "ready_to_confirm",
                "confirmed",
                "deleted",
            }:
                raise ValueError("status")
            if not isinstance(document["brief_version"], int):
                raise TypeError("brief_version")
            if not isinstance(document["catalog_version"], str):
                raise TypeError("catalog_version")
            if not isinstance(document["updated_at"], str):
                raise TypeError("updated_at")
            card_set = document.get("decision_card_set")
            if card_set is not None:
                DecisionCardSet.model_validate(card_set)
            brief = document.get("brief")
            if brief is not None:
                CreativeBrief.model_validate(brief)
        except (KeyError, TypeError, ValueError) as exc:
            raise cls._invalid_response(type(exc).__name__) from None
        return document

    @classmethod
    def _generation(cls, document: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(document["generation_id"], str):
                raise TypeError("generation_id")
            if document["status"] not in {
                "created",
                "pending_credit",
                "queued",
                "generating",
                "validating",
                "signing",
                "ready",
                "failed",
                "credit_return_pending",
                "credit_returned",
            }:
                raise ValueError("status")
            if not isinstance(document["updated_at"], str):
                raise TypeError("updated_at")
            manifest = document.get("manifest")
            if manifest is not None:
                ProductionManifest.model_validate(manifest)
        except (KeyError, TypeError, ValueError) as exc:
            raise cls._invalid_response(type(exc).__name__) from None
        return document

    def create_session(self, source: dict[str, Any]) -> dict[str, Any]:
        return self._session(
            self.request("POST", "/director/sessions", {"source": source})
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._session(
            self.request("GET", f"/director/sessions/{session_id}")
        )

    def answer(
        self,
        session_id: str,
        *,
        question_id: str,
        option_id: str,
        catalog_version: str,
        custom_text: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question_id": question_id,
            "option_id": option_id,
            "catalog_version": catalog_version,
        }
        if custom_text is not None:
            payload["custom_text"] = custom_text
        return self._session(
            self.request(
                "POST", f"/director/sessions/{session_id}/answers", payload
            )
        )

    def confirm_brief(
        self, session_id: str, *, expected_brief_version: int
    ) -> dict[str, Any]:
        return self._session(
            self.request(
                "POST",
                f"/director/sessions/{session_id}/brief/confirm",
                {"expected_brief_version": expected_brief_version},
            )
        )

    def create_generation(
        self,
        session_id: str,
        *,
        generation_id: str,
        expected_brief_version: int,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        return self._generation(
            self.request(
                "POST",
                f"/director/sessions/{session_id}/generations",
                {
                    "generation_id": generation_id,
                    "expected_brief_version": expected_brief_version,
                    "capabilities": capabilities,
                },
            )
        )

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        return self._generation(
            self.request("GET", f"/director/generations/{generation_id}")
        )

    def delete_session(self, session_id: str) -> dict[str, Any]:
        document = self.request("DELETE", f"/director/sessions/{session_id}")
        if (
            document.get("session_id") != session_id
            or document.get("deleted") is not True
            or not isinstance(document.get("content_deleted_at"), str)
        ):
            raise self._invalid_response("delete_response")
        return document


@dataclass(frozen=True)
class DirectorState:
    payload: dict[str, Any]

    @property
    def revision(self) -> int:
        return int(self.payload["state_revision"])

    @property
    def session_id(self) -> str:
        return str(self.payload["session_id"])

    @property
    def generation_id(self) -> str | None:
        value = self.payload.get("generation_id")
        return str(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class DirectorStateStore:
    def __init__(self, root: Path | str) -> None:
        self.project = ProjectStore(root)
        self.path = self.project.directory / "director-state.json"

    @staticmethod
    def _validate(payload: dict[str, Any]) -> DirectorState:
        required = {
            "schema_version",
            "project_id",
            "state_revision",
            "server_url",
            "session_id",
            "session_status",
            "brief_version",
            "catalog_version",
            "adapter_kind",
            "adapter_version",
            "generation_id",
            "generation_status",
            "updated_at",
        }
        if set(payload) != required:
            raise ValueError("unexpected or incomplete Director state")
        if payload.get("schema_version") != DIRECTOR_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported Director state version")
        revision = payload.get("state_revision")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("invalid Director state revision")
        normalize_server_url(str(payload["server_url"]))
        return DirectorState(payload)

    def _load_unlocked(self) -> DirectorState:
        project = self.project._load_unlocked()
        if self.path.is_symlink():
            raise ValueError("Director state must not be a symlink")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Director state is not an object")
        state = self._validate(payload)
        if state.payload["project_id"] != project.payload["project_id"]:
            raise ValueError("Director state belongs to another project")
        return state

    def load(self) -> DirectorState:
        try:
            with self.project._locked():
                return self._load_unlocked()
        except FileNotFoundError:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Director Session。",
                next_action="先运行 lecturecast director start。",
            ) from None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise LectureCastError(
                code="manifest_incompatible",
                message="director-state.json 无法读取或不安全。",
                next_action="从可信备份恢复，或创建新的本地项目。",
                cause=type(exc).__name__,
            ) from None

    def create(
        self,
        *,
        server_url: str,
        session: dict[str, Any],
        adapter_kind: str,
        adapter_version: str,
    ) -> DirectorState:
        with self.project._locked():
            project = self.project._load_unlocked()
            if self.path.exists():
                raise LectureCastError(
                    code="generation_conflict",
                    message="本地项目已经绑定 Director Session。",
                    next_action="运行 director next/status 恢复，或新建另一个本地项目。",
                )
            payload = {
                "schema_version": DIRECTOR_STATE_SCHEMA_VERSION,
                "project_id": project.payload["project_id"],
                "state_revision": 1,
                "server_url": normalize_server_url(server_url),
                "session_id": session["session_id"],
                "session_status": session["status"],
                "brief_version": int(session["brief_version"]),
                "catalog_version": session["catalog_version"],
                "adapter_kind": adapter_kind,
                "adapter_version": adapter_version,
                "generation_id": None,
                "generation_status": None,
                "updated_at": session["updated_at"],
            }
            state = self._validate(payload)
            atomic_write_json(self.path, state.payload)
            return state

    def update(
        self,
        state: DirectorState,
        *,
        session: dict[str, Any] | None = None,
        generation: dict[str, Any] | None = None,
        generation_id: str | None = None,
        generation_status: str | None = None,
        session_status: str | None = None,
        updated_at: str | None = None,
    ) -> DirectorState:
        with self.project._locked():
            current = self._load_unlocked()
            if current.revision != state.revision:
                raise LectureCastError(
                    code="project_revision_conflict",
                    message="Director 状态已被另一个 Agent 更新。",
                    next_action="重新运行 director next/status 后重试。",
                    retryable=True,
                )
            if session is not None and session.get("session_id") != current.session_id:
                raise LectureCastError(
                    code="generation_conflict",
                    message="Server 返回了不同的 Director Session。",
                    next_action="保留本地状态并重新查询原 session_id。",
                )
            if generation is not None:
                returned_id = generation.get("generation_id")
                expected_id = current.generation_id
                if expected_id is None or returned_id != expected_id:
                    raise LectureCastError(
                        code="generation_conflict",
                        message="Server 返回了不同的 generation_id。",
                        next_action="保留本地稳定 ID，不要保存或渲染该响应。",
                    )
            payload = current.to_dict()
            payload["state_revision"] = current.revision + 1
            if session is not None:
                payload.update(
                    {
                        "session_status": session["status"],
                        "brief_version": int(session["brief_version"]),
                        "catalog_version": session["catalog_version"],
                        "updated_at": session["updated_at"],
                    }
                )
            if generation is not None:
                payload.update(
                    {
                        "generation_id": generation["generation_id"],
                        "generation_status": generation["status"],
                        "updated_at": generation["updated_at"],
                    }
                )
            if generation_id is not None:
                payload["generation_id"] = generation_id
            if generation_status is not None:
                payload["generation_status"] = generation_status
            if session_status is not None:
                payload["session_status"] = session_status
            if updated_at is not None:
                payload["updated_at"] = updated_at
            next_state = self._validate(payload)
            atomic_write_json(self.path, next_state.payload)
            return next_state


def load_source_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise ValueError("source summary file must not be a symlink")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("source summary file is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message="Source summary JSON 无法读取。",
            next_action="提供不超过 64 KiB、只含 source_type/title/summary/language 的 JSON。",
            cause=type(exc).__name__,
        ) from None
    if isinstance(payload, dict) and set(payload) == {"source"}:
        payload = payload["source"]
    allowed = {"source_type", "title", "summary", "language"}
    if not isinstance(payload, dict) or set(payload) != allowed:
        raise LectureCastError(
            code="manifest_incompatible",
            message="Source summary JSON 字段不符合 Director v1。",
            next_action="只提供 source_type、title、summary 和 language；不要提供媒体或路径。",
        )
    if not all(isinstance(payload[key], str) and payload[key].strip() for key in allowed):
        raise LectureCastError(
            code="manifest_incompatible",
            message="Source summary JSON 字段必须是非空文本。",
            next_action="修复 source summary 后重试。",
        )
    return payload
