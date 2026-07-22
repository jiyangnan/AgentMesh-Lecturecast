"""LectureCast commercial CLI entrypoint.

AgentMesh360 owns identity, paid access, and credits. Creative direction is cloud
assisted while media, voice, rendering, editing, and durable project state stay local.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from .commands.auth import app as auth_app
from .commands.agent import app as agent_app
from .commands.director import app as director_app
from .commands.doctor import doctor
from .commands.manifest import app as manifest_app
from .commands.onboard import onboard
from .commands.outcome import app as outcome_app
from .commands.project import app as project_app

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()
app.add_typer(auth_app, name="auth", help="Bind the required AgentMesh360 commercial account.")
app.add_typer(agent_app, name="agent", help="Drive the current native host through one safe next action.")
app.add_typer(director_app, name="director", help="Use the cloud creative Director.")
app.add_typer(project_app, name="project", help="Create and resume durable local projects.")
app.add_typer(manifest_app, name="manifest", help="Inspect and verify signed manifests.")
app.add_typer(
    outcome_app,
    name="outcome",
    help="Create explicit local outcome evidence without tracking or upload.",
)
app.command()(doctor)
app.command()(onboard)


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
    """Show the commercial agent workflow and its local production runbook."""
    root = _repo_root()
    agents = root / "AGENTS.md"
    local = root / "docs" / "LOCAL-WORKFLOW.md"
    body = (
        "LectureCast requires a paid AgentMesh360 account and universal API Key. "
        "The cloud Director provides the signed creative plan; original media, voice, "
        "rendering and exports stay on this machine.\n\n"
        "Start every agent run with the exact host-specific onboard command from the "
        "installed Skill. Do not create a project until workflow.ready is true. After "
        "each step, execute only the machine-returned workflow.next_action for:\n"
        "  script -> Edge/MiniMax TTS -> Remotion render -> ffmpeg subtitle burn -> covers\n\n"
        f"Agent runbook:   [bold]{agents}[/bold]\n"
        f"Full pipeline:   [bold]{local}[/bold]\n\n"
        "[dim]Tell your agent:  做一条关于 RAG 工作原理的 5 分钟课程视频[/dim]"
    )
    console.print(Panel(body, title="lecturecast — commercial workflow", border_style="magenta"))
