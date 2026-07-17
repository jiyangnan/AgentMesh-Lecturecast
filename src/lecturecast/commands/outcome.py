from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..errors import LectureCastError
from ..outcome import (
    ADOPTION_STATUSES,
    FAILURE_REASONS,
    RENDER_STATUSES,
    SHARE_CONSENT,
    OutcomeStore,
    aggregate_outcome_reports,
    build_anonymous_report,
    read_anonymous_report,
    write_anonymous_report,
    write_outcome_aggregate,
)
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


def _safe_receipt_status(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": receipt["schema_version"],
        "receipt_id": receipt["receipt_id"],
        "receipt_revision": receipt["receipt_revision"],
        "render_status": receipt["render_status"],
        "adoption_status": receipt["adoption_status"],
        "failure_reason": receipt["failure_reason"],
        "shareable": False,
        "network_sent": False,
    }


@app.command("record")
def record(
    directory: Path = typer.Argument(Path(".")),
    render_status: str = typer.Option(
        ...,
        "--render-status",
        help=f"One of: {', '.join(RENDER_STATUSES)}.",
    ),
    adoption_status: str = typer.Option(
        ...,
        "--adoption-status",
        help=f"One of: {', '.join(ADOPTION_STATUSES)}.",
    ),
    failure_reason: str | None = typer.Option(
        None,
        "--failure-reason",
        help=f"Required for partial/failed. One of: {', '.join(FAILURE_REASONS)}.",
    ),
    expected_revision: int | None = typer.Option(None, "--expected-revision", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record explicit bounded choices in a private local-only receipt."""
    try:
        receipt = OutcomeStore(directory).record(
            render_status=render_status,
            adoption_status=adoption_status,
            failure_reason=failure_reason,
            expected_revision=expected_revision,
        )
        emit(
            _safe_receipt_status(receipt),
            json_output=json_output,
            message=(
                f"本地 outcome receipt 已记录，revision {receipt['receipt_revision']}；"
                "它不可分享，也未发送网络请求。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect only the safe status of the private local receipt."""
    try:
        receipt = OutcomeStore(directory).load()
        emit(
            _safe_receipt_status(receipt),
            json_output=json_output,
            message=(
                f"本地 outcome receipt revision {receipt['receipt_revision']}；"
                "它不可分享，也未发送网络请求。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("export")
def export(
    directory: Path = typer.Argument(Path(".")),
    report_out: Path = typer.Option(..., "--report-out"),
    consent: str = typer.Option(..., "--consent"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explicitly export a bounded anonymous report without transmitting it."""
    try:
        receipt = OutcomeStore(directory).load()
        report = build_anonymous_report(receipt, consent=consent)
        write_anonymous_report(report_out, report)
        emit(
            {**report, "shareable": True, "network_sent": False},
            json_output=json_output,
            message="匿名 outcome report 已在本机导出；尚未上传或发送。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("verify")
def verify(
    report: Path = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate the exact anonymous report shape offline."""
    try:
        payload = read_anonymous_report(report)
        emit(
            {**payload, "valid": True, "shareable": True, "network_sent": False},
            json_output=json_output,
            message="匿名 outcome report 结构与隐私契约有效。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("aggregate")
def aggregate(
    reports: list[Path] = typer.Argument(...),
    evidence_out: Path = typer.Option(..., "--evidence-out"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Aggregate at least three unique anonymous reports without retaining IDs."""
    try:
        evidence = aggregate_outcome_reports(
            [read_anonymous_report(path) for path in reports]
        )
        write_outcome_aggregate(evidence_out, evidence)
        emit(
            evidence,
            json_output=json_output,
            message=(
                f"已离线聚合 {evidence['report_count']} 份匿名 outcome report；"
                "结果不含 individual IDs，也未发送网络请求。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


__all__ = ["SHARE_CONSENT", "app"]
