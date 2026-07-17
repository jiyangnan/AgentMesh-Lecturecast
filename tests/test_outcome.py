from __future__ import annotations

import json
import os
import stat
import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.errors import LectureCastError
from lecturecast.outcome import (
    ANONYMOUS_OUTCOME_REPORT_SCHEMA,
    LOCAL_OUTCOME_RECEIPT_SCHEMA,
    OUTCOME_AGGREGATE_SCHEMA,
    SHARE_CONSENT,
    OutcomeStore,
    aggregate_outcome_reports,
    build_anonymous_report,
    read_anonymous_report,
    write_anonymous_report,
    write_outcome_aggregate,
)
from lecturecast.project import ProjectStore


FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _ready_project(root: Path) -> OutcomeStore:
    project = ProjectStore(root)
    state = project.init(name="Private local outcome", project_id="project_private_123")
    project.save_manifest(
        _fixture("production-manifest-v1.json"),
        expected_revision=state.revision,
    )
    return OutcomeStore(root)


def test_private_receipt_binds_verified_manifest_and_stays_mode_0600(
    tmp_path: Path,
) -> None:
    store = _ready_project(tmp_path)

    receipt = store.record(
        render_status="completed",
        adoption_status="exported",
    )

    assert receipt["schema_version"] == LOCAL_OUTCOME_RECEIPT_SCHEMA
    assert receipt["shareable"] is False
    assert receipt["project_id"] == "project_private_123"
    assert receipt["manifest_digest"].startswith("sha256:")
    assert receipt["manifest_key_id"] == "fixture_key_v1"
    assert receipt["receipt_revision"] == 1
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert str(tmp_path) not in store.path.read_text(encoding="utf-8")


def test_outcome_record_requires_a_verified_manifest(tmp_path: Path) -> None:
    ProjectStore(tmp_path).init(name="Not generated yet")

    with pytest.raises(LectureCastError, match="ProductionManifest"):
        OutcomeStore(tmp_path).record(
            render_status="not_attempted",
            adoption_status="undecided",
        )


@pytest.mark.parametrize(
    ("render_status", "adoption_status", "failure_reason"),
    [
        ("completed", "published", "render_failed"),
        ("failed", "undecided", None),
        ("partial", "exported", "quality_rejected"),
        ("not_a_status", "undecided", None),
    ],
)
def test_outcome_choices_fail_closed(
    tmp_path: Path,
    render_status: str,
    adoption_status: str,
    failure_reason: str | None,
) -> None:
    store = _ready_project(tmp_path)

    with pytest.raises(LectureCastError) as captured:
        store.record(
            render_status=render_status,
            adoption_status=adoption_status,
            failure_reason=failure_reason,
        )

    assert captured.value.code == "outcome_evidence_invalid"


def test_receipt_updates_are_revision_safe_and_idempotent(tmp_path: Path) -> None:
    store = _ready_project(tmp_path)
    original = store.record(render_status="completed", adoption_status="exported")

    same = store.record(
        render_status="completed",
        adoption_status="exported",
        expected_revision=1,
    )
    assert same == original

    updated = store.record(
        render_status="completed",
        adoption_status="published",
        expected_revision=1,
    )
    assert updated["receipt_revision"] == 2
    assert updated["receipt_id"] == original["receipt_id"]
    assert updated["created_at"] == original["created_at"]

    with pytest.raises(LectureCastError) as captured:
        store.record(
            render_status="failed",
            adoption_status="discarded",
            failure_reason="render_failed",
            expected_revision=1,
        )
    assert captured.value.code == "project_revision_conflict"


def test_anonymous_export_has_an_exact_non_identifying_shape(tmp_path: Path) -> None:
    store = _ready_project(tmp_path)
    receipt = store.record(render_status="completed", adoption_status="published")

    report = build_anonymous_report(receipt, consent=SHARE_CONSENT)
    serialized = json.dumps(report, sort_keys=True)

    assert report["schema_version"] == ANONYMOUS_OUTCOME_REPORT_SCHEMA
    assert report["report_id"] == receipt["receipt_id"]
    assert report["consent"] == "manual_anonymous_export"
    assert report["privacy"]["network_sent"] is False
    assert set(report) == {
        "schema_version",
        "report_id",
        "evidence_level",
        "render_status",
        "adoption_status",
        "failure_reason",
        "client_version",
        "consent",
        "privacy",
    }
    for forbidden in (
        "project_private_123",
        receipt["manifest_digest"],
        "session_id",
        "generation_id",
        "account_id",
        str(tmp_path),
        "AI 求职产品演示",
    ):
        assert forbidden not in serialized

    output = tmp_path / "share" / "anonymous-outcome.json"
    write_anonymous_report(output, report)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert read_anonymous_report(output) == report
    with pytest.raises(LectureCastError, match="已存在"):
        write_anonymous_report(output, report)


def test_export_requires_exact_consent_and_report_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    receipt = _ready_project(tmp_path).record(
        render_status="not_attempted",
        adoption_status="undecided",
    )
    with pytest.raises(LectureCastError, match="明确同意"):
        build_anonymous_report(receipt, consent="yes")

    report = build_anonymous_report(receipt, consent=SHARE_CONSENT)
    report["project_id"] = "leak"
    path = tmp_path / "invalid-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(LectureCastError, match="字段"):
        read_anonymous_report(path)


def test_offline_aggregate_requires_three_unique_reports_and_drops_ids(
    tmp_path: Path,
) -> None:
    reports: list[dict[str, object]] = []
    choices = [
        ("completed", "published", None),
        ("completed", "exported", None),
        ("failed", "discarded", "render_failed"),
    ]
    for index, (render, adoption, reason) in enumerate(choices):
        root = tmp_path / f"project-{index}"
        receipt = _ready_project(root).record(
            render_status=render,
            adoption_status=adoption,
            failure_reason=reason,
        )
        reports.append(build_anonymous_report(receipt, consent=SHARE_CONSENT))

    with pytest.raises(LectureCastError, match="至少三份"):
        aggregate_outcome_reports(reports[:2])
    with pytest.raises(LectureCastError, match="重复"):
        aggregate_outcome_reports([reports[0], reports[1], reports[0]])

    aggregate = aggregate_outcome_reports(reports)
    serialized = json.dumps(aggregate, sort_keys=True)
    assert aggregate["schema_version"] == OUTCOME_AGGREGATE_SCHEMA
    assert aggregate["ready"] is True
    assert aggregate["report_count"] == 3
    assert aggregate["render_status_counts"]["completed"] == 2
    assert aggregate["adoption_status_counts"]["published"] == 1
    assert aggregate["failure_reason_counts"]["render_failed"] == 1
    assert aggregate["render_completed_rate"] == pytest.approx(2 / 3)
    assert aggregate["adoption_published_rate"] == pytest.approx(1 / 3)
    assert all(report["report_id"] not in serialized for report in reports)

    output = tmp_path / "aggregate.json"
    write_outcome_aggregate(output, aggregate)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_outcome_cli_never_prints_private_binding_or_output_paths(tmp_path: Path) -> None:
    _ready_project(tmp_path)
    recorded = runner.invoke(
        app,
        [
            "outcome",
            "record",
            str(tmp_path),
            "--render-status",
            "completed",
            "--adoption-status",
            "exported",
            "--json",
        ],
    )
    assert recorded.exit_code == 0, recorded.stderr
    recorded_payload = json.loads(recorded.stdout)
    assert recorded_payload["shareable"] is False
    assert "project_id" not in recorded_payload
    assert "manifest_digest" not in recorded_payload
    assert str(tmp_path) not in recorded.stdout

    report_path = tmp_path / "anonymous.json"
    exported = runner.invoke(
        app,
        [
            "outcome",
            "export",
            str(tmp_path),
            "--report-out",
            str(report_path),
            "--consent",
            SHARE_CONSENT,
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.stderr
    exported_payload = json.loads(exported.stdout)
    assert exported_payload["shareable"] is True
    assert exported_payload["network_sent"] is False
    assert str(report_path) not in exported.stdout
    assert "manifest_digest" not in exported.stdout
    assert "manifest_key_id" not in exported.stdout


def test_outcome_cli_rejects_inferred_consent_without_creating_report(
    tmp_path: Path,
) -> None:
    _ready_project(tmp_path).record(
        render_status="completed",
        adoption_status="exported",
    )
    report_path = tmp_path / "must-not-exist.json"

    result = runner.invoke(
        app,
        [
            "outcome",
            "export",
            str(tmp_path),
            "--report-out",
            str(report_path),
            "--consent",
            "yes",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["code"] == "outcome_evidence_invalid"
    assert not report_path.exists()


def test_outcome_cli_verifies_and_aggregates_without_emitting_report_ids(
    tmp_path: Path,
) -> None:
    report_paths: list[Path] = []
    report_ids: list[str] = []
    for index in range(3):
        root = tmp_path / f"cli-project-{index}"
        receipt = _ready_project(root).record(
            render_status="completed",
            adoption_status="published" if index == 0 else "exported",
        )
        report = build_anonymous_report(receipt, consent=SHARE_CONSENT)
        report_path = tmp_path / f"cli-report-{index}.json"
        write_anonymous_report(report_path, report)
        report_paths.append(report_path)
        report_ids.append(report["report_id"])

    verified = runner.invoke(app, ["outcome", "verify", str(report_paths[0]), "--json"])
    assert verified.exit_code == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True

    aggregate_path = tmp_path / "cli-aggregate.json"
    aggregated = runner.invoke(
        app,
        [
            "outcome",
            "aggregate",
            *(str(path) for path in report_paths),
            "--evidence-out",
            str(aggregate_path),
            "--json",
        ],
    )
    assert aggregated.exit_code == 0, aggregated.stderr
    payload = json.loads(aggregated.stdout)
    assert payload["report_count"] == 3
    assert payload["privacy"]["contains_individual_report_ids"] is False
    combined = aggregated.stdout + aggregate_path.read_text(encoding="utf-8")
    assert all(report_id not in combined for report_id in report_ids)


def test_outcome_module_has_no_network_client_imports() -> None:
    module_path = Path(__file__).parents[1] / "src" / "lecturecast" / "outcome.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(
        {"httpx", "requests", "urllib", "socket", "openai", "boto3"}
    )
