from __future__ import annotations

from pathlib import Path

import typer

from ..capabilities import capture_capabilities, doctor_report
from .output import emit


def doctor(json_output: bool = typer.Option(False, "--json")) -> None:
    """Inspect local rendering capabilities without uploading any media."""
    repo_root = Path(__file__).resolve().parents[3]
    report = doctor_report(capture_capabilities(repo_root=repo_root))
    missing = ", ".join(report["missing"]) or "无"
    emit(
        report,
        json_output=json_output,
        message=f"本地渲染就绪：{'是' if report['ready'] else '否'}；缺失：{missing}。",
    )

