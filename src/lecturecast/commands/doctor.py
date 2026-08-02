from __future__ import annotations

import os
from pathlib import Path

import typer

from ..capabilities import (
    build_heygen_doctor_section,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
    doctor_report,
)
from .output import emit


def doctor(
    project_root: Path | None = typer.Option(
        None,
        "--project-root",
        help="LectureCast 项目根（含 remotion/node_modules 与 .lecturecast/）。",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """检查本地渲染 + 数字人能力（只读，不上传任何媒体或密钥）。"""
    repo_root = Path(__file__).resolve().parents[3]
    project_root = project_root or Path.cwd()
    # §5.5e5d D1: fresh capture + LIVE probes — never read the stored
    # client-capabilities.json cache (a stale snapshot could report a HeyGen
    # state that no longer holds: key rotated, adapter uninstalled, journal
    # migrated). The probes are read-only (mode=ro URI; no migration write).
    capabilities = capture_capabilities_v1_1(
        project_root=project_root,
        repo_root=repo_root,
        env=os.environ,
        adapter_probe=default_heygen_adapter_probe,
        journal_probe=lambda: default_heygen_journal_probe(project_root),
    )
    heygen_section = build_heygen_doctor_section(
        env=os.environ,
        project_root=project_root,
        adapter_probe=default_heygen_adapter_probe,
    )
    report = doctor_report(capabilities, heygen_section=heygen_section)

    missing = ", ".join(report["missing"]) or "无"
    actions = "\n".join(f"- {action}" for action in report["next_actions"])
    message = f"本地渲染就绪：{'是' if report['ready'] else '否'}；缺失：{missing}。"
    if actions:
        message = f"{message}\n下一步：\n{actions}"

    # HeyGen 数字人能力（v1.1 additive；M1 基础渲染不依赖它）。
    third_party = report.get("third_party")
    if isinstance(third_party, dict):
        if third_party.get("configured"):
            ops = third_party.get("operations") or []
            message += f"\nHeyGen 数字人：已就绪（{len(ops)} 项操作可服务）。"
        else:
            blockers = third_party.get("blockers") or []
            warnings = third_party.get("warnings") or []
            parts = list(blockers) + [f"WARN:{w}" for w in warnings]
            detail = ", ".join(parts) or "未知"
            message += f"\nHeyGen 数字人：未就绪（{detail}）。"

    emit(
        report,
        json_output=json_output,
        message=message,
    )
