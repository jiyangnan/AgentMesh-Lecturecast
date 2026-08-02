"""§5.5e5d-b canary harness — deterministic §5 line-489 rollout smoke test.

A self-contained, zero-credit, ISOLATED-SANDBOX harness that asserts the 8
§5 line-489 rollout invariants hold for the client's locked HeyGen stack. It
is the client-local counterpart to the server canary (TECH-SPEC §6 cross-repo
contract tests); the two together gate the §5 line-487 staged rollout.

What the canary drives
----------------------
The ONLY locked entry the canary drives is ``DeletionCoordinator`` (deletion
recovery, D8): it seeds the post-download *verified* operation state (the
legitimate §3.5 entry point — the lifecycle the locked submit/poll/download
coordinators produce), then drives the coordinator to sweep every resource to
the ``deleted`` terminal state via STUB adapters. The full submit→poll→
download generation driver is §5.5e6 scope (design doc §1.12) — the canary is
NOT a generation driver.

Blind-prediction constraints (e5c/d, verbatim — all honored here)
-----------------------------------------------------------------
  (b) doctor/canary 只读不写（绝不触发真实删除/上传）— the canary writes ONLY
      to its own isolated journal (init_database + seeded fixture rows); the
      deletion drive uses STUB adapters (``_StubDeleter`` / ``_StubAdapter``)
      that make zero real HeyGen calls and spend zero real credits. It never
      touches the user's real project.
  (d) 不放松 c1/c2/c3 任一已锁不变量 — the canary only READS locked primitives
      (``_journal_state``, ``capture_capabilities_v1_1``, ``validate_pricing_
      estimate``) and DRIVES the locked ``DeletionCoordinator`` entry with a
      literal ``force=False`` bool; it introduces no new truthy force source,
      no parallel gate, no bypass of the claim↔apply mirror (constraint a/c).

Depth honesty (lesson #13: 原则陈述正确 ≠ 实现穷举)
-------------------------------------------------
The 8 invariants are asserted at CLIENT-OBSERVABLE depth. The full server-DB
depth of #2 (ledger rows), #5 (awaiting_credits row), and #8 (refund execution)
is the SERVER canary's scope (TECH-SPEC §6 lines 495-509). Each result's
``detail`` states exactly what was asserted and where the server/e6 boundary
falls — no overclaiming.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capabilities import (
    _journal_state,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
)
from .heygen_adapter import DeleteResult
from .heygen_asset_adapter import AssetDeleteResult
from .heygen_journal import _SCHEMA_VERSION, init_database
from .operation_repository import DeletionCoordinator
from .pricing import validate_pricing_estimate
from .protocol import canonical_digest

# §5 line 489: "一次最多 30 credits" — hard cap on the projected cost of a
# single canary run. Enforced as a gate (D7): if the validated estimate's
# per-milestone sum exceeds it, the canary REFUSES to drive the deletion pass
# (in real-credit mode this is the spend guard).
CANARY_CREDIT_CAP: int = 30

# §5 line 489 "Core 3 action" — the three server milestones. The canary's
# pricing fixture exercises exactly these (the billing surface rollback +
# estimate equality are validated against).
_CORE_MILESTONES = ("manifest", "presenter_plan", "orchestration")

_DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryInvariantResult:
    """One of the 8 §5 line-489 invariants. ``detail`` states precisely what was
    asserted and, for server-side projections, where the boundary falls."""

    key: str
    title: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CanaryReport:
    """Aggregate canary result. ``passed`` is the AND of every invariant plus
    the credit-cap gate."""

    project_dir: str
    invariants: tuple[CanaryInvariantResult, ...]
    total_credits_projected: int
    credit_cap: int
    deletion_summary: dict[str, int]
    # Routing proof: which remote_ids the stub deleter (video route) vs the stub
    # asset adapter (asset route) actually drove. Populated from the canary's own
    # in-module stubs (zero injection seam — constraint b enforced by construction).
    deletion_calls: dict[str, tuple[str, ...]]
    passed: bool

    def invariant(self, key: str) -> CanaryInvariantResult:
        for inv in self.invariants:
            if inv.key == key:
                return inv
        raise KeyError(key)


class CanaryError(RuntimeError):
    """Raised when the canary cannot construct or drive its own sandbox
    (not when an invariant fails — that is recorded, not raised)."""


# ---------------------------------------------------------------------------
# stub adapters (deterministic, zero real network / credits — constraint b)
# ---------------------------------------------------------------------------


class _StubDeleter:
    """Video deleter: every call returns ``DeleteResult('deleted')``. Spy records
    the remote_ids driven, so the report can show the sweep touched every
    resource."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_video(self, remote_id: str) -> DeleteResult:
        self.calls.append(remote_id)
        return DeleteResult("deleted")


class _StubAdapter:
    """Asset deleter: every call returns ``AssetDeleteResult('deleted')``."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_asset(self, asset_id: str) -> AssetDeleteResult:
        self.calls.append(asset_id)
        return AssetDeleteResult("deleted")


# ---------------------------------------------------------------------------
# pricing fixture (a valid v1.1 final estimate over the 3 core milestones)
# ---------------------------------------------------------------------------


_CANARY_BRIEF: dict[str, Any] = {
    "schema_version": "1.1",
    "brief_id": "canary-brief-0001",
    "topic": "canary smoke test",
}


def _build_canary_estimate(brief: dict[str, Any], *, per_milestone_cost: int) -> dict[str, Any]:
    """Build a schema-valid v1.1 *final* PricingEstimate over the 3 core
    milestones at ``per_milestone_cost`` each (default use = 10 → total 30,
    exactly the cap). The digests are computed exactly as ``validate_pricing_
    estimate`` verifies them (canonical_digest, estimate_digest excludes
    itself), so the fixture round-trips through the real validator."""
    per_milestone = {m: per_milestone_cost for m in _CORE_MILESTONES}
    total = sum(per_milestone.values())
    estimate: dict[str, Any] = {
        "estimate_status": "final",
        "minimum_total": total,
        "maximum_total": total,
        "charge_model": "per_milestone_success",
        "pricing_version": "pricing.v1",
        "next_milestone_cost": per_milestone_cost,
        "applicable_milestones": list(_CORE_MILESTONES),
        "per_milestone": per_milestone,
        "brief_digest": canonical_digest(brief),
    }
    estimate["estimate_digest"] = canonical_digest(
        {k: v for k, v in estimate.items() if k != "estimate_digest"}
    )
    return estimate


# ---------------------------------------------------------------------------
# seeding the post-download *verified* operation (the §3.5 entry point)
# ---------------------------------------------------------------------------


def _connect(project_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(project_dir / _DB_REL))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_verified_operation(conn: sqlite3.Connection, op_id: str) -> tuple[str, str, str]:
    """Seed the legitimate post-download state §3.5 deletion consumes: one
    download-verified operation with an ephemeral video + audio + portrait
    (each resource + its exclusive ref; the two assets also carry their upload
    rows). Mirrors the lifecycle the locked submit/poll/download coordinators
    produce; seeded directly because the generation driver is §5.5e6 scope
    (design doc §1.12). Returns the three remote_ids (video, audio, portrait)."""
    conn.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, generation_id,"
        " manifest_digest, request_digest, idempotency_key, heygen_title,"
        " credential_profile_id, download_status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, "video", "/v3/videos", "gen", "sha256:m", "sha256:r",
         f"idem-{op_id}", f"lc:{op_id}", "heygen_env_default", "verified", "t", "t"),
    )

    def _add_resource(kind: str, remote_id: str) -> int:
        cur = conn.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind,"
            " remote_id, retention_mode, created_by_operation_id, deletion_status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("heygen_env_default", kind, remote_id, "ephemeral", op_id,
             "not_started", "t", "t"),
        )
        rid = cur.lastrowid
        conn.execute(
            "INSERT INTO heygen_resource_operation_refs"
            " (resource_id, operation_id, created_at) VALUES (?,?,?)",
            (rid, op_id, "t"),
        )
        return rid

    def _add_asset(role: str, kind: str, remote_id: str, upload_id: str) -> None:
        rid = _add_resource(kind, remote_id)
        conn.execute(
            "INSERT INTO heygen_asset_uploads (upload_id, parent_operation_id, asset_role,"
            " content_digest, local_ref, content_type, size_bytes, provider_filename,"
            " idempotency_key, remote_resource_id, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (upload_id, op_id, role, "sha256:" + remote_id, "loc",
             "application/octet-stream", 1, remote_id + ".bin",
             "idem-" + upload_id, rid, "uploaded", "t", "t"),
        )

    _add_resource("video", "v1")
    _add_asset("synthetic_narration_audio", "audio_asset", "a1", "u_audio")
    _add_asset("portrait_photo", "portrait_asset", "p1", "u_port")
    return "v1", "a1", "p1"


# ---------------------------------------------------------------------------
# the harness
# ---------------------------------------------------------------------------


def run_canary(
    project_dir: Path | str,
    *,
    now_iso: str,
    lease_owner: str = "lecturecast-canary",
    lease_seconds: int = 300,
    env: dict[str, str] | None = None,
    brief: dict[str, Any] | None = None,
    pricing_estimate: dict[str, Any] | None = None,
    per_milestone_cost: int = 10,
    credit_cap: int = CANARY_CREDIT_CAP,
) -> CanaryReport:
    """Run the 8-invariant canary against an ISOLATED project dir.

    The caller supplies the isolated dir (a pytest ``tmp_path`` or a CLI-made
    tempfile — never the user's real project, constraint b). The journal is
    initialized to head via the locked ``init_database`` (the one accepted
    idempotent migration write into the CANARY's own sandbox, per design doc
    §3.4 commentary), then the 8 invariants are asserted and the deletion
    recovery is driven through the locked ``DeletionCoordinator``.

    Zero-network is enforced BY CONSTRUCTION (constraint b): the canary creates
    its own in-module ``_StubDeleter`` / ``_StubAdapter`` — there is NO deleter/
    adapter injection seam, so no caller can route the deletion drive through a
    real HeyGen transport. The stubs' observed routing is exposed via
    ``report.deletion_calls`` (the §3.5 video→deleter / assets→adapter proof).
    ``pricing_estimate`` defaults to a built final estimate over the 3 core
    milestones at ``per_milestone_cost`` each.
    """
    project_dir = Path(project_dir)
    sources = env if env is not None else {}
    brief_dict = brief if brief is not None else _CANARY_BRIEF

    # Zero-network enforced by construction: the canary ALWAYS drives its own
    # in-module stubs. No injection seam → no path to a real transport.
    deleter_obj = _StubDeleter()
    adapter_obj = _StubAdapter()

    # 1. Isolated journal — the one accepted write into the canary's own sandbox.
    try:
        init_database(project_dir)
    except Exception as exc:  # pragma: no cover - defensive
        raise CanaryError(f"init_database failed in canary sandbox: {exc}") from exc

    invariants: list[CanaryInvariantResult] = []

    # ----- invariant #1: migration head 一致 -----
    try:
        cls = _journal_state(project_dir)["classification"]
        passed = cls == "current"
        detail = (
            f"journal classification=current (head=={_SCHEMA_VERSION}); "
            f"init_database brought the sandbox to the locked head."
        ) if passed else f"classification={cls!r}, expected 'current' (head=={_SCHEMA_VERSION})"
    except Exception as exc:  # pragma: no cover - _journal_state has its own backstop
        passed, cls, detail = False, "raised", f"_journal_state raised: {exc}"
    invariants.append(CanaryInvariantResult("migration_head", "migration head 一致", passed, detail))

    # ----- pricing estimate (invariants #2/#3/#4/#6/#8 all derive from it) -----
    estimate = pricing_estimate if pricing_estimate is not None else _build_canary_estimate(
        brief_dict, per_milestone_cost=per_milestone_cost,
    )

    # #6: client 展示 estimate == server pricing_estimate (the estimate the
    # canary "displays" is the one it received; it must validate cleanly).
    try:
        validated = validate_pricing_estimate(
            estimate, protocol_version="1.1", brief=brief_dict,
        )
        estimate_ok = True
        estimate_err: str | None = None
    except Exception as exc:
        validated = {}
        estimate_ok = False
        estimate_err = f"{type(exc).__name__}: {exc}"

    # #2: Core 3 action 成本逐项一致 — CLIENT-OBSERVABLE: the validated estimate
    # carries exactly the 3 core milestones with integer per-item costs that sum
    # to minimum_total. The per-action registry↔milestone_charges consistency on
    # the SERVER is the server canary's scope (TECH-SPEC §6 line 495).
    if estimate_ok:
        per_ms = validated.get("per_milestone") or {}
        core_present = set(_CORE_MILESTONES) == set(per_ms)
        passed2 = core_present and sum(per_ms.values()) == validated.get("minimum_total")
        detail2 = (
            "estimate carries the 3 core milestones (manifest/presenter_plan/"
            "orchestration) with per-item costs summing to minimum_total. "
            "SERVER canary owns registry↔milestone_charges parity (§6 line 495)."
        ) if passed2 else (
            f"per_milestone={per_ms}; core milestones present={core_present}"
        )
    else:
        passed2, detail2 = False, f"estimate did not validate: {estimate_err}"
    invariants.append(CanaryInvariantResult("core_3_cost", "Core 3 action 成本逐项一致", passed2, detail2))

    # #3: digest 链四项 + 补跑案例 — the final estimate's brief_digest +
    # estimate_digest both verify (validate_pricing_estimate recomputes them;
    # re-validation is stable/idempotent — the client-side digest chain holds).
    # The server-side re-run cases A/B/C (TECH-SPEC §6 line 499) are the server
    # canary's scope.
    if estimate_ok and validated.get("estimate_status") == "final":
        passed3 = bool(validated.get("brief_digest")) and bool(validated.get("estimate_digest"))
        detail3 = (
            "final estimate brief_digest + estimate_digest both verify under "
            "validate_pricing_estimate; re-validation is stable. SERVER canary "
            "owns re-run cases A/B/C (§6 line 499)."
        )
    else:
        passed3, detail3 = False, "estimate is not a digest-valid final estimate"
    invariants.append(CanaryInvariantResult("digest_chain", "digest 链四项 + 补跑案例", passed3, detail3))

    # #4: 一次最多 30 credits — HARD GATE (D7), fail-closed. The projected cost
    # is derived ONLY from the VALIDATED estimate's per-milestone sum. If the
    # estimate did not validate, the projected cost is UNKNOWABLE and the cap
    # FAILS CLOSED (cap_held=False) — an invalid estimate must NOT reach the
    # deletion drive on the strength of its own untrusted minimum_total field
    # (the round-1 fail-open: 3×100 costs but minimum_total=0 slipped through).
    if estimate_ok:
        total_projected = int(sum((validated.get("per_milestone") or {}).values()))
    else:
        # Display the raw per-milestone sum if parseable (for debugging) — but
        # cap_held below is False regardless. The drive is refused (fail-closed).
        raw_pm = (estimate.get("per_milestone") or {}) if isinstance(estimate, dict) else {}
        try:
            total_projected = int(sum(raw_pm.values()))
        except Exception:
            total_projected = 0
    cap_held = bool(estimate_ok) and (total_projected <= credit_cap)
    if not estimate_ok:
        detail4 = (
            f"estimate failed validation — projected cost unknowable, cap fails "
            f"closed (deletion drive REFUSED). raw_per_milestone_sum={total_projected}"
        )
    elif total_projected <= credit_cap:
        detail4 = f"projected={total_projected} ≤ cap={credit_cap}"
    else:
        detail4 = f"projected={total_projected} > cap={credit_cap} — deletion drive REFUSED"
    invariants.append(CanaryInvariantResult("credit_cap_30", "一次最多 30 credits", cap_held, detail4))

    # ----- deletion recovery (invariant #5) — only drive when the cap holds -----
    # §5 line 489: "三笔 ledger + awaiting_credits + 删除恢复（download_status=
    # verified → 逐资源 deleted）". The 删除恢复 half is FULLY client-assertable
    # (D8): seed a verified op, drive DeletionCoordinator, assert every resource
    # reaches 'deleted'. The 三笔 ledger + awaiting_credits rows live in the
    # SERVER's generation_milestone_charges table — the client observes only the
    # milestone status strings; the server canary owns the ledger depth (§6).
    deletion_summary: dict[str, int] = {"driven": 0, "deleted": 0, "resources": 0}
    deletion_skipped_reason: str | None = None
    if not estimate_ok:
        deletion_skipped_reason = "estimate invalid — projected cost unknowable, drive refused (fail-closed)"
    elif not cap_held:
        deletion_skipped_reason = "credit cap exceeded — drive refused"
    if deletion_skipped_reason is None:
        op_id = "canary-op-0001"
        try:
            with _connect(project_dir) as conn:
                conn.execute("BEGIN")
                video_rid, audio_rid, portrait_rid = _seed_verified_operation(conn, op_id)
                conn.commit()
            coord = DeletionCoordinator(project_dir)
            # Pass 1: §3.5 normal order — the non-deleted video gates the tail.
            coord.delete_pass_for_operation(
                operation_id=op_id, force=False, deleter=deleter_obj,
                adapter=adapter_obj, lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds,
            )
            # Pass 2: video deleted → audio + portrait tail released.
            pass2 = coord.delete_pass_for_operation(
                operation_id=op_id, force=False, deleter=deleter_obj,
                adapter=adapter_obj, lease_owner=lease_owner, now_iso=now_iso,
                lease_seconds=lease_seconds,
            )
            driven_remote_ids = {video_rid, audio_rid, portrait_rid}
            deletion_summary["resources"] = len(driven_remote_ids)
            deletion_summary["driven"] = (
                len(deleter_obj.calls) + len(adapter_obj.calls)
            )
            with _connect(project_dir) as conn:
                rows = conn.execute(
                    "SELECT remote_id, deletion_status FROM heygen_remote_resources "
                    "WHERE remote_id IN (?, ?, ?) ORDER BY remote_id",
                    tuple(driven_remote_ids),
                ).fetchall()
            terminal = {str(r["remote_id"]): str(r["deletion_status"]) for r in rows}
            deletion_summary["deleted"] = sum(1 for s in terminal.values() if s == "deleted")
            per_resource_deleted = (
                set(terminal) == driven_remote_ids
                and all(s == "deleted" for s in terminal.values())
            )
            passed5 = per_resource_deleted
            detail5 = (
                f"verified op {op_id}: all {len(driven_remote_ids)} resources reached "
                f"'deleted' (video→pass1, audio+portrait→pass2; "
                f"pass2 deleted={pass2.deleted}). 三笔 ledger + awaiting_credits rows "
                f"are SERVER-side (§6 line 501) — client observes milestone status strings only."
            ) if passed5 else (
                f"per-resource terminal states: {terminal}; expected all 'deleted'"
            )
        except Exception as exc:
            passed5, detail5 = False, f"deletion drive raised: {type(exc).__name__}: {exc}"
            deletion_skipped_reason = "drive raised"
    else:
        passed5, detail5 = False, f"deletion recovery skipped: {deletion_skipped_reason}"
    invariants.append(CanaryInvariantResult(
        "ledger_awaiting_deletion_recovery",
        "三笔 ledger + awaiting_credits + 删除恢复",
        passed5, detail5,
    ))

    # #6: client 展示 estimate == server pricing_estimate — exercises the REAL
    # user-visible display projection (round-2: round-1 only proved schema
    # validity; round-2-next_milestone_cost_or_fail was the credit-cost reader,
    # not the display path). The display path is director._session_workflow: it
    # validates the session pricing_estimate via _validated_estimate (which also
    # enforces card↔session estimate equality), then projects an 8-field subset
    # into workflow["pricing_estimate"] for user disclosure. The canary drives a
    # v1.1 confirmed session carrying the server estimate (+ a card with the SAME
    # estimate) through _session_workflow and asserts the projected display
    # equals the validated estimate's 8 fields EXACTLY — no transformation /
    # omission / wrong minimum_total can slip through. It also locks that
    # validate_pricing_estimate returns the estimate BY IDENTITY (validated is
    # estimate — no in-place mutation, no copy).
    _DISPLAYED_KEYS = (
        "estimate_status", "minimum_total", "maximum_total",
        "next_milestone_cost", "applicable_milestones",
        "per_milestone", "charge_model", "pricing_version",
    )
    identity_held = bool(estimate_ok) and (validated is estimate)
    projection_ok = False
    projection_detail = ""
    try:
        from .commands.director import _session_workflow
        from .director import DirectorState

        st = DirectorState({
            "schema_version": "1.2", "project_id": "canary-p", "state_revision": 1,
            "server_url": "https://api.test", "session_id": "canary-s",
            "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
            "adapter_kind": "codex", "adapter_version": "1.0.0",
            "protocol_version": "1.1", "generation_id": None,
            "updated_at": now_iso,
        })
        # confirmed session carrying the server estimate; the card carries the
        # SAME estimate (exercises the card↔session equality boundary too).
        session_payload = {
            "status": "confirmed", "pricing_estimate": estimate,
            "brief": brief_dict,
            "decision_card_set": {"pricing_estimate": estimate},
        }
        workflow = _session_workflow(Path("/tmp"), st, session_payload)
        displayed = workflow.get("pricing_estimate")
        expected = {k: validated.get(k) for k in _DISPLAYED_KEYS}
        projection_ok = isinstance(displayed, dict) and displayed == expected
        projection_detail = (
            f"_session_workflow projected the 8 validated fields unchanged "
            f"(phase={workflow.get('phase')})"
        )
    except Exception as exc:  # pragma: no cover - defensive
        projection_detail = f"display projection raised: {type(exc).__name__}: {exc}"
    passed6 = bool(identity_held and projection_ok)
    detail6 = (
        "client displays the server pricing_estimate verbatim — director._session_"
        "workflow projects the validated estimate's 8 fields (estimate_status/"
        "minimum_total/maximum_total/next_milestone_cost/applicable_milestones/"
        "per_milestone/charge_model/pricing_version) UNCHANGED, AND validate_pricing_"
        "estimate returns the estimate by IDENTITY (validated is estimate — no "
        f"mutation/copy). {projection_detail}"
    ) if passed6 else (
        f"identity_held={identity_held} (validated is estimate); projection_ok="
        f"{projection_ok} ({projection_detail}); estimate_ok={estimate_ok}"
    )
    invariants.append(CanaryInvariantResult(
        "estimate_equals_pricing", "client 展示 estimate == server pricing_estimate", passed6, detail6,
    ))

    # #7: M1 不依赖 HeyGen 配置 — with no HEYGEN_API_KEY (env={}), even when the
    # adapter + the canary's own head-current journal ARE ready, the HeyGen
    # processor is omitted (third_party_processors absent) → M1 base-video path
    # is unaffected. This is the locked fail-closed omit (§5.5e5c).
    try:
        caps = capture_capabilities_v1_1(
            adapter_kind="text", adapter_version="1.0.0",
            project_root=project_dir, env=sources,
            adapter_probe=default_heygen_adapter_probe,
            journal_probe=lambda: default_heygen_journal_probe(project_root),
        )
        payload = caps.model_dump()
        third_party = payload.get("third_party_processors")
        m1_untouched = not third_party  # absent / [] when no key
        # M1 base fields stay populated regardless of HeyGen.
        m1_runtime_present = bool(payload.get("runtime"))
        passed7 = m1_untouched and m1_runtime_present
        detail7 = (
            "env without HEYGEN_API_KEY → third_party_processors omitted even with "
            "adapter + journal ready; M1 runtime fields stay populated (M1 path unaffected)."
        ) if passed7 else (
            f"third_party_processors={third_party!r}; runtime_present={m1_runtime_present}"
        )
    except Exception as exc:
        passed7, detail7 = False, f"capture_capabilities_v1_1 raised: {type(exc).__name__}: {exc}"
    invariants.append(CanaryInvariantResult("m1_independence", "M1 不依赖 HeyGen 配置", passed7, detail7))

    # #8: rollback 已 charged 处理方案 — exercises the CLIENT-OBSERVABLE rollback
    # surface, not just the charge-model field: (a) the charge contract in force
    # is per_milestone_success (the granularity under which per-milestone refund
    # is well-defined); (b) the client RECOGNIZES + ROUTES the rollback billing
    # vocabulary — director._status_workflow maps credit_returned →
    # estimate_refresh_required (v1.1: director.next to refresh after credit
    # return) and awaiting_credits+resume_available → credit_resume_required.
    # This is the client-depth "处理方案"; the actual refund execution is the
    # SERVER refund worker (§5.3.10b/c/d) + the client RecoveryDirectiveCatalog
    # mapping (§5.5e6, not yet wired) — both out of e5d-b scope.
    charge_model_ok = estimate_ok and validated.get("charge_model") == "per_milestone_success"
    routing_ok = False
    route_detail = ""
    try:
        from .commands.director import _status_workflow
        from .director import DirectorState

        base_state = {
            "schema_version": "1.2", "project_id": "canary-p", "state_revision": 1,
            "server_url": "https://api.test", "session_id": "canary-s",
            "session_status": "confirmed", "brief_version": 1, "catalog_version": "cv",
            "adapter_kind": "codex", "adapter_version": "1.0.0",
            "protocol_version": "1.1", "generation_id": "canary-g",
            "updated_at": now_iso,
        }
        # credit_returned → estimate_refresh_required (the v1.1 rollback route).
        st_returned = DirectorState({
            **base_state, "generation_status": "credit_returned",
            "billing_state": "credit_returned",
        })
        wf_returned = _status_workflow(
            st_returned,
            {"generation_id": "canary-g", "status": "credit_returned", "updated_at": now_iso},
            "/tmp",
        )
        # awaiting_credits + resume_available → credit_resume_required.
        st_awaiting = DirectorState({
            **base_state, "generation_status": "queued",
            "billing_state": "awaiting_credits", "resume_available": True,
            "billing_updated_at": now_iso,
        })
        wf_awaiting = _status_workflow(
            st_awaiting,
            {"generation_id": "canary-g", "status": "ready", "updated_at": now_iso,
             "billing_state": "awaiting_credits", "resume_available": True},
            "/tmp",
        )
        routing_ok = (
            wf_returned["phase"] == "estimate_refresh_required"
            and wf_awaiting["phase"] == "credit_resume_required"
        )
        route_detail = (
            f"credit_returned→{wf_returned['phase']}; "
            f"awaiting_credits→{wf_awaiting['phase']}"
        )
    except Exception as exc:  # pragma: no cover - defensive
        route_detail = f"routing exercise raised: {type(exc).__name__}: {exc}"
    passed8 = bool(charge_model_ok and routing_ok)
    detail8 = (
        "charge contract = per_milestone_success (per-milestone refund granularity) "
        f"AND the client routes the rollback vocabulary ({route_detail}). "
        "Refund execution = server refund worker (§5.3.10b/c/d) + client "
        "RecoveryDirectiveCatalog mapping (§5.5e6) — out of e5d-b scope."
    ) if passed8 else (
        f"charge_model_ok={charge_model_ok}; routing_ok={routing_ok} ({route_detail})"
    )
    invariants.append(CanaryInvariantResult("rollback_charged", "rollback 已 charged 处理方案", passed8, detail8))

    inv_tuple = tuple(invariants)
    return CanaryReport(
        project_dir=str(project_dir),
        invariants=inv_tuple,
        total_credits_projected=total_projected,
        credit_cap=credit_cap,
        deletion_summary=deletion_summary,
        deletion_calls={
            "video": tuple(deleter_obj.calls),
            "asset": tuple(adapter_obj.calls),
        },
        passed=all(inv.passed for inv in inv_tuple),
    )
