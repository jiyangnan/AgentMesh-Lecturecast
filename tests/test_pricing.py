"""PricingEstimate validation (§5.5c)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lecturecast.pricing import (
    PricingEstimateError,
    next_milestone_cost_or_fail,
    validate_pricing_estimate,
)
from lecturecast.protocol import canonical_digest


def _provisional(**overrides: Any) -> dict[str, Any]:
    """Real server provisional: minimum is the known subset (manifest=10),
    maximum is the full possible total (3 milestones × 10 = 30)."""
    base: dict[str, Any] = {
        "estimate_status": "provisional",
        "minimum_total": 10,
        "maximum_total": 30,
        "charge_model": "per_milestone_success",
        "pricing_version": "pricing.v1",
        "next_milestone_cost": 10,
        "applicable_milestones": ["manifest"],
        "per_milestone": {"manifest": 10},
    }
    base.update(overrides)
    return base


def _final(brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Final: minimum==maximum==30 (all 3 milestones applicable)."""
    est = _provisional(
        estimate_status="final", minimum_total=30, maximum_total=30,
        applicable_milestones=["manifest", "presenter_plan", "orchestration"],
        per_milestone={"manifest": 10, "presenter_plan": 10, "orchestration": 10},
    )
    if brief is not None:
        est["brief_digest"] = canonical_digest(brief)
    est["estimate_digest"] = canonical_digest(
        {k: v for k, v in est.items() if k != "estimate_digest"}
    )
    return est


# ---- structural validation ----

def test_valid_provisional():
    result = validate_pricing_estimate(_provisional(), protocol_version="1.1")
    assert result["estimate_status"] == "provisional"


def test_missing_required_field():
    est = _provisional()
    del est["charge_model"]
    with pytest.raises(PricingEstimateError, match="charge_model"):
        validate_pricing_estimate(est, protocol_version="1.1")


def test_wrong_pricing_version():
    with pytest.raises(PricingEstimateError, match="pricing_version"):
        validate_pricing_estimate(_provisional(pricing_version="pricing.v2"), protocol_version="1.1")


def test_wrong_charge_model():
    with pytest.raises(PricingEstimateError, match="charge_model"):
        validate_pricing_estimate(_provisional(charge_model="flat_fee"), protocol_version="1.1")


def test_min_gt_max():
    with pytest.raises(PricingEstimateError, match="minimum"):
        validate_pricing_estimate(_provisional(minimum_total=31, maximum_total=30), protocol_version="1.1")


def test_bool_cost_rejected():
    with pytest.raises(PricingEstimateError, match="next_milestone_cost"):
        validate_pricing_estimate(_provisional(next_milestone_cost=True), protocol_version="1.1")


def test_not_a_dict():
    with pytest.raises(PricingEstimateError, match="not a dict"):
        validate_pricing_estimate("not an estimate", protocol_version="1.1")


def test_applicable_milestone_missing_per_milestone():
    est = _provisional(applicable_milestones=["manifest", "orchestration"])
    # per_milestone only has "manifest" → set mismatch.
    with pytest.raises(PricingEstimateError, match="applicable_milestones"):
        validate_pricing_estimate(est, protocol_version="1.1")


# ---- final estimate integrity ----

def test_valid_final_with_brief():
    brief = {"schema_version": "1.1", "presenter": {}, "outputs": [], "constraints": [], "visual": {"palette": []}}
    est = _final(brief=brief)
    result = validate_pricing_estimate(est, protocol_version="1.1", brief=brief)
    assert result["estimate_status"] == "final"


def test_final_missing_brief_digest():
    est = _final(brief={"x": 1})
    del est["brief_digest"]
    with pytest.raises(PricingEstimateError, match="brief_digest"):
        validate_pricing_estimate(est, protocol_version="1.1")


def test_final_wrong_brief_digest():
    brief = {"key": "value"}
    est = _final(brief=brief)
    # Tamper with brief_digest.
    est["brief_digest"] = "sha256:deadbeef" + "0" * 54
    with pytest.raises(PricingEstimateError, match="brief_digest"):
        validate_pricing_estimate(est, protocol_version="1.1", brief=brief)


def test_final_wrong_estimate_digest():
    est = _final(brief={"x": 1})
    est["estimate_digest"] = "sha256:deadbeef" + "0" * 54
    with pytest.raises(PricingEstimateError, match="estimate_digest"):
        validate_pricing_estimate(est, protocol_version="1.1")


# ---- next_milestone_cost_or_fail ----

def test_v1_0_returns_legacy_constant():
    assert next_milestone_cost_or_fail(None, protocol_version="1.0") == 10


def test_v1_1_valid_estimate_returns_cost():
    session = {"pricing_estimate": _provisional(next_milestone_cost=10)}
    assert next_milestone_cost_or_fail(session, protocol_version="1.1") == 10


def test_v1_1_missing_estimate_fails_closed():
    with pytest.raises(PricingEstimateError, match="missing pricing_estimate"):
        next_milestone_cost_or_fail({"session_id": "s1"}, protocol_version="1.1")


def test_v1_1_none_session_fails_closed():
    with pytest.raises(PricingEstimateError, match="None"):
        next_milestone_cost_or_fail(None, protocol_version="1.1")


def test_v1_1_malformed_estimate_fails_closed():
    session = {"pricing_estimate": {"estimate_status": "garbage"}}
    with pytest.raises(PricingEstimateError):
        next_milestone_cost_or_fail(session, protocol_version="1.1")


def test_final_without_brief_fails_closed():
    """A final estimate without a Brief to bind against must fail closed — the
    Brief binding is the integrity guarantee for the final amount."""
    est = _final(brief={"x": 1})  # has brief_digest from a brief
    # Now validate WITHOUT passing the brief → must fail (can't verify binding).
    with pytest.raises(PricingEstimateError, match="requires a Brief"):
        validate_pricing_estimate(est, protocol_version="1.1", brief=None)


# ---- v1.1 workflow regression (§5.5c r9) ----

def test_v1_1_credit_returned_workflow_is_estimate_refresh():
    """v1.1 credit_returned → phase=estimate_refresh_required + director.next
    (no credit_cost). v1.0 → credit_approval_required + legacy cost."""
    from lecturecast.commands.director import _status_workflow
    from lecturecast.director import DirectorState

    def _state(pv: str) -> DirectorState:
        return DirectorState({
            "schema_version": pv, "project_id": "p1", "state_revision": 1,
            "server_url": "https://api.lecturecast.agentmesh360.com",
            "session_id": "s1", "session_status": "confirmed", "brief_version": 1,
            "catalog_version": "cv", "adapter_kind": "codex", "adapter_version": "1.0.0",
            **({"protocol_version": "1.1"} if pv == "1.1" else {}),
            "generation_id": "g1", "generation_status": "credit_returned",
            "updated_at": "2026-07-28T12:00:00Z",
        })

    gen = {"status": "credit_returned", "generation_id": "g1"}

    # v1.1
    wf11 = _status_workflow(_state("1.1"), gen, "/tmp")
    assert wf11["phase"] == "estimate_refresh_required"
    assert wf11["next_action"]["id"] == "director.next"
    assert "credit_cost" not in wf11["next_action"]

    # v1.0
    wf10 = _status_workflow(_state("1.0"), gen, "/tmp")
    assert wf10["phase"] == "credit_approval_required"
    assert wf10["next_action"]["id"] == "director.generate"
    assert wf10["next_action"]["credit_cost"] == 10


def test_v1_1_confirm_workflow_projects_final_estimate():
    """confirm_brief for v1.1 should reuse _session_workflow which projects
    the validated FINAL pricing_estimate (confirmed sessions require final)."""
    from lecturecast.commands.director import _session_workflow
    from lecturecast.director import DirectorState

    # A v1.1 confirmed session with a FINAL pricing_estimate bound to a Brief.
    brief = {"schema_version": "1.1", "presenter": {}}
    estimate = _final(brief=brief)
    session = {
        "session_id": "s1", "status": "confirmed", "brief_version": 1,
        "catalog_version": "cv", "updated_at": "2026-07-28T12:00:00Z",
        "pricing_estimate": estimate,
        "brief": brief,
    }
    state_payload = {
        "schema_version": "1.1", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.lecturecast.agentmesh360.com",
        "session_id": "s1", "session_status": "confirmed", "brief_version": 1,
        "catalog_version": "cv", "adapter_kind": "codex", "adapter_version": "1.0.0",
        "protocol_version": "1.1", "generation_id": None, "generation_status": None,
        "updated_at": "2026-07-28T12:00:00Z",
    }
    state = DirectorState(state_payload)
    workflow = _session_workflow(Path("/tmp"), state, session)
    assert workflow["phase"] == "credit_approval_required"
    assert "pricing_estimate" in workflow
    assert workflow["pricing_estimate"]["estimate_status"] == "final"
    assert workflow["pricing_estimate"]["next_milestone_cost"] == 10
    assert workflow["next_action"].get("credit_cost") == 10


def test_v1_1_confirmed_session_rejects_provisional_estimate():
    """A confirmed session with a provisional estimate must fail closed —
    credit approval requires a final estimate bound to the Brief."""
    from lecturecast.commands.director import _session_workflow
    from lecturecast.director import DirectorState
    from lecturecast.errors import LectureCastError

    estimate = _provisional(next_milestone_cost=10)
    session = {
        "session_id": "s1", "status": "confirmed", "brief_version": 1,
        "catalog_version": "cv", "updated_at": "2026-07-28T12:00:00Z",
        "pricing_estimate": estimate,
    }
    state_payload = {
        "schema_version": "1.1", "project_id": "p1", "state_revision": 1,
        "server_url": "https://api.lecturecast.agentmesh360.com",
        "session_id": "s1", "session_status": "confirmed", "brief_version": 1,
        "catalog_version": "cv", "adapter_kind": "codex", "adapter_version": "1.0.0",
        "protocol_version": "1.1", "generation_id": None, "generation_status": None,
        "updated_at": "2026-07-28T12:00:00Z",
    }
    state = DirectorState(state_payload)
    with pytest.raises(LectureCastError, match="final pricing_estimate"):
        _session_workflow(Path("/tmp"), state, session)


# ---- doc/skill contract guard (§5.5c) ----

def test_host_agent_contracts_describe_per_milestone_billing() -> None:
    """The host-agent behavioral contracts must describe per-milestone billing,
    not the retired single-Manifest/10-credit model."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "AGENTS.md", "README.md", "README.zh.md",
        "docs/LOCAL-WORKFLOW.md", "skills/codex/SKILL.md",
        "skills/shared/director-workflow.md",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        # Must NOT contain the retired single-Manifest hard-coded cost.
        assert "explicit 10-credit approval" not in text, f"{rel}: old 10-credit approval"
        assert "固定扣 10" not in text, f"{rel}: old 固定扣 10"
        assert "≥10 credits" not in text, f"{rel}: old ≥10 credits"
        assert "至少 10 credits" not in text, f"{rel}: old 至少 10 credits"
