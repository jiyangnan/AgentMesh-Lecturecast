from __future__ import annotations

from pathlib import Path

import typer

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

