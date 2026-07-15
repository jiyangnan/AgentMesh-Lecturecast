from __future__ import annotations

import json
from typing import Any, NoReturn

import typer
from rich.console import Console

from ..errors import LectureCastError


console = Console()
error_console = Console(stderr=True)


def emit(payload: dict[str, Any], *, json_output: bool, message: str) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        console.print(message)


def fail(error: LectureCastError, *, json_output: bool) -> NoReturn:
    if json_output:
        typer.echo(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), err=True)
    else:
        error_console.print(f"[red]{error.message}[/red]\n{error.next_action}")
    raise typer.Exit(code=1)

