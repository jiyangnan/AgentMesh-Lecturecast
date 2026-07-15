from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import uuid
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from .config import PROJECT_DIRECTORY, PROJECT_SCHEMA_VERSION
from .errors import LectureCastError
from .manifest import verify_manifest
from .protocol import ClientCapabilities, CreativeBrief, ProductionManifest, canonical_digest


PROJECT_REQUIRED_FIELDS = {
    "schema_version",
    "project_id",
    "name",
    "project_revision",
    "status",
    "creative_brief_digest",
    "capability_digest",
    "production_manifest_digest",
    "created_at",
    "updated_at",
}
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KEYS = {"api_key", "authorization", "raw_content", "secret", "token"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _ensure_shareable(value: Any, *, location: str = "overrides") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in _SENSITIVE_KEYS:
                raise LectureCastError(
                    code="manifest_incompatible",
                    message=f"{location}.{key} 不能写入可分享项目。",
                    next_action="请把凭证和原始素材保留在系统凭证库或本机素材目录。",
                )
            _ensure_shareable(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _ensure_shareable(child, location=f"{location}[{index}]")
    elif isinstance(value, str):
        if value.startswith(("/", "~/")) or _WINDOWS_ABSOLUTE.match(value):
            raise LectureCastError(
                code="manifest_incompatible",
                message=f"{location} 不能包含真实绝对路径。",
                next_action="请使用 asset:// 逻辑引用或仅保存在本机、不分享的素材映射。",
            )


def atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    """Write a complete JSON file or leave the previous bytes untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LectureCastError(
            code="manifest_incompatible",
            message="拒绝写入符号链接项目文件。",
            next_action="请移除 .lecturecast 中的符号链接后重试。",
        )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message=f"项目文件 {path.name} 无法读取。",
            next_action="请从版本控制或备份恢复该文件，再运行 project resume。",
            cause=type(exc).__name__,
        ) from None
    if not isinstance(payload, dict):
        raise LectureCastError(
            code="manifest_incompatible",
            message=f"项目文件 {path.name} 必须是 JSON 对象。",
            next_action="请修复文件格式后重试。",
        )
    return payload


@dataclass(frozen=True)
class ProjectState:
    payload: dict[str, Any]

    @property
    def revision(self) -> int:
        return int(self.payload["project_revision"])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class ProjectStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / PROJECT_DIRECTORY
        self.project_path = self.directory / "project.json"
        self.brief_path = self.directory / "creative-brief.json"
        self.capabilities_path = self.directory / "client-capabilities.json"
        self.manifest_path = self.directory / "production-manifest.json"
        self.overrides_path = self.directory / "local-overrides.json"
        self.assets_directory = self.directory / "assets"
        self.lock_path = self.directory / ".project.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def init(self, *, name: str, project_id: str | None = None) -> ProjectState:
        clean_name = name.strip()
        if not clean_name:
            raise LectureCastError(
                code="manifest_incompatible",
                message="项目名称不能为空。",
                next_action="请提供一个可识别的本地项目名称。",
            )
        with self._locked():
            if self.project_path.exists():
                raise LectureCastError(
                    code="generation_conflict",
                    message="当前目录已经存在 LectureCast 项目。",
                    next_action="运行 lecturecast project resume，或选择另一个目录。",
                )
            now = _utc_now()
            identifier = project_id or f"project_{uuid.uuid4().hex}"
            payload: dict[str, Any] = {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "project_id": identifier,
                "name": clean_name,
                "project_revision": 1,
                "status": "initialized",
                "creative_brief_digest": None,
                "capability_digest": None,
                "production_manifest_digest": None,
                "created_at": now,
                "updated_at": now,
            }
            atomic_write_json(self.project_path, payload)
            atomic_write_json(
                self.overrides_path,
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "project_id": identifier,
                    "manifest_digest": None,
                    "overrides": {},
                },
            )
            self.assets_directory.mkdir(mode=0o700, exist_ok=True)
            return ProjectState(payload)

    def _load_unlocked(self) -> ProjectState:
        try:
            payload = _read_object(self.project_path)
        except FileNotFoundError:
            raise LectureCastError(
                code="session_not_found",
                message="当前目录没有 LectureCast 项目。",
                next_action="先运行 lecturecast project init。",
            ) from None
        missing = PROJECT_REQUIRED_FIELDS - payload.keys()
        if payload.get("schema_version") != PROJECT_SCHEMA_VERSION or missing:
            fields = ", ".join(sorted(missing)) or "schema_version"
            raise LectureCastError(
                code="client_upgrade_required",
                message="本地项目格式需要迁移。",
                next_action=f"请备份 .lecturecast 后运行新版迁移工具；待迁移字段：{fields}。",
            )
        if not isinstance(payload["project_revision"], int) or payload["project_revision"] < 1:
            raise LectureCastError(
                code="manifest_incompatible",
                message="project_revision 无效。",
                next_action="请从备份恢复 project.json。",
            )
        return ProjectState(payload)

    def load(self) -> ProjectState:
        with self._locked():
            state = self._load_unlocked()
            self._verify_documents(state)
            return state

    def _verify_documents(self, state: ProjectState) -> None:
        expected_brief = state.payload["creative_brief_digest"]
        if expected_brief is not None:
            brief = CreativeBrief.model_validate(_read_object(self.brief_path))
            if canonical_digest(brief) != expected_brief:
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="Creative Brief 与项目索引不一致。",
                    next_action="请恢复匹配的 creative-brief.json。",
                )
        expected_manifest = state.payload["production_manifest_digest"]
        if expected_manifest is not None:
            manifest = ProductionManifest.model_validate(_read_object(self.manifest_path))
            if canonical_digest(manifest) != expected_manifest:
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="ProductionManifest 原件已被修改。",
                    next_action="请恢复云端签发的原始 Manifest；本地修改应写入 local-overrides.json。",
                )
        expected_capabilities = state.payload["capability_digest"]
        if expected_capabilities is not None:
            capabilities = ClientCapabilities.model_validate(_read_object(self.capabilities_path))
            if canonical_digest(capabilities) != expected_capabilities:
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="ClientCapabilities 与项目索引不一致。",
                    next_action="重新采集能力，并用新的 capability digest 请求 Manifest。",
                )

    @staticmethod
    def _check_revision(state: ProjectState, expected_revision: int) -> None:
        if state.revision != expected_revision:
            raise LectureCastError(
                code="project_revision_conflict",
                message="项目已被另一个 Agent 更新。",
                next_action="重新运行 project resume，基于最新 project_revision 再提交。",
                retryable=True,
            )

    def save_brief(
        self,
        brief: CreativeBrief | dict[str, Any],
        *,
        expected_revision: int,
    ) -> ProjectState:
        document = brief if isinstance(brief, CreativeBrief) else CreativeBrief.model_validate(brief)
        with self._locked():
            state = self._load_unlocked()
            self._check_revision(state, expected_revision)
            atomic_write_json(self.brief_path, document.model_dump())
            return self._advance(
                state,
                status="brief_ready",
                creative_brief_digest=canonical_digest(document),
            )

    def save_capabilities(
        self,
        capabilities: ClientCapabilities | dict[str, Any],
        *,
        expected_revision: int,
    ) -> ProjectState:
        document = (
            capabilities
            if isinstance(capabilities, ClientCapabilities)
            else ClientCapabilities.model_validate(capabilities)
        )
        with self._locked():
            state = self._load_unlocked()
            self._check_revision(state, expected_revision)
            atomic_write_json(self.capabilities_path, document.model_dump())
            return self._advance(state, capability_digest=canonical_digest(document))

    def save_manifest(
        self,
        manifest: ProductionManifest | dict[str, Any],
        *,
        expected_revision: int,
    ) -> ProjectState:
        document = (
            manifest if isinstance(manifest, ProductionManifest) else ProductionManifest.model_validate(manifest)
        )
        serialized = _json_bytes(document.model_dump())
        with self._locked():
            state = self._load_unlocked()
            self._check_revision(state, expected_revision)
            if self.manifest_path.exists():
                if self.manifest_path.read_bytes() == serialized:
                    return state
                raise LectureCastError(
                    code="generation_conflict",
                    message="ProductionManifest 原件不可覆盖。",
                    next_action="保留原件，并把时间线或样式调整写入 local-overrides.json。",
                )
            capability_digest = state.payload["capability_digest"]
            if capability_digest is not None and document.payload["capability_digest"] != capability_digest:
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="Manifest 没有绑定当前保存的 ClientCapabilities。",
                    next_action="用当前 capability digest 重新请求 ProductionManifest。",
                )
            verify_manifest(document)
            atomic_write_json(self.manifest_path, document.model_dump(), mode=0o444)
            return self._advance(
                state,
                status="manifest_ready",
                production_manifest_digest=canonical_digest(document),
            )

    def save_overrides(
        self,
        overrides: dict[str, Any],
        *,
        expected_revision: int,
    ) -> ProjectState:
        _ensure_shareable(overrides)
        with self._locked():
            state = self._load_unlocked()
            self._check_revision(state, expected_revision)
            manifest_digest = state.payload["production_manifest_digest"]
            if manifest_digest is None:
                raise LectureCastError(
                    code="brief_not_ready",
                    message="还没有可调整的 ProductionManifest。",
                    next_action="先保存并验证云端签发的 Manifest。",
                )
            next_state = self._next_state(state)
            atomic_write_json(
                self.overrides_path,
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "project_id": state.payload["project_id"],
                    "manifest_digest": manifest_digest,
                    "overrides": overrides,
                },
            )
            atomic_write_json(self.project_path, next_state)
            return ProjectState(next_state)

    def _next_state(self, state: ProjectState, **changes: Any) -> dict[str, Any]:
        payload = state.to_dict()
        payload.update(changes)
        payload["project_revision"] = state.revision + 1
        payload["updated_at"] = _utc_now()
        return payload

    def _advance(self, state: ProjectState, **changes: Any) -> ProjectState:
        payload = self._next_state(state, **changes)
        atomic_write_json(self.project_path, payload)
        return ProjectState(payload)

    def ensure_manifest_read_only(self) -> None:
        if not self.manifest_path.exists():
            return
        current = stat.S_IMODE(self.manifest_path.stat().st_mode)
        os.chmod(self.manifest_path, current & ~0o222)
