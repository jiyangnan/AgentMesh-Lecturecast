"""PricingEstimate validation (§5.5c).

Server-authoritative pricing estimate parsing + validation. The client never
computes cost — it only consumes and displays the server's estimate. V1.1
sessions MUST have a valid estimate; missing/malformed → fail-closed (never
silently fall back to the legacy v1.0 single-charge cost).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .protocol import canonical_digest


class PricingEstimateError(ValueError):
    """Raised when a pricing estimate is missing, malformed, or inconsistent."""


_REQUIRED_FIELDS = (
    "estimate_status", "minimum_total", "maximum_total",
    "charge_model", "pricing_version",
)
_PRICING_VERSION = "pricing.v1"
_CHARGE_MODEL = "per_milestone_success"

# Load the PricingEstimate subschema from the v1.1 decision-card-set bundle.
_SCHEMA_PATH = (
    Path(__file__).with_name("protocol") / "schemas" / "v1.1" / "decision-card-set.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_PRICING_ESTIMATE_SCHEMA = _SCHEMA["$defs"]["PricingEstimate"]
_SCHEMA_VALIDATOR = Draft202012Validator(_PRICING_ESTIMATE_SCHEMA)


def validate_pricing_estimate(
    estimate: Any,
    *,
    protocol_version: str = "1.0",
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a server-authoritative PricingEstimate.

    Step 1: JSON Schema validation (catches unknown fields, wrong types,
    pattern constraints, value ranges).
    Step 2: Semantic checks (pricing_version, charge_model, applicable↔
    per_milestone consistency, sum(per_milestone)==minimum_total, bool
    rejection, final minimum==maximum, digest verification).

    Raises PricingEstimateError on any failure.
    """
    if not isinstance(estimate, dict):
        raise PricingEstimateError("estimate is not a dict")

    # Step 1: JSON Schema.
    errors = sorted(_SCHEMA_VALIDATOR.iter_errors(estimate), key=lambda e: list(e.absolute_path))
    if errors:
        loc = ".".join(str(p) for p in errors[0].absolute_path) or "document"
        raise PricingEstimateError(f"schema: {loc}: {errors[0].message}")

    # Step 2: Semantic.
    if estimate["pricing_version"] != _PRICING_VERSION:
        raise PricingEstimateError(f"pricing_version mismatch: {estimate['pricing_version']!r}")
    if estimate["charge_model"] != _CHARGE_MODEL:
        raise PricingEstimateError(f"charge_model mismatch: {estimate['charge_model']!r}")

    minimum = estimate["minimum_total"]
    maximum = estimate["maximum_total"]
    if isinstance(minimum, bool) or isinstance(maximum, bool):
        raise PricingEstimateError("bool is not a valid total")
    if minimum > maximum:
        raise PricingEstimateError(f"minimum_total {minimum} > maximum_total {maximum}")

    estimate_status = estimate["estimate_status"]
    applicable = estimate.get("applicable_milestones") or []
    per_milestone = estimate.get("per_milestone") or {}
    if isinstance(applicable, list) and isinstance(per_milestone, dict):
        if set(applicable) != set(per_milestone):
            raise PricingEstimateError("applicable_milestones != per_milestone keys")
        if len(applicable) != len(set(applicable)):
            raise PricingEstimateError("duplicate applicable_milestone")
        computed_min = 0
        for ms, cost in per_milestone.items():
            if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
                raise PricingEstimateError(f"per_milestone[{ms}] invalid cost: {cost!r}")
            computed_min += cost
        if computed_min != minimum:
            raise PricingEstimateError(
                f"sum(per_milestone)={computed_min} != minimum_total={minimum}"
            )

    # next_milestone_cost must be a positive plain int.
    next_cost = estimate.get("next_milestone_cost")
    if next_cost is not None:
        if isinstance(next_cost, bool) or not isinstance(next_cost, int) or next_cost <= 0:
            raise PricingEstimateError(f"invalid next_milestone_cost: {next_cost!r}")

    # Final estimate integrity.
    if estimate_status == "final":
        if minimum != maximum:
            raise PricingEstimateError("final estimate must have minimum==maximum")
        brief_digest = estimate.get("brief_digest")
        estimate_digest = estimate.get("estimate_digest")
        if not brief_digest:
            raise PricingEstimateError("final estimate missing brief_digest")
        if not estimate_digest:
            raise PricingEstimateError("final estimate missing estimate_digest")
        if brief is None:
            raise PricingEstimateError("final estimate requires a Brief for binding")
        computed = canonical_digest(brief)
        if computed != brief_digest:
            raise PricingEstimateError("final estimate brief_digest does not match brief")
        without_digest = {k: v for k, v in estimate.items() if k != "estimate_digest"}
        recomputed = canonical_digest(without_digest)
        if recomputed != estimate_digest:
            raise PricingEstimateError("estimate_digest verification failed")

    return estimate


def next_milestone_cost_or_fail(
    session: dict[str, Any] | None,
    *,
    protocol_version: str = "1.0",
    brief: dict[str, Any] | None = None,
) -> int:
    """Get the validated next_milestone_cost from the session's estimate.

    V1.0: returns the legacy constant (no estimate expected).
    V1.1: validates the estimate (with Brief binding if provided) and returns
    next_milestone_cost; raises PricingEstimateError on missing/malformed."""
    from .config import MANIFEST_CREDIT_COST

    if protocol_version == "1.0":
        return MANIFEST_CREDIT_COST

    if not session:
        raise PricingEstimateError("v1.1 session is None — cannot read estimate")
    estimate = session.get("pricing_estimate")
    if not estimate:
        raise PricingEstimateError("v1.1 session missing pricing_estimate")
    session_brief = brief or (session.get("brief") if isinstance(session, dict) else None)
    validated = validate_pricing_estimate(
        estimate, protocol_version="1.1", brief=session_brief,
    )
    cost = validated.get("next_milestone_cost")
    if cost is None or isinstance(cost, bool) or not isinstance(cost, int) or cost <= 0:
        raise PricingEstimateError(f"v1.1 estimate has no valid next_milestone_cost: {cost!r}")
    return cost
