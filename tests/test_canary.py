"""§5.5e5d-b canary harness — §5 line-489 eight invariants (D6-D9, D12).

The canary is a deterministic, zero-credit, isolated-sandbox smoke test. These
tests assert each of the 8 invariants holds in the default run, the 30-credit
hard gate (D7), per-resource deletion recovery (D8), M1-independence (D9), and
sandbox isolation (D12).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lecturecast.canary import (
    CANARY_CREDIT_CAP,
    CanaryReport,
    _StubAdapter,
    _StubDeleter,
    run_canary,
)
from lecturecast.cli import app

NOW = "2026-08-02T00:00:00Z"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


def _run(tmp_path: Path, **kw) -> CanaryReport:
    return run_canary(tmp_path, now_iso=NOW, **kw)


# ----- D6: the 8 invariants all hold in the default run -----

def test_canary_default_run_all_eight_invariants_pass(tmp_path: Path) -> None:
    """D6: the default canary run asserts all 8 §5 line-489 invariants and they
    all pass. The 8 keys are exactly the §5 line-489 list, in order."""
    report = _run(tmp_path)
    keys = [inv.key for inv in report.invariants]
    assert keys == [
        "migration_head", "core_3_cost", "digest_chain", "credit_cap_30",
        "ledger_awaiting_deletion_recovery", "estimate_equals_pricing",
        "m1_independence", "rollback_charged",
    ]
    assert report.passed is True
    assert all(inv.passed for inv in report.invariants)


def test_canary_migration_head_current(tmp_path: Path) -> None:
    """D6 #1: init_database brings the sandbox journal to head==6 (current)."""
    report = _run(tmp_path)
    inv = report.invariant("migration_head")
    assert inv.passed
    assert "current" in inv.detail


def test_canary_core_3_cost_has_three_milestones(tmp_path: Path) -> None:
    """D6 #2: the validated estimate carries the 3 core milestones with per-item
    costs summing to minimum_total."""
    report = _run(tmp_path)
    inv = report.invariant("core_3_cost")
    assert inv.passed
    assert "manifest" in inv.detail and "presenter_plan" in inv.detail and "orchestration" in inv.detail


def test_canary_digest_chain_verifies(tmp_path: Path) -> None:
    """D6 #3: the final estimate's brief_digest + estimate_digest both verify."""
    report = _run(tmp_path)
    assert report.invariant("digest_chain").passed


def test_canary_credit_cap_at_exactly_30_passes(tmp_path: Path) -> None:
    """D6 #4 / boundary: projected cost exactly == cap (30) passes (≤, not <)."""
    report = _run(tmp_path, per_milestone_cost=10)  # 3 × 10 = 30
    inv = report.invariant("credit_cap_30")
    assert inv.passed
    assert report.total_credits_projected == 30 == CANARY_CREDIT_CAP


def test_canary_deletion_recovery_full_sweep(tmp_path: Path) -> None:
    """D6 #5: deletion recovery drives every resource to 'deleted'."""
    report = _run(tmp_path)
    inv = report.invariant("ledger_awaiting_deletion_recovery")
    assert inv.passed
    assert report.deletion_summary == {"driven": 3, "deleted": 3, "resources": 3}


def test_canary_estimate_equals_pricing(tmp_path: Path) -> None:
    """D6 #6: the displayed estimate == the server pricing_estimate (validated)."""
    report = _run(tmp_path)
    assert report.invariant("estimate_equals_pricing").passed


def test_canary_m1_independence_no_key(tmp_path: Path) -> None:
    """D6 #7 / D9: env without HEYGEN_API_KEY → third_party_processors omitted,
    M1 runtime fields stay populated."""
    report = _run(tmp_path, env={})
    inv = report.invariant("m1_independence")
    assert inv.passed
    assert "omitted" in inv.detail


def test_canary_rollback_contract(tmp_path: Path) -> None:
    """D6 #8: the charge contract is per_milestone_success (refund granularity)."""
    report = _run(tmp_path)
    inv = report.invariant("rollback_charged")
    assert inv.passed
    assert "per_milestone_success" in inv.detail


# ----- D7: 30-credit cap hard gate -----

def test_canary_credit_cap_hard_gate_refuses_deletion(tmp_path: Path) -> None:
    """D7 / D-T7: a projected cost over the cap (3 × 11 = 33 > 30) FAILS the
    cap invariant AND the deletion drive is REFUSED — the canary spends no
    effort (even mock) beyond the cap, modeling the real-credit guard."""
    report = _run(tmp_path, per_milestone_cost=11)
    cap_inv = report.invariant("credit_cap_30")
    assert cap_inv.passed is False
    assert "REFUSED" in cap_inv.detail
    # deletion recovery is skipped — nothing was driven.
    del_inv = report.invariant("ledger_awaiting_deletion_recovery")
    assert del_inv.passed is False
    assert "skipped" in del_inv.detail or "refused" in del_inv.detail.lower()
    assert report.deletion_summary["driven"] == 0
    assert report.deletion_summary["resources"] == 0
    assert report.passed is False


def test_canary_custom_credit_cap_applies(tmp_path: Path) -> None:
    """The cap is parameterizable; a lower cap catches a 30-cost run."""
    report = _run(tmp_path, credit_cap=20)  # 30 > 20 → exceeds
    assert report.invariant("credit_cap_30").passed is False
    assert report.passed is False


# ----- D8: per-resource deletion recovery -----

def test_canary_deletion_recovery_per_resource_deleted(tmp_path: Path) -> None:
    """D8 / D-T8: after the canary drives DeletionCoordinator, every one of the
    seeded verified-op resources reaches deletion_status='deleted' in the
    sandbox DB (video via pass 1; audio + portrait via pass 2)."""
    report = _run(tmp_path)
    assert report.passed
    conn = sqlite3.connect(str(tmp_path / DB_REL))
    try:
        rows = conn.execute(
            "SELECT remote_id, deletion_status FROM heygen_remote_resources "
            "WHERE remote_id IN ('v1', 'a1', 'p1')"
        ).fetchall()
        statuses = {r[0]: r[1] for r in rows}
        assert statuses == {"v1": "deleted", "a1": "deleted", "p1": "deleted"}
    finally:
        conn.close()


def test_canary_deletion_uses_real_locked_coordinator_entry(tmp_path: Path) -> None:
    """The canary drives the LOCKED DeletionCoordinator (no bypass); the stub
    deleter/adapter record the exact §3.5 routing — video→deleter, assets→adapter."""
    deleter, adapter = _StubDeleter(), _StubAdapter()
    report = run_canary(
        tmp_path, now_iso=NOW, deleter=deleter, adapter=adapter,
    )
    assert report.passed
    # §3.5 routing: video (v1) → deleter.delete_video; audio + portrait → adapter.delete_asset
    assert deleter.calls == ["v1"]
    assert sorted(adapter.calls) == ["a1", "p1"]


def test_canary_deletion_force_is_literal_bool(tmp_path: Path) -> None:
    """Constraint (a)/(c): the canary forwards force=False as a literal bool to
    the locked coordinator (no new truthy force source; the coordinator's own
    `type(force) is bool` guard is the authority and does not raise)."""
    # A passing run already proves force=False (literal) was accepted — the
    # coordinator raises ValueError on a truthy non-bool. Belt-and-suspenders:
    # the report passed, so the guard never tripped.
    report = _run(tmp_path)
    assert report.passed


# ----- D9: M1-independence -----

def test_canary_m1_independence_asserts_omission(tmp_path: Path) -> None:
    """D9 / D-T9: with env={} the captured v1.1 payload omits third_party_processors
    (the locked fail-closed omit) even though the adapter + the canary's own
    head-current journal ARE ready — so the M1 base-video path is unaffected."""
    report = _run(tmp_path, env={})
    assert report.invariant("m1_independence").passed


# ----- D12: isolated sandbox -----

def test_canary_writes_only_to_its_sandbox(tmp_path: Path) -> None:
    """D12 / D-T12: the canary writes ONLY under its given project_dir. A
    sibling 'user project' dir is never touched (constraint b: 绝不触发真实
    删除/上传 — the user's real project is untouched)."""
    sandbox = tmp_path / "canary-sandbox"
    user_project = tmp_path / "user-project"
    sandbox.mkdir()
    user_project.mkdir()
    # A sentinel in the user's project that must survive unchanged.
    sentinel = user_project / ".lecturecast" / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("user-data-must-survive")

    report = run_canary(sandbox, now_iso=NOW)
    assert report.passed

    # The canary's DB lives UNDER the sandbox, never under the user project.
    assert (sandbox / DB_REL).exists()
    assert not (user_project / "runtime").exists()
    assert not (user_project / DB_REL).exists()
    assert sentinel.read_text() == "user-data-must-survive"


def test_canary_zero_real_network_stubs_only(tmp_path: Path) -> None:
    """Constraint (b): the default canary uses deterministic stubs — no real
    HeyGen transport is constructed, no real network calls made, no real
    credits spent. The stubs are in-module and record their calls."""
    deleter, adapter = _StubDeleter(), _StubAdapter()
    report = run_canary(tmp_path, now_iso=NOW, deleter=deleter, adapter=adapter)
    assert report.passed
    # The stubs were driven (proving the mock path ran, not a real transport).
    assert len(deleter.calls) >= 1
    assert len(adapter.calls) >= 2


# ----- depth honesty (lesson #13) -----

def test_canary_invariants_document_server_scope(tmp_path: Path) -> None:
    """The server-side invariants (#2 ledger, #5 awaiting, #8 refund) document
    their CLIENT-OBSERVABLE depth honestly — the detail names the server / e6
    boundary rather than overclaiming full server-DB assertion."""
    report = _run(tmp_path)
    # #2 names the server canary's registry↔charges parity scope.
    assert "SERVER canary" in report.invariant("core_3_cost").detail or "§6" in report.invariant("core_3_cost").detail
    # #5 names the server-side ledger rows.
    assert "SERVER" in report.invariant("ledger_awaiting_deletion_recovery").detail
    # #8 names the server refund worker + e6 RecoveryDirectiveCatalog.
    assert "§5.3.10" in report.invariant("rollback_charged").detail
    assert "§5.5e6" in report.invariant("rollback_charged").detail


# ----- invalid estimate handling -----

def test_canary_rejects_malformed_estimate(tmp_path: Path) -> None:
    """A malformed estimate fails invariants #2/#3/#6 (validation fails) without
    crashing the canary — the harness records the failure, does not raise."""
    bad = {"estimate_status": "garbage"}
    report = run_canary(tmp_path, now_iso=NOW, pricing_estimate=bad, brief={"x": 1})
    assert report.passed is False
    assert report.invariant("estimate_equals_pricing").passed is False
    assert report.invariant("core_3_cost").passed is False


# ----- CLI leaf (commands/canary.py) -----

def test_canary_cli_leaf_passes() -> None:
    """The `lecturecast canary` CLI creates an isolated tempfile sandbox, runs
    the 8 invariants, prints a Chinese summary, and exits 0 on pass."""
    runner = CliRunner()
    result = runner.invoke(app, ["canary"])
    assert result.exit_code == 0, result.output
    assert "§5 line-489 canary" in result.output
    assert "全绿" in result.output
    for title in ("migration head", "M1", "删除恢复"):
        assert title in result.output


def test_canary_cli_leaf_json() -> None:
    """`--json` emits the full report payload."""
    runner = CliRunner()
    result = runner.invoke(app, ["canary", "--json"])
    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert len(payload["invariants"]) == 8
    assert payload["total_credits_projected"] == CANARY_CREDIT_CAP
