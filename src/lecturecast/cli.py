"""lecturecast CLI entrypoint — typer-based.

Lecturecast is a fully local, open-source video workflow for AI agents. There is
no cloud service, account, or API: the CLI is a thin local helper that points you
at the agent runbook. The actual pipeline (script -> Edge/MiniMax TTS -> Remotion
render -> ffmpeg subtitle burn -> covers) runs entirely on your machine and is
driven by an AI agent following docs/LOCAL-WORKFLOW.md.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()


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
        "Lecturecast is a [bold]fully local[/bold] video workflow — no cloud, no "
        "account, no API key.\n\n"
        "The pipeline is driven by an AI agent (Claude Code / OpenClaw / Cursor / "
        "Codex) using the bundled [bold]templates/[/bold]:\n"
        "  script -> Edge/MiniMax TTS -> Remotion render -> ffmpeg subtitle burn -> covers\n\n"
        f"Agent runbook:   [bold]{agents}[/bold]\n"
        f"Full pipeline:   [bold]{local}[/bold]\n\n"
        "[dim]Tell your agent:  做一条关于 RAG 工作原理的 5 分钟课程视频[/dim]"
    )
    console.print(Panel(body, title="lecturecast — local workflow", border_style="magenta"))
