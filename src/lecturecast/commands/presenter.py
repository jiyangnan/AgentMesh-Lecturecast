from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..capabilities import (
    build_heygen_doctor_section,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
)
from ..config import (
    HEYGEN_API_HELP_URL,
    HEYGEN_API_SETTINGS_URL,
)
from ..director import DirectorState, DirectorStateStore
from ..errors import LectureCastError
from ..heygen_credentials import (
    delete_stored_heygen_api_key,
    heygen_credential_status,
    save_heygen_api_key,
)
from ..host_agent import HOST_WORKFLOW_CONTRACT_VERSION, require_project_host_workflow
from ..project import ProjectStore
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


def _root(directory: Path | str) -> str:
    return str(Path(directory).expanduser().resolve())


def _brief_avatar(project_store: ProjectStore) -> str | None:
    try:
        brief = project_store.load_brief_dict()
        if not isinstance(brief, dict):
            return None
        presenter = brief.get("presenter")
        if not isinstance(presenter, dict):
            return None
        avatar = presenter.get("avatar")
        return avatar if isinstance(avatar, str) else None
    except Exception:
        return None


def heygen_key_user_prompt(root: str) -> str:
    return (
        "当前项目选择了 HeyGen 照片数字人，但本机尚未配置 HeyGen API Key。"
        f"请由用户自行注册或登录 HeyGen，并在 {HEYGEN_API_SETTINGS_URL} 的 "
        "Settings > API > API token 中生成或获取 Key。该 Key 是用户自带的第三方凭据，"
        "HeyGen 可能单独收费，与 AgentMesh360 credits 无关。不要把 Key 粘贴到聊天、"
        "命令参数、日志或 stdout。请在用户自己的本机终端运行 "
        f'`lecturecast presenter configure "{root}" --json`，并只在隐藏输入提示中输入；'
        "LectureCast 会把它保存到系统安全凭证存储，不写入项目，也不会上传到 "
        "AgentMesh360。"
    )


def heygen_configure_action(root: str) -> dict[str, Any]:
    return {
        "id": "presenter.heygen.configure",
        "kind": "interactive_secret_input",
        "argv": ["lecturecast", "presenter", "configure", root, "--json"],
        "provider": "heygen",
        "provider_setup_url": HEYGEN_API_SETTINGS_URL,
        "provider_help_url": HEYGEN_API_HELP_URL,
        "secret_input": {
            "prompt": "HeyGen API Key",
            "hidden": True,
            "transport": "local_tty",
            "storage": "system_credential_store",
        },
        "mutates": True,
        "requires_user_approval": True,
    }


def heygen_key_required_contract(
    directory: Path | str, *, generation_id: str | None
) -> dict[str, Any]:
    root = _root(directory)
    return {
        "requires_user_action": True,
        "user_prompt": heygen_key_user_prompt(root),
        "next_suggested": f'lecturecast presenter configure "{root}" --json',
        "local_generation": {
            "generation_id": generation_id,
            "cloud_submission_state": "not_submitted",
            "credit_deducted": False,
            "preserve_generation_id": generation_id is not None,
        },
        "workflow": {
            "ready": False,
            "phase": "heygen_credential_required",
            "blocked_by": ["heygen_key_required"],
            "requires_user_action": True,
            "policy": "execute_only_returned_next_action",
            "next_action": heygen_configure_action(root),
        },
    }


def reserved_generation_contract(
    directory: Path | str,
    state: DirectorState,
) -> dict[str, Any]:
    root = _root(directory)
    generation_id = state.generation_id
    if generation_id is None:
        raise ValueError("reserved generation requires generation_id")
    project_store = ProjectStore(directory)
    if state.protocol_version == "1.1" and _brief_avatar(project_store) == "photo":
        section = build_heygen_doctor_section(project_root=Path(root))
        if "key_missing" in section.get("blockers", []):
            return heygen_key_required_contract(root, generation_id=generation_id)
        if not section.get("configured"):
            blockers = ", ".join(section.get("blockers", [])) or "unknown"
            user_prompt = (
                "HeyGen 系统凭证已存在，但本地数字人组件仍未就绪："
                f"{blockers}。请执行唯一的 doctor 动作并按报告修复；"
                "保留当前 generation ID，不要创建新的 generation。"
            )
            return {
                "requires_user_action": True,
                "user_prompt": user_prompt,
                "next_suggested": f'lecturecast doctor --project-root "{root}" --json',
                "local_generation": {
                    "generation_id": generation_id,
                    "cloud_submission_state": "not_submitted",
                    "credit_deducted": False,
                    "preserve_generation_id": True,
                },
                "workflow": {
                    "ready": False,
                    "phase": "heygen_local_setup_required",
                    "blocked_by": list(section.get("blockers", [])),
                    "requires_user_action": True,
                    "policy": "execute_only_returned_next_action",
                    "next_action": {
                        "id": "lecturecast.doctor",
                        "kind": "command",
                        "argv": [
                            "lecturecast",
                            "doctor",
                            "--project-root",
                            root,
                            "--json",
                        ],
                        "mutates": False,
                        "requires_user_approval": False,
                    },
                },
            }
    return {
        "requires_user_action": False,
        "user_prompt": None,
        "next_suggested": (
            f'lecturecast director generate "{root}" --generation-id {generation_id} --json'
        ),
        "local_generation": {
            "generation_id": generation_id,
            "cloud_submission_state": "not_submitted",
            "credit_deducted": False,
            "preserve_generation_id": True,
        },
        "workflow": {
            "ready": True,
            "phase": "local_generation_ready_to_submit",
            "blocked_by": [],
            "requires_user_action": False,
            "policy": "execute_only_returned_next_action",
            "next_action": {
                "id": "director.generate.resume_reserved",
                "kind": "command",
                "argv": [
                    "lecturecast",
                    "director",
                    "generate",
                    root,
                    "--generation-id",
                    generation_id,
                    "--json",
                ],
                "mutates": True,
                "requires_user_approval": False,
            },
        },
    }


def is_locally_reserved_generation(state: DirectorState) -> bool:
    return state.generation_id is not None and state.payload.get("generation_status") in {
        "reserved",
        "reserved_downgrade",
    }


@app.command("configure")
def configure(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Securely store a user-owned HeyGen API Key, then resume the same generation."""
    try:
        state = DirectorStateStore(directory).load()
        if state.protocol_version != "1.1":
            raise LectureCastError(
                code="manifest_incompatible",
                message="HeyGen 照片数字人配置只属于 Director v1.1 项目。",
                next_action="旧 v1.0 项目保持锁定，不要修改其协议或本地状态。",
            )
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        api_key = typer.prompt(
            "HeyGen API Key（仅保存到本机系统凭证存储）",
            hide_input=True,
            confirmation_prompt=False,
            err=True,
        )
        credential = save_heygen_api_key(api_key)

        section = build_heygen_doctor_section(project_root=directory)
        if not section.get("configured"):
            blockers = list(section.get("blockers", []))
            raise LectureCastError(
                code="missing_credential",
                message="HeyGen Key 已保存，但本地数字人能力仍未就绪。",
                next_action=(
                    "运行 lecturecast doctor --project-root <project-path> --json，"
                    f"修复：{', '.join(blockers) or 'unknown'}；保留原 generation ID。"
                ),
            )

        receipt = require_project_host_workflow(directory)
        project_store = ProjectStore(directory)
        project = project_store.load()
        capabilities = capture_capabilities_v1_1(
            adapter_kind=str(receipt["adapter"]["kind"]),
            adapter_version=str(receipt["adapter"]["version"]),
            project_root=directory,
            repo_root=Path(__file__).resolve().parents[3],
            adapter_probe=default_heygen_adapter_probe,
            journal_probe=lambda: default_heygen_journal_probe(directory),
        )
        updated = project_store.save_capabilities(capabilities, expected_revision=project.revision)
        if is_locally_reserved_generation(state):
            continuation = reserved_generation_contract(directory, state)
        else:
            root = _root(directory)
            continuation = {
                "requires_user_action": False,
                "user_prompt": None,
                "next_suggested": "lecturecast agent status",
                "workflow": {
                    "ready": True,
                    "phase": "heygen_credential_configured",
                    "blocked_by": [],
                    "requires_user_action": False,
                    "policy": "execute_only_returned_next_action",
                    "next_action": {
                        "id": "agent.status",
                        "kind": "command",
                        "argv": [
                            "lecturecast",
                            "agent",
                            "status",
                            root,
                            "--adapter",
                            str(state.payload["adapter_kind"]),
                            "--host-contract",
                            HOST_WORKFLOW_CONTRACT_VERSION,
                            "--json",
                        ],
                        "mutates": False,
                        "requires_user_approval": False,
                    },
                },
            }
        emit(
            {
                "provider": "heygen",
                "credential": credential.to_dict(),
                "key_echoed": False,
                "project": updated.to_dict(),
                **continuation,
            },
            json_output=json_output,
            message="HeyGen API Key 已保存到本机系统凭证存储；明文未输出。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read HeyGen credential/readiness state without revealing the key."""
    try:
        state = DirectorStateStore(directory).load()
        section = build_heygen_doctor_section(project_root=directory)
        credential = heygen_credential_status()
        payload: dict[str, Any] = {
            "provider": "heygen",
            "credential": credential.to_dict(),
            "key_echoed": False,
            "readiness": section,
        }
        if is_locally_reserved_generation(state):
            payload.update(reserved_generation_contract(directory, state))
        emit(
            payload,
            json_output=json_output,
            message=(
                "HeyGen 本地凭证已配置。" if credential.configured else "HeyGen 本地凭证尚未配置。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("logout")
def logout(json_output: bool = typer.Option(False, "--json")) -> None:
    """Delete the stored HeyGen key; an environment override remains active."""
    try:
        delete_stored_heygen_api_key()
        emit(
            {
                "provider": "heygen",
                "deleted": True,
                "key_echoed": False,
            },
            json_output=json_output,
            message="HeyGen 系统凭证已删除；未输出任何 Key。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
