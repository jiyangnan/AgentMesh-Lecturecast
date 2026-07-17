from __future__ import annotations

from pathlib import Path

import typer

from ..dogfood import (
    begin_dogfood,
    build_dogfood_receipt,
    capture_render_evidence,
    evaluate_dogfood_gate,
    load_dogfood_session,
    read_dogfood_receipt,
    write_dogfood_gate,
    write_dogfood_receipt,
)
from ..errors import LectureCastError
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("begin")
def begin(
    directory: Path = typer.Argument(Path(".")),
    run_id: str = typer.Option(..., "--run-id"),
    run_kind: str = typer.Option(..., "--run-kind"),
    adapter: str = typer.Option(..., "--adapter"),
    public_first_attestation: Path = typer.Option(..., "--public-first-attestation"),
    public_wheel: Path = typer.Option(..., "--public-wheel"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start one local release-dogfood evidence session without network use."""
    try:
        session = begin_dogfood(
            directory,
            run_id=run_id,
            run_kind=run_kind,
            expected_adapter=adapter,
            public_first_attestation_path=public_first_attestation,
            public_wheel_path=public_wheel,
        )
        emit(
            {
                "schema_version": session["schema_version"],
                "run_id": session["run_id"],
                "run_kind": session["run_kind"],
                "project_id": session["project_id"],
                "expected_adapter": session["expected_adapter"],
                "client_version": session["client_version"],
                "release": session["release"],
                "event_count": 0,
            },
            json_output=json_output,
            message=f"Dogfood run 已开始：{run_id}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect safe dogfood session metadata without source or credentials."""
    try:
        session = load_dogfood_session(directory)
        emit(
            {
                "schema_version": session["schema_version"],
                "run_id": session["run_id"],
                "run_kind": session["run_kind"],
                "project_id": session["project_id"],
                "expected_adapter": session["expected_adapter"],
                "client_version": session["client_version"],
                "release": session["release"],
                "event_count": len(session["events"]),
                "journal_revision": session["journal_revision"],
            },
            json_output=json_output,
            message=f"Dogfood 已记录 {len(session['events'])} 个事件。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("capture-render")
def capture_render(
    directory: Path = typer.Argument(Path(".")),
    output_directory: Path = typer.Option(..., "--output-dir"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify and hash the actual local two-video/two-cover outputs."""
    try:
        result = capture_render_evidence(directory, output_directory)
        emit(
            result,
            json_output=json_output,
            message="本地双视频与双封面证据已记录。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("finish")
def finish(
    directory: Path = typer.Argument(Path(".")),
    receipt_out: Path = typer.Option(..., "--receipt-out"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate one run and create an immutable non-secret receipt."""
    try:
        receipt = build_dogfood_receipt(directory)
        write_dogfood_receipt(receipt_out, receipt)
        emit(
            {
                "schema_version": receipt["schema_version"],
                "run_id": receipt["run_id"],
                "run_kind": receipt["run_kind"],
                "expected_adapter": receipt["expected_adapter"],
                "generation_id": receipt["generation_id"],
                "manifest_digest": receipt["manifest_digest"],
                "journal_digest": receipt["journal_digest"],
                "output_count": len(receipt["outputs"]),
                "passed": True,
            },
            json_output=json_output,
            message=f"Dogfood receipt 已通过：{receipt['run_id']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("gate")
def gate(
    receipts: list[Path] = typer.Argument(...),
    evidence_out: Path = typer.Option(..., "--evidence-out"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Require the exact three-host, handoff, and text-fallback matrix."""
    try:
        report = evaluate_dogfood_gate([read_dogfood_receipt(path) for path in receipts])
        write_dogfood_gate(evidence_out, report)
        emit(
            report,
            json_output=json_output,
            message=(
                "三宿主 dogfood 发布门已通过。"
                if report["ready"]
                else "三宿主 dogfood 发布门未通过。"
            ),
        )
        if not report["ready"]:
            raise typer.Exit(code=1)
    except LectureCastError as error:
        fail(error, json_output=json_output)
