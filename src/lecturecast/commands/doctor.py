from __future__ import annotations

from pathlib import Path

import typer

from ..capabilities import capture_capabilities, doctor_report
from .output import emit


def doctor(
    project_root: Path | None = typer.Option(
        None,
        "--project-root",
        help="LectureCast project containing remotion/node_modules.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect local rendering capabilities without uploading any media."""
    repo_root = Path(__file__).resolve().parents[3]
    report = doctor_report(
        capture_capabilities(
            project_root=project_root or Path.cwd(),
            repo_root=repo_root,
        )
    )
    missing = ", ".join(report["missing"]) or "无"
    actions = "\n".join(f"- {action}" for action in report["next_actions"])
    message = f"本地渲染就绪：{'是' if report['ready'] else '否'}；缺失：{missing}。"
    if actions:
        message = f"{message}\n下一步：\n{actions}"
    emit(
        report,
        json_output=json_output,
        message=message,
    )
