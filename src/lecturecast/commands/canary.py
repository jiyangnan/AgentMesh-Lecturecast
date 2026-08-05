"""§5.5e5d-b canary CLI leaf — `lecturecast canary`.

Runs the 8-invariant §5 line-489 smoke test in a fresh ISOLATED sandbox dir
(a tempfile the CLI creates and removes — never the user's real project,
constraint b). Prints a Chinese summary by default; ``--json`` emits the full
report. ``--keep`` retains the sandbox dir for debugging.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import typer

from ..canary import run_canary
from .output import emit


def canary(
    keep: bool = typer.Option(
        False, "--keep",
        help="保留 canary 沙箱目录用于调试（默认运行后删除）。",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """在隔离沙箱里跑 §5 line-489 八项不变量烟测（零真实 HeyGen 调用 / 零真实扣费）。"""
    sandbox = Path(tempfile.mkdtemp(prefix="lecturecast-canary-"))
    try:
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        report = run_canary(sandbox, now_iso=now_iso)
    finally:
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)

    payload = asdict(report)
    # asdict turns the tuple of dataclasses into a list of plain dicts — fine for JSON.
    payload["invariants"] = [asdict(inv) for inv in report.invariants]

    passed = report.passed
    lines = [f"§5 line-489 canary：{'全绿' if passed else '失败'}（{report.total_credits_projected}/{report.credit_cap} credits）。"]
    for inv in report.invariants:
        mark = "✓" if inv.passed else "✗"
        lines.append(f"  {mark} {inv.title}")
    if report.deletion_summary.get("resources"):
        ds = report.deletion_summary
        lines.append(
            f"删除恢复：{ds['deleted']}/{ds['resources']} 资源达 deleted 终态"
            f"（驱动 {ds['driven']} 次 stub）。"
        )
    if keep:
        lines.append(f"沙箱目录：{sandbox}")
    message = "\n".join(lines)

    emit(payload, json_output=json_output, message=message)
    if not passed:
        raise typer.Exit(code=1)
