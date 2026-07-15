from __future__ import annotations

from pathlib import Path

import typer

from ..capabilities import capture_capabilities
from ..errors import LectureCastError
from ..project import ProjectStore
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("init")
def init_project(
    directory: Path = typer.Argument(Path(".")),
    name: str = typer.Option("Untitled LectureCast Project", "--name"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a shareable local project index."""
    try:
        state = ProjectStore(directory).init(name=name)
        emit(
            state.to_dict(),
            json_output=json_output,
            message=f"项目已建立：{state.payload['name']}（revision {state.revision}）。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


def _show_project(directory: Path, json_output: bool) -> None:
    try:
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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Resume from disk; conversation history is not used as project state."""
    _show_project(directory, json_output)


@app.command("capabilities")
def capabilities(
    directory: Path = typer.Argument(Path(".")),
    adapter: str = typer.Option("text", "--adapter"),
    adapter_version: str = typer.Option("1.0.0", "--adapter-version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Capture and persist the exact capability snapshot later bound to a Manifest."""
    try:
        store = ProjectStore(directory)
        state = store.load()
        document = capture_capabilities(
            adapter_kind=adapter,
            adapter_version=adapter_version,
            repo_root=Path(__file__).resolve().parents[3],
        )
        updated = store.save_capabilities(document, expected_revision=state.revision)
        emit(
            {
                "project": updated.to_dict(),
                "capabilities": document.model_dump(),
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
