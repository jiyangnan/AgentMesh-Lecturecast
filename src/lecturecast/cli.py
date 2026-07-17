"""LectureCast CLI entrypoint.

Community creation remains fully local. Director adds an optional cloud decision
layer while media, voice, rendering, editing, and durable project state stay local.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from .commands.auth import app as auth_app
from .commands.director import app as director_app
from .commands.dogfood import app as dogfood_app
from .commands.doctor import doctor
from .commands.manifest import app as manifest_app
from .commands.project import app as project_app

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()
app.add_typer(auth_app, name="auth", help="Manage the optional Director API credential.")
app.add_typer(director_app, name="director", help="Use the optional cloud creative Director.")
app.add_typer(
    dogfood_app,
    name="dogfood",
    help="Collect local three-host release evidence without uploading media.",
)
app.add_typer(project_app, name="project", help="Create and resume durable local projects.")
app.add_typer(manifest_app, name="manifest", help="Inspect and verify signed manifests.")
app.command()(doctor)


def _repo_root() -> Path:
    # src/lecturecast/cli.py -> repo root is three levels up
    return Path(__file__).resolve().parents[2]


@app.command()
def version() -> None:
    """Print the installed lecturecast version."""
    try:
        v = _pkg_version("lecturecast")
    except PackageNotFoundError:
        v = "unknown (running from source)"
    console.print(f"lecturecast [bold]{v}[/bold]")


@app.command()
def workflow() -> None:
    """Show where the local workflow lives and how to drive it from an AI agent."""
    root = _repo_root()
    agents = root / "AGENTS.md"
    local = root / "docs" / "LOCAL-WORKFLOW.md"
    body = (
        "LectureCast Community is a [bold]fully local[/bold] video workflow — no cloud, no "
        "account, no API key. Director is optional and only sends structured creative "
        "inputs for cloud decisions.\n\n"
        "The pipeline is driven by an AI agent (Claude Code / OpenClaw / Cursor / "
        "Codex) using the bundled [bold]templates/[/bold]:\n"
        "  script -> Edge/MiniMax TTS -> Remotion render -> ffmpeg subtitle burn -> covers\n\n"
        f"Agent runbook:   [bold]{agents}[/bold]\n"
        f"Full pipeline:   [bold]{local}[/bold]\n\n"
        "[dim]Tell your agent:  做一条关于 RAG 工作原理的 5 分钟课程视频[/dim]"
    )
    console.print(Panel(body, title="lecturecast — local workflow", border_style="magenta"))
