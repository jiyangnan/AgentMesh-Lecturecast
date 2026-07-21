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


def onboarding_status(project_root: Path | None = None) -> dict[str, Any]:
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
    if not credential.configured:
        blocked_by.append("api_key_required")
    elif not access.valid:
        blocked_by.append(access.reason)
    elif not access.usable:
        blocked_by.append(access.reason)
    if not renderer["ready"]:
        blocked_by.append("renderer_not_ready")
    if access.usable and not director["reachable"]:
        blocked_by.append("director_unavailable")

    ready = not blocked_by
    if not credential.configured:
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
    elif not renderer["ready"]:
        user_prompt = "商业账户已绑定；请按 renderer.next_actions 补齐本地渲染能力。"
        next_suggested = "lecturecast doctor --json"
    else:
        user_prompt = None
        next_suggested = "lecturecast project init <project-path> --name <name> --json"

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
        "director": director,
        "renderer": renderer,
        "workflow": {
            "ready": ready,
            "blocked_by": blocked_by,
            "requires_user_action": not ready,
            "next_suggested": next_suggested,
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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Bind commercial access and report the next safe agent action."""
    payload = onboarding_status(project_root)
    message = (
        "LectureCast 商业工作流已就绪。"
        if payload["ok"]
        else f"LectureCast 尚未就绪：{', '.join(payload['workflow']['blocked_by'])}。"
    )
    if payload["user_prompt"]:
        message = f"{message}\n{payload['user_prompt']}"
    emit(payload, json_output=json_output, message=message)
