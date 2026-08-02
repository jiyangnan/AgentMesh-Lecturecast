from __future__ import annotations

from pathlib import Path

import typer

from ..capabilities import (
    capture_capabilities,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
)
from ..commercial import require_commercial_access
from ..config import resolve_protocol_version
from ..errors import LectureCastError
from ..host_agent import (
    HOST_ADAPTER_VERSION,
    HOST_WORKFLOW_CONTRACT_VERSION,
    HostWorkflowStore,
    require_host_adapter,
    require_project_host_workflow,
)
from ..project import ProjectStore
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("init")
def init_project(
    directory: Path = typer.Argument(Path(".")),
    name: str = typer.Option("Untitled LectureCast Project", "--name"),
    adapter: str = typer.Option(..., "--adapter"),
    host_contract: str = typer.Option(..., "--host-contract"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a shareable local project index."""
    try:
        require_host_adapter(adapter, host_contract)
        require_commercial_access()
        state = ProjectStore(directory).init(name=name)
        receipt = HostWorkflowStore(directory).bind(
            adapter=adapter,
            contract_version=host_contract,
        )
        emit(
            {
                "project": state.to_dict(),
                "host_workflow": receipt,
                "workflow": {
                    "phase": "source_summary_required",
                    "next_action": {
                        "id": "source.prepare",
                        "kind": "prepare_bounded_source_summary",
                        "mutates": True,
                        "requires_user_approval": False,
                        "then_argv": [
                            "lecturecast",
                            "director",
                            "start",
                            str(directory.expanduser().resolve()),
                            "--source",
                            "<source-summary.json>",
                            "--adapter",
                            adapter,
                            "--adapter-version",
                            HOST_ADAPTER_VERSION,
                            "--json",
                        ],
                    },
                },
            },
            json_output=json_output,
            message=f"项目已建立：{state.payload['name']}（revision {state.revision}）。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


def _show_project(
    directory: Path, json_output: bool, *, require_access: bool = False
) -> None:
    try:
        if require_access:
            require_commercial_access()
        state = ProjectStore(directory).load()
        emit(
            state.to_dict(),
            json_output=json_output,
            message=(
                f"项目：{state.payload['name']}；状态：{state.payload['status']}；"
                f"revision {state.revision}。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read the current durable project state."""
    _show_project(directory, json_output)


@app.command("resume")
def resume(
    directory: Path = typer.Argument(Path(".")),
    adapter: str = typer.Option(..., "--adapter"),
    host_contract: str = typer.Option(..., "--host-contract"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Resume from disk; conversation history is not used as project state."""
    try:
        require_host_adapter(adapter, host_contract)
        require_commercial_access()
        state = ProjectStore(directory).load()
        receipt = HostWorkflowStore(directory).bind(
            adapter=adapter,
            contract_version=host_contract,
        )
        emit(
            {
                "project": state.to_dict(),
                "host_workflow": receipt,
                "workflow": {
                    "phase": "project_resumed",
                    "next_action": {
                        "id": "agent.status",
                        "kind": "command",
                        "argv": [
                            "lecturecast",
                            "agent",
                            "status",
                            str(directory.expanduser().resolve()),
                            "--adapter",
                            adapter,
                            "--host-contract",
                            HOST_WORKFLOW_CONTRACT_VERSION,
                            "--json",
                        ],
                        "mutates": False,
                        "requires_user_approval": False,
                    },
                },
            },
            json_output=json_output,
            message=(
                f"项目已由当前宿主 Agent 接管：{state.payload['name']}；"
                f"revision {state.revision}。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("capabilities")
def capabilities(
    directory: Path = typer.Argument(Path(".")),
    adapter: str | None = typer.Option(None, "--adapter"),
    adapter_version: str | None = typer.Option(None, "--adapter-version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and persist the exact capability snapshot later bound to a Manifest."""
    try:
        receipt = require_project_host_workflow(directory)
        require_commercial_access()
        store = ProjectStore(directory)
        state = store.load()
        receipt_adapter = receipt["adapter"]
        if adapter is not None and adapter != receipt_adapter["kind"]:
            raise LectureCastError(
                code="client_upgrade_required",
                message="命令提供的宿主 Adapter 与项目工作流收据不一致。",
                next_action="在当前宿主的新任务中重新运行 project resume。",
            )
        if adapter_version is not None and adapter_version != receipt_adapter["version"]:
            raise LectureCastError(
                code="client_upgrade_required",
                message="命令提供的 Adapter version 与当前 Skill 合同不一致。",
                next_action="不要手工指定版本；在新任务中按当前 Skill 继续。",
            )
        protocol_version = resolve_protocol_version()
        if protocol_version == "1.1":
            # §5.5e5c: v1.1 capture with real HeyGen probes (mirror director.py
            # generate path) so the persisted client-capabilities.json carries
            # third_party_processors when the shipped stack is importable + the
            # journal is not in a refuse-downgrade state.
            document = capture_capabilities_v1_1(
                adapter_kind=str(receipt_adapter["kind"]),
                adapter_version=str(receipt_adapter["version"]),
                project_root=directory,
                repo_root=Path(__file__).resolve().parents[3],
                adapter_probe=default_heygen_adapter_probe,
                journal_probe=lambda: default_heygen_journal_probe(directory),
            )
        else:
            document = capture_capabilities(
                adapter_kind=str(receipt_adapter["kind"]),
                adapter_version=str(receipt_adapter["version"]),
                project_root=directory,
                repo_root=Path(__file__).resolve().parents[3],
            )
        updated = store.save_capabilities(document, expected_revision=state.revision)
        emit(
            {
                "project": updated.to_dict(),
                "capabilities": document.model_dump(),
                "host_workflow": receipt,
                "workflow": {
                    "phase": "capabilities_saved",
                    "policy": "execute_only_returned_next_action",
                    "next_action": {
                        "id": "agent.status",
                        "kind": "command",
                        "argv": [
                            "lecturecast",
                            "agent",
                            "status",
                            str(directory.expanduser().resolve()),
                            "--adapter",
                            str(receipt_adapter["kind"]),
                            "--host-contract",
                            HOST_WORKFLOW_CONTRACT_VERSION,
                            "--json",
                        ],
                        "mutates": False,
                        "requires_user_approval": False,
                    },
                },
            },
            json_output=json_output,
            message=f"ClientCapabilities 已保存（revision {updated.revision}）。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        fail(
            LectureCastError(
                code="manifest_incompatible",
                message="无法采集本机能力。",
                next_action="运行 lecturecast doctor 检查 Node、Remotion 与 ffmpeg。",
                cause=type(exc).__name__,
            ),
            json_output=json_output,
        )
