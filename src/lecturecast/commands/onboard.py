from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..auth import auth_status, get_api_key
from ..capabilities import capture_capabilities, doctor_report
from ..commercial import CommercialClient, missing_commercial_access
from ..config import ACCOUNT_URL, PRICING_URL
from ..director import probe_director
from ..errors import LectureCastError
from ..host_agent import (
    HOST_WORKFLOW_CONTRACT_VERSION,
    HostWorkflowStore,
    host_adapter_status,
)
from .output import emit


def _renderer(project_root: Path | None = None) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[3]
    return doctor_report(
        capture_capabilities(
            project_root=project_root or Path.cwd(),
            repo_root=repo_root,
        )
    )


def _director() -> dict[str, Any]:
    try:
        return probe_director()
    except LectureCastError as error:
        return {
            "reachable": False,
            "url": None,
            "status": "unavailable",
            "error": {
                **error.to_dict(),
                "code": "director_unavailable",
            },
        }


def onboarding_status(
    project_root: Path | None = None,
    *,
    adapter: str | None = None,
    host_contract: str | None = None,
) -> dict[str, Any]:
    host_agent = host_adapter_status(adapter, host_contract)
    credential = auth_status()
    key = get_api_key()
    account_error: dict[str, Any] | None = None
    if key is None:
        access = missing_commercial_access()
    else:
        try:
            access = CommercialClient(api_key=key).access()
        except LectureCastError as error:
            access = missing_commercial_access()
            account_error = error.to_dict()
            access = type(access)(
                **{
                    **access.to_dict(),
                    "reason": error.code,
                    "next_suggested": (
                        "lecturecast auth login"
                        if error.code not in {"core_unavailable"}
                        else "lecturecast onboard --json"
                    ),
                }
            )

    renderer = _renderer(project_root)
    director = (
        _director()
        if access.usable
        else {
            "reachable": None,
            "url": None,
            "status": "not_checked",
            "reason": "commercial_access_required",
        }
    )
    blocked_by: list[str] = []
    if not host_agent["ready"]:
        blocked_by.append(str(host_agent["reason"]))
    if not credential.configured:
        blocked_by.append("api_key_required")
    elif not access.valid:
        blocked_by.append(access.reason)
    elif not access.usable:
        blocked_by.append(access.reason)
    if project_root is not None and not renderer["ready"]:
        blocked_by.append("renderer_not_ready")
    if access.usable and not director["reachable"]:
        blocked_by.append("director_unavailable")

    ready = not blocked_by
    if not host_agent["ready"]:
        user_prompt = (
            "当前宿主 Agent 会话没有证明已加载本次安装的 LectureCast Skill。"
            "请停止当前流程，新建宿主 Agent 任务，读取最新版 Skill，并运行其中的精确 "
            "onboard 命令；不要在旧会话中手工继续。"
        )
        next_suggested = (
            " ".join(host_agent["bootstrap_argv"])
            if host_agent["bootstrap_argv"]
            else "重新运行官方安装器并新建受支持的宿主 Agent 任务"
        )
    elif not credential.configured:
        user_prompt = (
            "请前往 AgentMesh360 账户中心创建通用 API Key，然后运行 "
            "lecturecast auth login；完成后重新运行 lecturecast onboard --json。"
        )
        next_suggested = ACCOUNT_URL
    elif account_error is not None:
        user_prompt = str(account_error["message"])
        next_suggested = access.next_suggested
    elif not access.usable:
        user_prompt = (
            "当前 AgentMesh360 账户没有可用的付费 LectureCast 权限或不足 10 credits。"
        )
        next_suggested = PRICING_URL
    elif not director["reachable"]:
        user_prompt = (
            "商业账户已绑定，但 LectureCast Director 服务当前不可用；"
            "请保留本地项目并稍后重试。"
        )
        next_suggested = "lecturecast onboard --json"
    elif project_root is not None and not renderer["ready"]:
        user_prompt = "商业账户已绑定；请按 renderer.next_actions 补齐本地渲染能力。"
        next_suggested = "lecturecast doctor --json"
    else:
        user_prompt = None
        next_suggested = (
            "lecturecast project init <project-path> --name <name> "
            f"--adapter {adapter} --host-contract {HOST_WORKFLOW_CONTRACT_VERSION} --json"
        )

    if ready:
        next_action = {
            "id": "project.init",
            "kind": "command",
            "argv": [
                "lecturecast",
                "project",
                "init",
                "<project-path>",
                "--name",
                "<name>",
                "--adapter",
                adapter,
                "--host-contract",
                HOST_WORKFLOW_CONTRACT_VERSION,
                "--json",
            ],
            "mutates": True,
            "requires_user_approval": False,
        }
    elif not host_agent["ready"]:
        next_action = {
            "id": "host.restart",
            "kind": "new_host_task",
            "argv": host_agent["bootstrap_argv"],
            "mutates": False,
            "requires_user_approval": True,
        }
    elif project_root is not None and not renderer["ready"]:
        next_action = {
            "id": "renderer.setup",
            "kind": "local_setup",
            "steps": renderer["next_actions"],
            "then_argv": [
                "lecturecast",
                "agent",
                "status",
                str(project_root.expanduser().resolve()),
                "--adapter",
                adapter,
                "--host-contract",
                HOST_WORKFLOW_CONTRACT_VERSION,
                "--json",
            ],
            "mutates": True,
            "requires_user_approval": False,
        }
    else:
        next_action = {
            "id": "onboarding.blocked",
            "kind": "user_action",
            "target": next_suggested,
            "mutates": False,
            "requires_user_approval": True,
        }

    return {
        "ok": ready,
        "environment_healthy": bool(
            renderer["ready"] and director["reachable"] is not False
        ),
        "auth": {
            **credential.to_dict(),
            "valid": access.valid,
        },
        "account": (
            {
                "legacy_tier": access.legacy_tier,
                "pass_status": access.pass_status,
                "expires_at": access.expires_at,
            }
            if access.valid
            else None
        ),
        "cloud_access": access.to_dict(),
        "host_agent": host_agent,
        "director": director,
        "renderer": renderer,
        "workflow": {
            "ready": ready,
            "blocked_by": blocked_by,
            "requires_user_action": not ready,
            "next_suggested": next_suggested,
            "next_action": next_action,
        },
        "requires_user_action": not ready,
        "user_prompt": user_prompt,
        "next_suggested": next_suggested,
    }


def onboard(
    project_root: Path | None = typer.Option(
        None,
        "--project-root",
        help="LectureCast project containing remotion/node_modules.",
    ),
    adapter: str | None = typer.Option(
        None,
        "--adapter",
        help="Current native host: codex, claude-code, or openclaw.",
    ),
    host_contract: str | None = typer.Option(
        None,
        "--host-contract",
        help="Exact workflow contract declared by the currently loaded Skill.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Bind commercial access and report the next safe agent action."""
    payload = onboarding_status(
        project_root,
        adapter=adapter,
        host_contract=host_contract,
    )
    if (
        payload["ok"]
        and project_root is not None
        and (project_root / ".lecturecast" / "project.json").is_file()
    ):
        payload["host_workflow"] = HostWorkflowStore(project_root).bind(
            adapter=str(adapter),
            contract_version=str(host_contract),
        )
    message = (
        "LectureCast 商业工作流已就绪。"
        if payload["ok"]
        else f"LectureCast 尚未就绪：{', '.join(payload['workflow']['blocked_by'])}。"
    )
    if payload["user_prompt"]:
        message = f"{message}\n{payload['user_prompt']}"
    emit(payload, json_output=json_output, message=message)
