"""PricingEstimate validation (§5.5c)."""

from __future__ import annotations

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
