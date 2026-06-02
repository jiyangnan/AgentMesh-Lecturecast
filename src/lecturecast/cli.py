"""lecturecast CLI entrypoint — typer-based."""
import json
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from . import config
from .api import LectureCastAPI

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()


@app.command()
def init(
    key: str = typer.Option(..., "--key", help="License key (lc_live_xxx)"),
    api: str = typer.Option(config.DEFAULT_API, "--api", help="Override API endpoint"),
) -> None:
    """Configure ~/.lecturecast/config.toml with your license key."""
    if not key.startswith(("lc_live_", "lc_team_", "lc_test_")):
        console.print("[red]✗[/red] key must start with lc_live_, lc_team_, or lc_test_")
        raise typer.Exit(1)
    config.save({"token": key, "api_base": api})
    console.print(f"[green]✓[/green] saved to [dim]{config.config_path()}[/dim]")
    console.print("Try: [bold]lecturecast status[/bold]")


@app.command()
def status() -> None:
    """Check API + your subscription health."""
    try:
        cli = LectureCastAPI()
    except RuntimeError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1)
    h = cli.health()
    console.print(Panel(json.dumps(h, indent=2), title="lecturecast api", border_style="green"
                        if h.get("status") == 200 else "red"))


@app.command()
def new(
    topic: str = typer.Argument(..., help="Course topic, e.g. \"RAG 工作原理\""),
    depth: str = typer.Option("concept", "--depth", help="concept | deep | hands_on"),
    platforms: str = typer.Option("bilibili,xiaohongshu", "--platforms",
                                  help="comma-separated"),
    voice: str = typer.Option("zh-CN-YunxiNeural", "--voice"),
    script: Path | None = typer.Option(None, "--script",
                                       help="Skip draft. Use this JSON as the script."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-approve draft"),
    out: Path = typer.Option(Path.home() / "lecturecast", "--out",
                             help="Output directory for downloaded files"),
) -> None:
    """Start a new course end-to-end."""
    cli = LectureCastAPI()
    user_script = script.read_text() if script else None

    with console.status("[cyan]submitting[/cyan]…"):
        job = cli.new_course(
            topic=topic, depth=depth,
            platforms=[p.strip() for p in platforms.split(",") if p.strip()],
            voice=voice, user_script=user_script,
        )
    jid = job["job_id"]
    console.print(f"  job_id = [bold]{jid}[/bold]")

    _drive_to_completion(cli, jid, topic=topic, auto_approve=yes, out_root=out)


def _drive_to_completion(cli: LectureCastAPI, jid: str, *, topic: str,
                         auto_approve: bool, out_root: Path) -> None:
    """Long-poll the job, handle approval gate, download final files."""
    last_pct = -1
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  console=console, transient=False) as prog:
        task = prog.add_task("waiting…", total=None)
        while True:
            job = cli.get_course(jid)
            status = job["status"]
            pr = job.get("progress") or {}
            pct = pr.get("pct", 0)
            stage = pr.get("stage", status)
            if pct != last_pct:
                prog.update(task, description=f"[cyan]{stage}[/cyan] [dim]({pct}%)[/dim]")
                last_pct = pct

            if status == "awaiting_approval":
                prog.stop()
                _handle_approval_gate(cli, jid, job.get("draft_script") or {},
                                      auto_approve=auto_approve)
                prog.start()
                task = prog.add_task("rendering…", total=None)
                last_pct = -1
                continue

            if status == "complete":
                prog.update(task, description="[green]complete[/green] (100%)")
                break
            if status in ("failed", "refunded"):
                console.print(f"[red]✗[/red] {status}: {job.get('error') or '(no detail)'}")
                raise typer.Exit(1)

            time.sleep(2.0)

    outs = job.get("outputs") or {}
    if not outs:
        console.print("[yellow]⚠[/yellow] job complete but no outputs reported")
        return
    _download_all(outs, out_root / topic.replace("/", "_"))


def _handle_approval_gate(cli: LectureCastAPI, jid: str, draft: dict, *,
                          auto_approve: bool) -> None:
    """Show the draft script and Y/E/N."""
    sections = draft.get("sections", [])
    table = Table(show_header=True, header_style="bold")
    table.add_column("§")
    table.add_column("Duration", justify="right")
    table.add_column("Section")
    for i, s in enumerate(sections, 1):
        dur = s.get("duration") or 0
        table.add_row(str(i), f"{dur}s", s.get("title", s.get("id", "?")))
    console.print(Panel(table, title=f"Draft · {draft.get('title', '')}",
                        subtitle=f"total {draft.get('total_seconds', 0)}s"))

    if auto_approve:
        console.print("[dim]--yes set, auto-approving[/dim]")
        cli.approve(jid, approved=True)
        return

    choice = Prompt.ask("[Y]es / [E]dit / [N]o", choices=["Y", "y", "E", "e", "N", "n"],
                        default="Y", show_choices=False)
    if choice.lower() == "y":
        cli.approve(jid, approved=True)
    elif choice.lower() == "n":
        cli.approve(jid, approved=False)
        console.print("[yellow]aborted; credits refunded[/yellow]")
        raise typer.Exit(0)
    else:
        console.print("[dim]launching $EDITOR with the draft … (M2)[/dim]")
        edits = Prompt.ask("Inline edits (one line)")
        cli.approve(jid, approved=True, edits=edits)


def _download_all(outputs: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as cli, \
         Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
        for key, url in outputs.items():
            fn = out_dir / Path(url).name
            t = prog.add_task(f"↓ {fn.name}")
            r = cli.get(url)
            r.raise_for_status()
            fn.write_bytes(r.content)
            prog.update(t, description=f"[green]✓[/green] {fn.name} "
                        f"[dim]({len(r.content)//1024} KB)[/dim]")
    console.print(f"\n[green]✓[/green] {len(outputs)} files in [bold]{out_dir}[/bold]")


@app.command("list")
def list_cmd(limit: int = typer.Option(20, "--limit")) -> None:
    """Recent jobs."""
    cli = LectureCastAPI()
    jobs = cli.list_courses(limit=limit)
    if not jobs:
        console.print("(no jobs yet)")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("job_id"); table.add_column("status"); table.add_column("stage")
    for j in jobs:
        pr = j.get("progress") or {}
        table.add_row(j["job_id"], j["status"], pr.get("stage", ""))
    console.print(table)


@app.command()
def get(
    job_id: str = typer.Argument(...),
    out: Path = typer.Option(Path.home() / "lecturecast", "--out"),
) -> None:
    """Re-download a previous job's outputs."""
    cli = LectureCastAPI()
    job = cli.get_course(job_id)
    if job["status"] != "complete":
        console.print(f"[yellow]job is {job['status']} — nothing to download yet[/yellow]")
        return
    _download_all(job.get("outputs") or {}, out / job_id)


@app.command()
def usage() -> None:
    """Show current credit balance and tier."""
    cli = LectureCastAPI()
    # M1: balance lives on core, we route via lecturecast-server's /v1/usage in M2.
    # For now, point users at the dashboard.
    console.print(Panel(
        "Visit [link=https://agentmesh360.com/account]agentmesh360.com/account[/link] "
        "to see your shared AgentMesh credit balance.\n\n"
        "[dim]CLI-side usage view ships in M2.[/dim]",
        title="usage",
    ))
