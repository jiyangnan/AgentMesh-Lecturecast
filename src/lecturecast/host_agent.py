from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .config import CLIENT_VERSION, PROJECT_DIRECTORY
from .errors import LectureCastError
from .project import atomic_write_json


HOST_WORKFLOW_CONTRACT_VERSION = "1.0.0"
HOST_ADAPTER_VERSION = "1.0.0"
NATIVE_HOST_ADAPTERS = frozenset({"codex", "claude-code", "openclaw"})
HOST_WORKFLOW_SCHEMA_VERSION = "1.0"
HOST_WORKFLOW_FILENAME = "host-workflow.json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _skill_digest(adapter: str, *, repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    digest = hashlib.sha256()
    for path in (
        root / "skills" / adapter / "SKILL.md",
        root / "skills" / "shared" / "director-workflow.md",
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _adapter_targets(adapter: str, *, home: Path) -> list[Path]:
    if adapter == "codex":
        return [home / ".codex" / "skills" / "lecturecast"]
    if adapter == "claude-code":
        return [home / ".claude" / "skills" / "lecturecast"]
    if adapter == "openclaw":
        return [
            home / ".openclaw" / "skills" / "lecturecast",
            home / ".openclaw" / "workspace" / "skills" / "lecturecast",
        ]
    return []


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def host_adapter_status(
    adapter: str | None,
    contract_version: str | None,
    *,
    home: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    clean_adapter = (adapter or "").strip()
    root = repo_root or _repo_root()
    # Windows' expanduser() follows USERPROFILE and ignores a deliberately
    # isolated HOME. The installer and host-agent canaries both use HOME, so
    # honor it before falling back to the platform profile directory.
    user_home = home or Path(os.environ.get("HOME") or Path.home())
    bootstrap_argv = (
        [
            "lecturecast",
            "onboard",
            "--adapter",
            clean_adapter,
            "--host-contract",
            HOST_WORKFLOW_CONTRACT_VERSION,
            "--json",
        ]
        if clean_adapter in NATIVE_HOST_ADAPTERS
        else None
    )
    base: dict[str, Any] = {
        "required": True,
        "adapter": clean_adapter or None,
        "adapter_version": HOST_ADAPTER_VERSION,
        "contract_version": HOST_WORKFLOW_CONTRACT_VERSION,
        "declared_contract_version": contract_version,
        "contract_attested": contract_version == HOST_WORKFLOW_CONTRACT_VERSION,
        "installed": False,
        "installer_owned": False,
        "content_current": False,
        "ready": False,
        "reason": "host_adapter_required",
        "bootstrap_argv": bootstrap_argv,
    }
    if clean_adapter not in NATIVE_HOST_ADAPTERS:
        base["reason"] = (
            "host_adapter_required" if not clean_adapter else "unsupported_host_adapter"
        )
        return base

    expected = root / "skills" / clean_adapter
    expected_resolved = _resolved(expected)
    expected_digest = _skill_digest(clean_adapter, repo_root=root)
    targets = _adapter_targets(clean_adapter, home=user_home)
    installed_target = next((target for target in targets if target.exists()), targets[0])
    installed_resolved = _resolved(installed_target)
    installed = installed_resolved is not None and (installed_target / "SKILL.md").is_file()
    installer_owned = (
        installed
        and expected_resolved is not None
        and installed_resolved == expected_resolved
    )
    actual_digest: str | None = None
    if installed_resolved is not None and installed:
        try:
            actual_digest = _skill_digest(clean_adapter, repo_root=installed_resolved.parents[1])
        except (OSError, ValueError):
            actual_digest = None
    content_current = installer_owned and actual_digest == expected_digest
    contract_attested = contract_version == HOST_WORKFLOW_CONTRACT_VERSION
    reason = "ready"
    if not installed:
        reason = "host_adapter_not_installed"
    elif not installer_owned:
        reason = "host_adapter_not_installer_owned"
    elif not content_current:
        reason = "host_adapter_content_mismatch"
    elif not contract_attested:
        reason = "host_session_restart_required"

    return {
        **base,
        "installed": installed,
        "installer_owned": installer_owned,
        "content_current": content_current,
        "ready": content_current and contract_attested,
        "reason": reason,
        "expected_skill_digest": expected_digest,
        "installed_skill_digest": actual_digest,
        "installed_path": str(installed_target),
    }


def require_host_adapter(
    adapter: str,
    contract_version: str,
    *,
    home: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    status = host_adapter_status(
        adapter,
        contract_version,
        home=home,
        repo_root=repo_root,
    )
    if status["ready"]:
        return status
    raise LectureCastError(
        code="client_upgrade_required",
        message=(
            "当前宿主 Agent 没有加载由本次 LectureCast 安装提供的工作流合同。"
        ),
        next_action=(
            "停止当前流程；重新运行官方安装器，然后新建宿主 Agent 任务并读取最新版 "
            "LectureCast Skill。不要在当前旧会话中手工继续。"
        ),
        cause=str(status["reason"]),
    )


class HostWorkflowStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / PROJECT_DIRECTORY / HOST_WORKFLOW_FILENAME

    def bind(
        self,
        *,
        adapter: str,
        contract_version: str,
        home: Path | None = None,
        repo_root: Path | None = None,
    ) -> dict[str, Any]:
        status = require_host_adapter(
            adapter,
            contract_version,
            home=home,
            repo_root=repo_root,
        )
        receipt = {
            "schema_version": HOST_WORKFLOW_SCHEMA_VERSION,
            "receipt_id": f"host_workflow_{uuid.uuid4().hex}",
            "contract_version": HOST_WORKFLOW_CONTRACT_VERSION,
            "client_version": CLIENT_VERSION,
            "adapter": {
                "kind": adapter,
                "version": HOST_ADAPTER_VERSION,
                "skill_digest": status["expected_skill_digest"],
            },
            "bound_at": _utc_now(),
        }
        atomic_write_json(self.path, receipt)
        return receipt

    def load(self) -> dict[str, Any]:
        try:
            if self.path.is_symlink():
                raise ValueError("host workflow receipt must not be a symlink")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise LectureCastError(
                code="client_upgrade_required",
                message="当前项目尚未绑定新版宿主 Agent 工作流。",
                next_action=(
                    "新建宿主 Agent 任务并读取最新版 LectureCast Skill，然后运行带 "
                    "--adapter 和 --host-contract 的 project resume。"
                ),
                cause="host_workflow_receipt_missing",
            ) from None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise LectureCastError(
                code="manifest_incompatible",
                message="host-workflow.json 无法读取或不安全。",
                next_action="不要手工修改；在新版宿主 Agent 任务中重新运行 project resume。",
                cause=type(exc).__name__,
            ) from None
        if not isinstance(payload, dict):
            raise LectureCastError(
                code="manifest_incompatible",
                message="host-workflow.json 必须是 JSON 对象。",
                next_action="不要手工修改；在新版宿主 Agent 任务中重新运行 project resume。",
            )
        return payload

    def require_current(
        self,
        *,
        expected_adapter: str | None = None,
        home: Path | None = None,
        repo_root: Path | None = None,
    ) -> dict[str, Any]:
        payload = self.load()
        adapter = payload.get("adapter")
        if not isinstance(adapter, Mapping):
            adapter = {}
        kind = adapter.get("kind")
        valid_shape = (
            payload.get("schema_version") == HOST_WORKFLOW_SCHEMA_VERSION
            and payload.get("contract_version") == HOST_WORKFLOW_CONTRACT_VERSION
            and payload.get("client_version") == CLIENT_VERSION
            and isinstance(payload.get("receipt_id"), str)
            and isinstance(payload.get("bound_at"), str)
            and kind in NATIVE_HOST_ADAPTERS
            and adapter.get("version") == HOST_ADAPTER_VERSION
        )
        if not valid_shape or (expected_adapter is not None and kind != expected_adapter):
            raise LectureCastError(
                code="client_upgrade_required",
                message="当前项目的宿主 Agent 工作流收据已过期或与当前宿主不一致。",
                next_action=(
                    "新建宿主 Agent 任务并读取最新版 LectureCast Skill，然后运行带 "
                    "--adapter 和 --host-contract 的 project resume。"
                ),
                cause="host_workflow_receipt_stale",
            )
        status = require_host_adapter(
            str(kind),
            HOST_WORKFLOW_CONTRACT_VERSION,
            home=home,
            repo_root=repo_root,
        )
        if adapter.get("skill_digest") != status["expected_skill_digest"]:
            raise LectureCastError(
                code="client_upgrade_required",
                message="项目绑定的 LectureCast Skill 已被升级。",
                next_action="在新的宿主 Agent 任务中重新运行 project resume 后继续。",
                cause="host_workflow_skill_changed",
            )
        return payload


def require_project_host_workflow(
    root: Path | str,
    *,
    expected_adapter: str | None = None,
) -> dict[str, Any]:
    return HostWorkflowStore(root).require_current(expected_adapter=expected_adapter)
