from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..director import DirectorStateStore
from ..errors import LectureCastError
from ..host_agent import (
    HOST_WORKFLOW_CONTRACT_VERSION,
    NATIVE_HOST_ADAPTERS,
    HostWorkflowStore,
    host_adapter_status,
)
from ..project import ProjectStore
from .onboard import onboarding_status
from .output import emit


app = typer.Typer(no_args_is_help=True, add_completion=False)


def _command(action_id: str, argv: list[str], *, approval: bool = False) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "command",
        "argv": argv,
        "mutates": action_id
        not in {"director.next", "director.brief.show", "manifest.review"},
        "requires_user_approval": approval,
    }


def _project_action(root: Path, adapter: str) -> tuple[str, dict[str, Any]]:
    project_store = ProjectStore(root)
    project = project_store.load()
    approval = project_store.manifest_approval_status()
    if approval["approved"]:
        return (
            "local_render_ready",
            _command(
                "render.local",
                [
                    "bash",
                    str(
                        Path(__file__).resolve().parents[3]
                        / "templates"
                        / "shared"
                        / "build_manifest_video.sh"
                    ),
                    str(root),
                ],
            ),
        )
    if project.payload["production_manifest_digest"] is not None:
        return (
            "script_approval_required",
            _command(
                "manifest.review",
                ["lecturecast", "manifest", "review", str(root), "--json"],
                approval=True,
            ),
        )

    director_store = DirectorStateStore(root)
    try:
        director = director_store.load()
    except LectureCastError as error:
        if error.code != "session_not_found":
            raise
        return (
            "source_summary_required",
            {
                "id": "source.prepare",
                "kind": "prepare_bounded_source_summary",
                "then_argv": [
                    "lecturecast",
                    "director",
                    "start",
                    str(root),
                    "--source",
                    "<source-summary.json>",
                    "--adapter",
                    adapter,
                    "--json",
                ],
                "mutates": True,
                "requires_user_approval": False,
            },
        )

    if director.payload["adapter_kind"] != adapter:
        return (
            "director_rebind_required",
            _command(
                "director.resume",
                [
                    "lecturecast",
                    "director",
                    "resume",
                    str(root),
                    "--adapter",
                    adapter,
                    "--host-contract",
                    HOST_WORKFLOW_CONTRACT_VERSION,
                    "--json",
                ],
            ),
        )

    generation_status = director.payload["generation_status"]
    if director.generation_id is not None:
        if generation_status == "ready":
            return (
                "manifest_recovery_required",
                _command(
                    "director.status",
                    ["lecturecast", "director", "status", str(root), "--json"],
                ),
            )
        return (
            "generation_in_progress",
            _command(
                "director.status",
                ["lecturecast", "director", "status", str(root), "--json"],
            ),
        )
    session_status = director.payload["session_status"]
    if session_status == "collecting_decisions":
        return (
            "decision_required",
            _command(
                "director.next",
                ["lecturecast", "director", "next", str(root), "--json"],
                approval=True,
            ),
        )
    if session_status == "ready_to_confirm":
        return (
            "brief_approval_required",
            _command(
                "director.brief.show",
                ["lecturecast", "director", "brief", "show", str(root), "--json"],
            ),
        )
    if session_status == "confirmed":
        return (
            "credit_approval_required",
            _command(
                "director.generate",
                ["lecturecast", "director", "generate", str(root), "--json"],
                approval=True,
            ),
        )
    return (
        "stopped",
        {
            "id": "workflow.stop",
            "kind": "stop",
            "mutates": False,
            "requires_user_approval": True,
        },
    )


@app.command("adapters")
def adapters(json_output: bool = typer.Option(False, "--json")) -> None:
    """Inspect installer-owned native Skills without claiming a session loaded them."""
    payload = {
        "contract_version": HOST_WORKFLOW_CONTRACT_VERSION,
        "installation_check_only": True,
        "adapters": {
            adapter: host_adapter_status(adapter, HOST_WORKFLOW_CONTRACT_VERSION)
            for adapter in sorted(NATIVE_HOST_ADAPTERS)
        },
    }
    emit(payload, json_output=json_output, message="LectureCast 宿主适配器安装状态已读取。")


@app.command("status")
def status(
    project_root: Path | None = typer.Argument(None),
    adapter: str = typer.Option(..., "--adapter"),
    host_contract: str = typer.Option(..., "--host-contract"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Return the one safe next action for the current host-agent workflow."""
    root = project_root.expanduser().resolve() if project_root is not None else None
    onboarding = onboarding_status(
        root,
        adapter=adapter,
        host_contract=host_contract,
    )
    if not onboarding["ok"]:
        emit(
            {
                **onboarding,
                "ok": False,
            },
            json_output=json_output,
            message=str(onboarding["user_prompt"]),
        )
        return
    if root is None or not (root / ".lecturecast" / "project.json").is_file():
        phase = "project_required"
        next_action = onboarding["workflow"]["next_action"]
        project = None
        receipt = None
    else:
        receipt = HostWorkflowStore(root).require_current(expected_adapter=adapter)
        phase, next_action = _project_action(root, adapter)
        project = ProjectStore(root).load().to_dict()
    emit(
        {
            "ok": True,
            "host_agent": onboarding["host_agent"],
            "host_workflow": receipt,
            "project": project,
            "workflow": {
                "ready": True,
                "phase": phase,
                "policy": "execute_only_returned_next_action",
                "next_action": next_action,
            },
        },
        json_output=json_output,
        message=f"LectureCast 工作流阶段：{phase}。",
    )
