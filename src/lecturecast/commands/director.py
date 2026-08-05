from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import typer

from ..capabilities import (
    capture_capabilities,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
    heygen_processor,
)
from ..commercial import require_commercial_access
from ..director import (
    DIRECTOR_ADAPTER_KINDS,
    DirectorClient,
    DirectorState,
    DirectorStateStore,
    derive_billing_state,
    load_source_file,
    normalize_adapter_identity,
    resolve_server_url,
)
from ..config import MANIFEST_CREDIT_COST, resolve_protocol_version
from ..errors import LectureCastError
from ..host_agent import (
    HOST_ADAPTER_VERSION,
    HOST_WORKFLOW_CONTRACT_VERSION,
    HostWorkflowStore,
    require_host_adapter,
    require_project_host_workflow,
)
from ..manifest import verify_recovery_catalog_signature as _verify_catalog_signature
from ..project import ProjectStore
from ..protocol import ClientCapabilities, canonical_digest, parse_client_capabilities
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)
brief_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(brief_app, name="brief", help="Show or confirm the server-backed Creative Brief.")

digital_human_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(
    digital_human_app,
    name="digital-human",
    help="Client-local digital-human capability decisions (D13).",
)


def _make_client(server_url: str) -> DirectorClient:
    require_commercial_access()
    return DirectorClient(server_url)


def _unexpected(exc: Exception, *, json_output: bool) -> None:
    fail(
        LectureCastError(
            code="core_unavailable",
            message="Director 操作未能完成。",
            next_action="保留当前本地项目，用相同命令重试。",
            cause=type(exc).__name__,
        ),
        json_output=json_output,
    )


def _adapter(kind: str, version: str) -> tuple[str, str]:
    if kind.strip() not in DIRECTOR_ADAPTER_KINDS:
        raise LectureCastError(
            code="manifest_incompatible",
            message="未知的 Agent Adapter。",
            next_action="使用 codex、claude-code、openclaw 或 text。",
        )
    try:
        normalized = normalize_adapter_identity(kind, version)
        if normalized[0] != "text" and normalized[1] != HOST_ADAPTER_VERSION:
            raise LectureCastError(
                code="client_upgrade_required",
                message="Adapter version 必须由当前安装的 Skill 合同决定。",
                next_action="移除手工版本覆盖，并在新的宿主 Agent 任务中重试。",
            )
        return normalized
    except ValueError:
        raise LectureCastError(
            code="manifest_incompatible",
            message="Adapter version 无效。",
            next_action="提供语义版本，例如 1.0.0。",
        ) from None


def _result(
    *,
    state: DirectorState,
    session: dict[str, Any] | None = None,
    generation: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"director": state.to_dict()}
    if session is not None:
        payload["session"] = session
        payload["decision_card_set"] = session.get("decision_card_set")
    if generation is not None:
        payload["generation"] = generation
    if project is not None:
        payload["project"] = project
    if workflow is not None:
        payload["workflow"] = workflow
    return payload


def _command_action(
    action_id: str,
    argv: list[str],
    *,
    approval: bool = False,
    credit_cost: int | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "id": action_id,
        "kind": "command",
        "argv": argv,
        "mutates": action_id not in {"director.next", "director.brief.show", "manifest.review"},
        "requires_user_approval": approval,
    }
    if credit_cost is not None:
        action["credit_cost"] = credit_cost
    return action


def _validated_estimate(
    session: dict[str, Any] | None, *, protocol_version: str = "1.0",
    brief: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate and return the session's pricing_estimate for v1.1 sessions.
    Returns None for v1.0 (no estimate expected). Raises LectureCastError on
    malformed/missing v1.1 estimate (no silent fallback). Also verifies
    card-level estimate matches session-level estimate."""
    if protocol_version != "1.1" or not session:
        return None
    estimate = session.get("pricing_estimate")
    if not estimate:
        raise LectureCastError(
            code="manifest_incompatible",
            message="v1.1 session 缺少 pricing_estimate。",
            next_action="重新运行 director next 刷新 session 后重试。",
        )
    # Card-level estimate must match session-level estimate (if both present).
    # If the session has a card_set, the card MUST carry the same estimate.
    card_set = session.get("decision_card_set")
    if isinstance(card_set, dict):
        card_estimate = card_set.get("pricing_estimate")
        if card_estimate is None:
            raise LectureCastError(
                code="manifest_incompatible",
                message="v1.1 card 缺少 pricing_estimate（与 session 顶层不一致）。",
                next_action="重新运行 director next 刷新 session 后重试。",
            )
        if card_estimate != estimate:
            raise LectureCastError(
                code="manifest_incompatible",
                message="card pricing_estimate 与 session 顶层 pricing_estimate 不一致。",
                next_action="重新运行 director next 刷新 session 后重试。",
            )
    from ..pricing import PricingEstimateError, validate_pricing_estimate

    try:
        return validate_pricing_estimate(
            estimate, protocol_version="1.1", brief=brief,
        )
    except PricingEstimateError as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message=f"server 定价预估无效：{exc}",
            next_action="重新运行 director next 刷新 session 后重试。",
        ) from None


def _pricing_credit_cost(
    session: dict[str, Any] | None, *, protocol_version: str = "1.0",
) -> int:
    """Get the validated next milestone credit cost from the server-authoritative
    pricing_estimate. V1.0 → legacy MANIFEST_CREDIT_COST. V1.1 → validated
    estimate; missing/malformed raises (no silent fallback to 10)."""
    from ..pricing import PricingEstimateError, next_milestone_cost_or_fail

    try:
        return next_milestone_cost_or_fail(session, protocol_version=protocol_version)
    except PricingEstimateError as exc:
        if protocol_version == "1.1":
            raise LectureCastError(
                code="manifest_incompatible",
                message=f"server 定价预估无效：{exc}",
                next_action="重新运行 director next 刷新 session 后重试。",
            ) from None
        return MANIFEST_CREDIT_COST


def _session_workflow(
    directory: Path,
    state: DirectorState,
    session: dict[str, Any],
) -> dict[str, Any]:
    root = str(directory.expanduser().resolve())
    card_set = session.get("decision_card_set")
    if isinstance(card_set, dict) and card_set.get("questions"):
        action = {
            "id": "director.answer",
            "kind": "host_choice",
            "argv_template": [
                "lecturecast",
                "director",
                "answer",
                root,
                "--question-id",
                "<question_id>",
                "--option-id",
                "<option_id>",
                "--catalog-version",
                str(state.payload["catalog_version"]),
                "--json",
            ],
            "mutates": True,
            "requires_user_approval": True,
        }
        phase = "decision_required"
    elif session["status"] == "ready_to_confirm":
        action = _command_action(
            "director.brief.show",
            ["lecturecast", "director", "brief", "show", root, "--json"],
        )
        phase = "brief_review_required"
    elif session["status"] == "confirmed":
        action = _command_action(
            "director.generate",
            ["lecturecast", "director", "generate", root, "--json"],
            approval=True,
            credit_cost=_pricing_credit_cost(session, protocol_version=state.protocol_version),
        )
        phase = "credit_approval_required"
    else:
        action = {"id": "workflow.stop", "kind": "stop", "mutates": False}
        phase = "stopped"
    workflow: dict[str, Any] = {
        "phase": phase,
        "policy": "execute_only_returned_next_action",
        "next_action": action,
    }
    # Project the server-authoritative pricing estimate (validated) for user
    # disclosure. maximum_total is NOT a start gate — it's advisory.
    estimate = _validated_estimate(
        session, protocol_version=state.protocol_version,
        brief=session.get("brief"),
    )
    if estimate:
        # A confirmed session must have a FINAL estimate (bound to the Brief via
        # brief_digest + estimate_digest). Provisional is insufficient for
        # credit approval.
        if session.get("status") == "confirmed" and estimate.get("estimate_status") != "final":
            raise LectureCastError(
                code="manifest_incompatible",
                message="confirmed session 必须有 final pricing_estimate（已绑定 Brief digest）。",
                next_action="重新运行 director next 刷新 session 后重试。",
            )
        workflow["pricing_estimate"] = {
            k: estimate.get(k)
            for k in (
                "estimate_status", "minimum_total", "maximum_total",
                "next_milestone_cost", "applicable_milestones",
                "per_milestone", "charge_model", "pricing_version",
            )
        }
    return workflow


def _can_release_manifest(generation: dict[str, Any], *, protocol_version: str) -> bool:
    """Whether it is safe to save the Manifest locally.
    - v1.0: status=ready (legacy compatibility).
    - v1.1: status=ready AND manifest milestone exists AND status==charged
      AND its artifact_digest matches generation.manifest_digest. When no
      manifest milestone charge row exists, fall back to the legacy single-charge
      M1 flow (server 裁决 A: the worker never creates an M1 charge row — M1
      stays on `generation.deducted_credits`). Only an already-paid ready
      generation with a delivered manifest_digest satisfies the fallback.
    """
    if generation.get("status") != "ready":
        return False
    if protocol_version != "1.1":
        return True  # v1.0 legacy
    charges = generation.get("milestone_charges") or []
    manifest_charge = next(
        (c for c in charges if c.get("milestone") == "manifest"), None,
    )
    if manifest_charge is not None:
        if manifest_charge.get("status") != "charged":
            return False
        artifact_digest = manifest_charge.get("artifact_digest")
        expected_digest = generation.get("manifest_digest")
        return (
            isinstance(expected_digest, str)
            and isinstance(artifact_digest, str)
            and artifact_digest == expected_digest
        )
    # No manifest milestone charge row: legacy single-charge M1 (裁决 A). A
    # ready generation that was actually charged and delivered a manifest digest
    # is released; the downstream save_manifest re-verifies the Ed25519
    # signature, so an over-broad gate cannot fabricate a manifest.
    deducted = generation.get("deducted_credits")
    return isinstance(deducted, int) and deducted > 0 and isinstance(
        generation.get("manifest_digest"), str
    )


def _presenter_plan_charged(generation: dict[str, Any]) -> bool:
    """Whether the M2 presenter_plan milestone is already charged. When it is,
    the status workflow must NOT re-offer create (idempotent re-run guard)."""
    charges = generation.get("milestone_charges") or []
    charge = next(
        (c for c in charges if c.get("milestone") == "presenter_plan"), None,
    )
    return charge is not None and charge.get("status") == "charged"


def _orchestration_plan_charged(generation: dict[str, Any]) -> bool:
    """Whether the M3 orchestration milestone is already charged. When it is,
    the status workflow must NOT re-offer create (idempotent re-run guard)."""
    charges = generation.get("milestone_charges") or []
    charge = next(
        (c for c in charges if c.get("milestone") == "orchestration"), None,
    )
    return charge is not None and charge.get("status") == "charged"


def _brief_m3_applicable(project_store: ProjectStore) -> bool:
    """Whether the M3 orchestration milestone applies to this project (tech spec
    §1.2 applicability matrix: M3 = photo / own_voice / bgm≠none). The signal
    lives in the Brief presenter — a photo avatar implies M3 (after M2), and an
    own_voice or BGM project needs M3 even without a digital human. A pure M1
    project (none avatar + stock voice + none bgm) never offers M3.

    Fail-closed: returns False on absent/malformed brief — a missing brief
    never triggers M3 (mirrors `_d13_brief_avatar`). Field values come from a
    brief already validated against the v1.1 CreativeBrief schema, so avatar /
    voice_mode are strings and bgm is str|None; the `bgm not in (None, "none")`
    check therefore sees only str-or-None (a non-str bgm cannot reach here)."""
    try:
        brief = project_store.load_brief_dict()
        if not isinstance(brief, dict):
            return False
        presenter = brief.get("presenter")
        if not isinstance(presenter, dict):
            return False
        avatar = presenter.get("avatar")
        voice_mode = presenter.get("voice_mode")
        bgm = presenter.get("bgm")
        if avatar == "photo":
            return True
        if voice_mode == "own_voice":
            return True
        if bgm not in (None, "none"):
            return True
        return False
    except Exception:
        return False


def _status_workflow(
    state: DirectorState,
    generation: dict[str, Any],
    root: str,
    project_store: ProjectStore | None = None,
) -> dict[str, Any]:
    """Build the workflow for a generation status response. Extracted so the
    v1.1/v1.0 credit_returned phase/action split is directly testable.
    ``project_store`` is used for the M2 branch (brief avatar intent) and the
    M3 branch (orchestration applicability) and may be None (callers that
    cannot resolve it just fall back to M1)."""
    gen_status = generation["status"]
    billing_state = generation.get("billing_state")
    resume_available = generation.get("resume_available") is True
    # Priority: billing_state (v1.1) > legacy gen_status.
    if billing_state == "awaiting_credits" and resume_available:
        action = _command_action(
            "director.generation.resume",
            ["lecturecast", "director", "generation-resume", root, "--json"],
            approval=True,
        )
        phase = "credit_resume_required"
    elif gen_status == "ready" and _can_release_manifest(generation, protocol_version=state.protocol_version):
        # M1 released. If the brief asks for a photo avatar, the next step is the
        # M2 presenter-plan create (needs a fresh user approval + capabilities).
        # After M2 (charged) — or for a project with no digital human but M3
        # needs (own_voice / bgm) — the next step is the M3 orchestration-plan
        # create. Otherwise this is the M1 manifest.review.
        if (
            state.protocol_version == "1.1"
            and project_store is not None
            and _d13_brief_avatar(project_store) == "photo"
            and not _presenter_plan_charged(generation)
        ):
            action = _command_action(
                "director.presenter.plan.create",
                ["lecturecast", "director", "generation-presenter-plan", root, "--json"],
                approval=True,
            )
            phase = "presenter_plan_create_required"
        elif (
            state.protocol_version == "1.1"
            and project_store is not None
            and _brief_m3_applicable(project_store)
            and not _orchestration_plan_charged(generation)
        ):
            action = _command_action(
                "director.orchestration.plan.create",
                ["lecturecast", "director", "generation-orchestration-plan", root, "--json"],
                approval=True,
            )
            phase = "orchestration_plan_create_required"
        else:
            action = _command_action(
                "manifest.review", ["lecturecast", "manifest", "review", root, "--json"],
                approval=True,
            )
            phase = "script_review_required"
    elif gen_status == "credit_returned":
        if state.protocol_version == "1.1":
            action = _command_action(
                "director.next", ["lecturecast", "director", "next", root, "--json"],
            )
            phase = "estimate_refresh_required"
        else:
            action = _command_action(
                "director.generate",
                ["lecturecast", "director", "generate", root, "--json"],
                approval=True, credit_cost=MANIFEST_CREDIT_COST,
            )
            phase = "credit_approval_required"
    else:
        action = _command_action(
            "director.status", ["lecturecast", "director", "status", root, "--json"],
        )
        phase = f"generation_{gen_status}"
    return {"phase": phase, "policy": "execute_only_returned_next_action", "next_action": action}


def _state_workflow(directory: Path, state: DirectorState) -> dict[str, Any]:
    root = str(directory.expanduser().resolve())
    # Priority: cached billing snapshot > generation recovery > session workflow.
    # Billing snapshot is advisory-only → always directs to director.status for
    # a fresh server response before the status workflow can offer resume.
    if state.billing_state == "awaiting_credits" and state.resume_available:
        action = _command_action(
            "director.status",
            ["lecturecast", "director", "status", root, "--json"],
        )
        phase = "billing_refresh_required"
    elif state.generation_id is not None:
        action = _command_action(
            "director.status",
            ["lecturecast", "director", "status", root, "--json"],
        )
        phase = "generation_recovery_required"
    elif state.payload["session_status"] == "collecting_decisions":
        action = _command_action(
            "director.next",
            ["lecturecast", "director", "next", root, "--json"],
        )
        phase = "decision_refresh_required"
    elif state.payload["session_status"] == "ready_to_confirm":
        action = _command_action(
            "director.brief.show",
            ["lecturecast", "director", "brief", "show", root, "--json"],
        )
        phase = "brief_review_required"
    elif state.payload["session_status"] == "confirmed":
        if state.protocol_version == "1.1":
            # v1.1 confirmed: no session context here → refresh first to get
            # the server-authoritative estimate before approving generation.
            action = _command_action(
                "director.next",
                ["lecturecast", "director", "next", root, "--json"],
            )
            phase = "estimate_refresh_required"
        else:
            action = _command_action(
                "director.generate",
                ["lecturecast", "director", "generate", root, "--json"],
                approval=True,
                credit_cost=MANIFEST_CREDIT_COST,
            )
            phase = "credit_approval_required"
    else:
        action = {"id": "workflow.stop", "kind": "stop", "mutates": False}
        phase = "stopped"
    return {
        "phase": phase,
        "policy": "execute_only_returned_next_action",
        "next_action": action,
    }


def _read_custom_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError("custom text file is too large")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message="Other 自定义文本无法读取。",
            next_action="提供不超过 4 KiB 的 UTF-8 文本文件。",
            cause=type(exc).__name__,
        ) from None
    if not value or "\x00" in value:
        raise LectureCastError(
            code="manifest_incompatible",
            message="Other 自定义文本为空或包含非法字符。",
            next_action="修复文本文件后重试。",
        )
    return value


@app.command("start")
def start(
    directory: Path = typer.Argument(Path(".")),
    source: Path = typer.Option(..., "--source", help="Path to bounded source-summary JSON."),
    server: str | None = typer.Option(None, "--server"),
    adapter: str = typer.Option(..., "--adapter"),
    adapter_version: str = typer.Option("1.0.0", "--adapter-version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a cloud Director Session bound to an existing local project."""
    try:
        require_project_host_workflow(directory, expected_adapter=adapter)
        ProjectStore(directory).load()
        state_store = DirectorStateStore(directory)
        if state_store.path.exists():
            raise LectureCastError(
                code="generation_conflict",
                message="本地项目已经绑定 Director Session。",
                next_action="运行 director next/status 恢复，或新建另一个本地项目。",
            )
        adapter, adapter_version = _adapter(adapter, adapter_version)
        server_url = resolve_server_url(server)
        protocol_version = resolve_protocol_version()
        session = _make_client(server_url).create_session(
            load_source_file(source), protocol_version=protocol_version,
        )
        state = state_store.create(
            server_url=server_url,
            session=session,
            adapter_kind=adapter,
            adapter_version=adapter_version,
            protocol_version=protocol_version,
        )
        emit(
            _result(
                state=state,
                session=session,
                workflow=_session_workflow(directory, state, session),
            ),
            json_output=json_output,
            message=f"Director Session 已创建：{state.session_id}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("next")
def next_step(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch the next stable DecisionCardSet from server state."""
    try:
        store = DirectorStateStore(directory)
        state = store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        session = _make_client(state.payload["server_url"]).get_session(
            state.session_id, protocol_version=state.protocol_version,
        )
        state = store.update(state, session=session)
        emit(
            _result(
                state=state,
                session=session,
                workflow=_session_workflow(directory, state, session),
            ),
            json_output=json_output,
            message=f"Session 状态：{session['status']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("resume")
def resume(
    directory: Path = typer.Argument(Path(".")),
    adapter: str = typer.Option(..., "--adapter"),
    adapter_version: str = typer.Option("1.0.0", "--adapter-version"),
    host_contract: str = typer.Option(..., "--host-contract"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rebind locally after verifying commercial access; no Director request is sent."""
    try:
        require_host_adapter(adapter, host_contract)
        require_commercial_access()
        project = ProjectStore(directory).load()
        adapter, adapter_version = _adapter(adapter, adapter_version)
        receipt = HostWorkflowStore(directory).bind(
            adapter=adapter,
            contract_version=host_contract,
        )
        store = DirectorStateStore(directory)
        previous = store.load()
        changed = (
            previous.payload["adapter_kind"],
            previous.payload["adapter_version"],
        ) != (adapter, adapter_version)
        state = store.bind_adapter(
            previous,
            adapter_kind=adapter,
            adapter_version=adapter_version,
        )
        payload = _result(state=state, project=project.to_dict())
        payload["resume"] = {
            "adapter_changed": changed,
            "network_requested": False,
            "director_network_requested": False,
            "commercial_access_verified": True,
            "credit_deducted": False,
            "capabilities_policy": "refresh_before_generate_on_adapter_mismatch",
        }
        payload["host_workflow"] = receipt
        payload["workflow"] = _state_workflow(directory, state)
        emit(
            payload,
            json_output=json_output,
            message=f"Director 已绑定当前 Agent：{adapter} {adapter_version}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("answer")
def answer(
    directory: Path = typer.Argument(Path(".")),
    question_id: str = typer.Option(..., "--question-id"),
    option_id: str = typer.Option(..., "--option-id"),
    catalog_version: str | None = typer.Option(None, "--catalog-version"),
    custom_text_file: Path | None = typer.Option(None, "--custom-text-file"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Submit stable IDs; display labels are never interpreted as IDs."""
    try:
        store = DirectorStateStore(directory)
        state = store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        session = _make_client(state.payload["server_url"]).answer(
            state.session_id,
            question_id=question_id,
            option_id=option_id,
            catalog_version=catalog_version or str(state.payload["catalog_version"]),
            custom_text=_read_custom_text(custom_text_file),
            protocol_version=state.protocol_version,
        )
        state = store.update(state, session=session)
        emit(
            _result(
                state=state,
                session=session,
                workflow=_session_workflow(directory, state, session),
            ),
            json_output=json_output,
            message=f"已提交 {question_id}={option_id}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@digital_human_app.command("decide")
def digital_human_decide(
    directory: Path = typer.Argument(Path(".")),
    choice: str = typer.Option(..., "--choice"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """§5.5e5d-d D13: route the user's digital-human downgrade decision.

    Client-local — does NOT hit the server. Option A (configure) routes to the
    read-only ``lecturecast doctor`` (HeyGen setup diagnosis); option B
    (downgrade) routes back to ``director generate --accept-digital-human-downgrade``
    (paid; the create_generation payload still omits third_party_processors).
    """
    try:
        # type-stability discipline: choice must be a str AND whitelisted.
        # A non-str or out-of-set value → LectureCastError (exit 2), never a
        # bare crash (exit 1) and never a silent fallthrough.
        if type(choice) is not str or choice not in {"configure", "downgrade"}:
            raise LectureCastError(
                code="invalid_choice",
                message="choice 必须是 configure 或 downgrade。",
                next_action="重新提交 D13 卡片选择（configure / downgrade）。",
            )
        store = DirectorStateStore(directory)
        state = store.load()
        root = str(directory.expanduser().resolve())
        if choice == "configure":
            # doctor is read-only (no project/server mutation) → mutates=False,
            # no approval. Built as a raw dict because _command_action's
            # mutate-heuristic would wrongly mark doctor mutates=True.
            action: dict[str, Any] = {
                "id": "lecturecast.doctor",
                "kind": "command",
                "argv": ["lecturecast", "doctor", "--project-root", root, "--json"],
                "mutates": False,
                "requires_user_approval": False,
            }
            phase = "digital_human_configure_required"
            message = (
                "请按 doctor 报告配置 HeyGen（设置 HEYGEN_API_KEY、确保 adapter 可导入、"
                "journal 就绪），再重新采集能力并运行 director generate。"
            )
        else:  # downgrade
            action = _command_action(
                "director.generate",
                [
                    "lecturecast",
                    "director",
                    "generate",
                    root,
                    "--accept-digital-human-downgrade",
                    "--json",
                ],
                approval=True,
            )
            phase = "credit_approval_required"
            message = "已记录降级裁定：将以下发 M1 基础视频（不含数字人）。"
        emit(
            _result(
                state=state,
                workflow={
                    "phase": phase,
                    "policy": "execute_only_returned_next_action",
                    "next_action": action,
                },
            ),
            json_output=json_output,
            message=message,
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@brief_app.command("show")
def show_brief(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the current candidate or confirmed Creative Brief."""
    try:
        store = DirectorStateStore(directory)
        state = store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        session = _make_client(state.payload["server_url"]).get_session(
            state.session_id, protocol_version=state.protocol_version,
        )
        state = store.update(state, session=session)
        if session.get("brief") is None:
            raise LectureCastError(
                code="brief_not_ready",
                message="Creative Brief 尚未形成。",
                next_action="继续处理 decision_card_set 后重试。",
            )
        emit(
            _result(
                state=state,
                session=session,
                workflow={
                    "phase": "brief_approval_required",
                    "policy": "execute_only_returned_next_action",
                    "next_action": _command_action(
                        "director.brief.confirm",
                        [
                            "lecturecast",
                            "director",
                            "brief",
                            "confirm",
                            str(directory.expanduser().resolve()),
                            "--expected-brief-version",
                            str(session["brief_version"]),
                            "--json",
                        ],
                        approval=True,
                    ),
                },
            ),
            json_output=json_output,
            message="Creative Brief 已读取。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@brief_app.command("confirm")
def confirm_brief(
    directory: Path = typer.Argument(Path(".")),
    expected_brief_version: int | None = typer.Option(None, "--expected-brief-version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Confirm the Brief without deducting credit, then persist it locally."""
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        client = _make_client(state.payload["server_url"])
        if expected_brief_version is None:
            current = client.get_session(
                state.session_id, protocol_version=state.protocol_version,
            )
            expected_brief_version = int(current["brief_version"])
        session = client.confirm_brief(
            state.session_id,
            expected_brief_version=expected_brief_version,
            protocol_version=state.protocol_version,
        )
        brief = session.get("brief")
        if not isinstance(brief, dict):
            raise LectureCastError(
                code="brief_not_ready",
                message="Server 未返回已确认 Creative Brief。",
                next_action="读取最新 Session 后重试。",
            )
        project_store = ProjectStore(directory)
        project = project_store.load()
        if project.payload["creative_brief_digest"] != canonical_digest(brief):
            project = project_store.save_brief(brief, expected_revision=project.revision)
        state = state_store.update(state, session=session)
        emit(
            _result(
                state=state,
                session=session,
                project=project.to_dict(),
                workflow=_session_workflow(directory, state, session),
            ),
            json_output=json_output,
            message="Creative Brief 已确认并保存；本步骤没有扣 credit。"
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


def _stored_capabilities(
    store: ProjectStore,
    *,
    adapter_kind: str,
    adapter_version: str,
    protocol_version: str = "1.0",
) -> ClientCapabilities | None:
    project = store.load()
    if project.payload["capability_digest"] is None:
        return None
    try:
        import json as _json

        document = parse_client_capabilities(
            _json.loads(store.capabilities_path.read_text(encoding="utf-8"))
        )
    except Exception as exc:
        raise LectureCastError(
            code="manifest_incompatible",
            message="已保存的 ClientCapabilities 无法读取。",
            next_action="重新运行 project capabilities 后重试。",
            cause=type(exc).__name__,
        ) from None
    if canonical_digest(document) != project.payload["capability_digest"]:
        raise LectureCastError(
            code="manifest_incompatible",
            message="ClientCapabilities 与项目 digest 不一致。",
            next_action="恢复项目文件或重新采集能力。",
        )
    saved_adapter = document.model_dump()["adapter"]
    if saved_adapter != {"kind": adapter_kind, "version": adapter_version}:
        return None
    # The stored capability must match the session's pinned protocol version;
    # otherwise re-capture under the pinned version.
    if document.model_dump().get("schema_version") != protocol_version:
        return None
    return document


def _stored_heygen_still_live(
    document: ClientCapabilities, directory: Path
) -> bool:
    """§5.5e5c round-2 (Codex round-1 B1): a stored capability snapshot
    reflects host state AT capture time. The M2 gate bills real PresenterPlan
    credits on third_party_processors[heygen].configured, so before Director
    reuses a stored snapshot it must confirm the live probes still agree. If
    the key was removed, an adapter method disappeared, or the journal was
    deleted / corrupted / downgraded since capture, the stored configured=true
    is now a false claim -> return False so the caller drops the snapshot and
    re-captures (which omits HeyGen).

    No HeyGen claim in the stored document -> nothing to invalidate -> True.
    Only the v1.1 path stores third_party_processors, so this is a no-op for
    v1.0 snapshots.

    Round-4 R3-4: top-level fail-closed backstop — the predicate never raises.
    If liveness cannot be established (probe raises unexpectedly), treat the
    snapshot as stale so the caller drops it and re-captures (omitting HeyGen)
    rather than billing on a snapshot whose live state is unknown."""
    try:
        payload = document.model_dump()
        heygen_configured = any(
            isinstance(processor, dict)
            and processor.get("provider") == "heygen"
            and processor.get("configured")
            for processor in (payload.get("third_party_processors") or [])
        )
        if not heygen_configured:
            return True
        live = heygen_processor(
            adapter_probe=default_heygen_adapter_probe,
            journal_probe=lambda: default_heygen_journal_probe(directory),
        )
        return live is not None
    except Exception:
        return False


def _d13_brief_avatar(project_store: ProjectStore) -> str | None:
    """§5.5e5d-d D13 intent signal: ``brief.presenter.avatar``.

    The Brief is M0 (confirmed before generate) and persisted at
    ``project_store.brief_path``. ``avatar == "photo"`` = user wants the HeyGen
    digital human; ``avatar == "none"`` = M1-only (never triggers D13). The
    intent lives in the Brief — NOT presenter_plan (which is M2/server-side).

    Fail-closed: returns None on absent/malformed brief or non-str avatar —
    None != "photo", so a missing/unreadable brief never triggers the card and
    the M1 path proceeds normally. The broad ``except Exception`` is intentional:
    D13 must NEVER abort the operator's billing flow because the brief file is
    unexpectedly unreadable; it silently falls back to "no photo intent".
    """
    try:
        brief = project_store.load_brief_dict()
        if not isinstance(brief, dict):
            return None
        presenter = brief.get("presenter")
        if not isinstance(presenter, dict):
            return None
        avatar = presenter.get("avatar")
        if type(avatar) is not str:
            return None
        return avatar
    except Exception:
        return None


def _d13_heygen_configured(capabilities: ClientCapabilities) -> bool:
    """§5.5e5d-d D13 capability signal: is HeyGen configured+live right now?

    Mirrors the canonical predicate in ``_stored_heygen_still_live`` (@755-760,
    locked e5c round-4) — same expression, same source: ``third_party_processors``
    presence + ``provider == "heygen"`` + ``configured`` truthy. Keep the two in
    sync if the HeyGen capability shape ever changes. ``capture_capabilities_v1_1``
    sets ``third_party_processors = [processor]`` ONLY when processor is not None
    (env HEYGEN_API_KEY + adapter probe + journal probe all pass), so key
    presence is the configured+live truth — the B1 stale-snapshot guard (@820)
    has already dropped + recaptured if live state diverged from the snapshot.
    """
    payload = capabilities.model_dump()
    return any(
        isinstance(processor, dict)
        and processor.get("provider") == "heygen"
        and processor.get("configured")
        for processor in (payload.get("third_party_processors") or [])
    )


def _d13_decision_action(root: str) -> dict[str, Any]:
    """§5.5e5d-d D13 interactive downgrade card (next_action).

    Client-local ``host_choice``: the server never learns about the local
    capability gap (§0 Principle 6: advisory never uploaded), so this card is
    NOT driven by ``session.decision_card_set`` — question/options are inline.
    The host fills ``<option_id>`` (configure|downgrade) into ``argv_template``
    and runs ``director digital-human decide``, which routes option A to the
    read-only doctor (HeyGen setup diagnosis) and option B back to
    ``director generate --accept-digital-human-downgrade`` (paid; payload still
    omits ``third_party_processors`` — configured source-of-truth is untouched).
    """
    return {
        "id": "director.digital_human.decide",
        "kind": "host_choice",
        "question_id": "digital_human_downgrade",
        "question_label": (
            "检测到 Creative Brief 指定 presenter.avatar=photo（要数字人），"
            "但本机尚未配置 HeyGen（缺 HEYGEN_API_KEY / adapter / journal）。请裁定："
        ),
        "options": [
            {
                "id": "configure",
                "label": "配置 HEYGEN_API_KEY 并重新采集（配置成功后可出数字人）",
            },
            {
                "id": "downgrade",
                "label": "降级为 M1 基础视频（本次不出数字人，仅口播；M2/M3 不触发）",
            },
        ],
        "argv_template": [
            "lecturecast",
            "director",
            "digital-human",
            "decide",
            root,
            "--choice",
            "<option_id>",
            "--json",
        ],
        "mutates": True,
        "requires_user_approval": True,
    }


@app.command("generate")
def generate(
    directory: Path = typer.Argument(Path(".")),
    generation_id: str | None = typer.Option(None, "--generation-id"),
    accept_digital_human_downgrade: bool = typer.Option(
        False,
        "--accept-digital-human-downgrade",
        help="§5.5e5d-d D13: user consented to the M1 downgrade (option B). "
        "Skips the interactive card; create_generation proceeds and the payload "
        "still omits third_party_processors (no false configured=true).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reserve one stable generation ID, then request the paid Manifest once."""
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        if state.payload["session_status"] != "confirmed":
            raise LectureCastError(
                code="brief_not_ready",
                message="Creative Brief 尚未确认。",
                next_action="先运行 director brief confirm；该步骤不会扣 credit。",
            )
        existing_id = state.generation_id
        if existing_id is not None and generation_id not in {None, existing_id}:
            raise LectureCastError(
                code="generation_conflict",
                message="本地项目已经锁定另一个 generation_id。",
                next_action="继续使用原 generation_id 查询或重试，不要创建第二笔 credit。",
            )
        selected_id = existing_id or generation_id or f"generation_{uuid.uuid4().hex}"
        if existing_id is None:
            state = state_store.update(
                state,
                generation_id=selected_id,
                generation_status="reserved",
            )

        project_store = ProjectStore(directory)
        adapter_kind = str(state.payload["adapter_kind"])
        adapter_version = str(state.payload["adapter_version"])
        capabilities = _stored_capabilities(
            project_store,
            adapter_kind=adapter_kind,
            adapter_version=adapter_version,
            protocol_version=state.protocol_version,
        )
        if capabilities is not None and state.protocol_version == "1.1":
            # §5.5e5c round-2 (B1): the stored snapshot only proves digest /
            # adapter / schema consistency — it does NOT re-probe HeyGen. Drop
            # it if the live host can no longer serve what the snapshot claims,
            # so a stale configured=true cannot bill an unexecutable capability.
            if not _stored_heygen_still_live(capabilities, directory):
                capabilities = None
        if capabilities is None:
            if state.protocol_version == "1.1":
                # §5.5e5c: pass real adapter + journal probes so the HeyGen
                # capability is reported when the shipped stack is actually
                # importable + the journal is not in a refuse-downgrade state.
                # The key is still gated inside heygen_processor (env HEYGEN_API_KEY).
                capabilities = capture_capabilities_v1_1(
                    adapter_kind=adapter_kind,
                    adapter_version=adapter_version,
                    project_root=directory,
                    repo_root=Path(__file__).resolve().parents[3],
                    adapter_probe=default_heygen_adapter_probe,
                    journal_probe=lambda: default_heygen_journal_probe(directory),
                )
            else:
                capabilities = capture_capabilities(
                    adapter_kind=adapter_kind,
                    adapter_version=adapter_version,
                    project_root=directory,
                    repo_root=Path(__file__).resolve().parents[3],
                )
            project = project_store.load()
            project_store.save_capabilities(
                capabilities, expected_revision=project.revision
            )

        # §5.5e5d-d D13: lazy interactive downgrade card. Fire ONLY when all four
        # hold: (1) v1.1 session (v1.0 has no HeyGen concept); (2) user wants the
        # digital human (brief.presenter.avatar == "photo"); (3) HeyGen is not
        # configured+live right now (after the B1 stale-snapshot guard above);
        # (4) user has NOT already consented to the M1 downgrade. Any path that
        # fails one of these (M1 avatar=none, already-configured, v1.0, or
        # post-consent option B) proceeds to create_generation unchanged. The
        # card is client-local — the server is NOT informed of the capability
        # gap (§0 Principle 6); create_generation's payload still omits
        # third_party_processors either way (configured source-of-truth =
        # capture_capabilities_v1_1, untouched by D13).
        if (
            state.protocol_version == "1.1"
            and not accept_digital_human_downgrade
            and _d13_brief_avatar(project_store) == "photo"
            and not _d13_heygen_configured(capabilities)
        ):
            emit(
                _result(
                    state=state,
                    workflow={
                        "phase": "digital_human_decision_required",
                        "policy": "execute_only_returned_next_action",
                        "next_action": _d13_decision_action(
                            str(directory.expanduser().resolve())
                        ),
                    },
                ),
                json_output=json_output,
                message=(
                    "检测到 avatar=photo 但本机未配置 HeyGen —— 已拦截 "
                    "create_generation，需用户裁定（配置 / 降级 M1）。"
                ),
            )
            return

        # §5.5e5d-d D13 payload-omission guard (fail-closed, defense-in-depth).
        # create_generation's payload must not carry a third_party_processors
        # entry that is not configured+live (§0.3 / §0 Principle 6: advisory is
        # never uploaded as a configured capability). capture_capabilities_v1_1
        # already omits the key when unconfigured (heygen_processor returns None
        # or configured=True — never configured=False), and the stored snapshot
        # is digest-bound (hand-tampering breaks _verify_documents). This guard
        # enforces the contract AT THE UPLOAD BOUNDARY too, so it holds
        # regardless of the stored doc's shape. It NEVER adds the key (no
        # force-include, no new truthy source — §2.4); it refuses to forward an
        # inconsistent doc that capture cannot produce (a present-but-not-
        # configured entry = local corruption; recapture to fix).
        capabilities_payload = capabilities.model_dump()
        if (
            state.protocol_version == "1.1"
            and "third_party_processors" in capabilities_payload
            and not _d13_heygen_configured(capabilities)
        ):
            raise LectureCastError(
                code="manifest_incompatible",
                message="本地能力快照声明了一个未配置的 third_party_processor（capture 不会产生此状态）。",
                next_action="重新运行 lecturecast project capabilities 采集能力，再 director generate。",
            )
        generation = _make_client(state.payload["server_url"]).create_generation(
            state.session_id,
            generation_id=selected_id,
            expected_brief_version=int(state.payload["brief_version"]),
            capabilities=capabilities_payload,
            protocol_version=state.protocol_version,
        )
        state = state_store.update(state, generation=generation)
        emit(
            _result(
                state=state,
                generation=generation,
                workflow={
                    "phase": f"generation_{generation['status']}",
                    "policy": "execute_only_returned_next_action",
                    "next_action": _command_action(
                        "director.status",
                        [
                            "lecturecast",
                            "director",
                            "status",
                            str(directory.expanduser().resolve()),
                            "--json",
                        ],
                    ),
                },
            ),
            json_output=json_output,
            message=f"Generation 状态：{generation['status']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("status")
def status(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch generation status and save a ready signed Manifest locally."""
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        if state.generation_id is None:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Manifest generation。",
                next_action="先运行 director generate。",
            )
        generation = _make_client(state.payload["server_url"]).get_generation(
            state.generation_id, protocol_version=state.protocol_version,
        )
        state = state_store.update(state, generation=generation)
        project_store = ProjectStore(directory)
        project = project_store.load()
        if _can_release_manifest(generation, protocol_version=state.protocol_version):
            manifest = generation.get("manifest")
            if not isinstance(manifest, dict):
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="ready generation 没有有效 Manifest。",
                    next_action="保留 generation_id 并联系支持；不要重复扣 credit。",
                )
            project = project_store.save_manifest(
                manifest, expected_revision=project.revision
            )
        if generation["status"] == "credit_returned":
            state = state_store.release_refunded_generation(state)
        root = str(directory.expanduser().resolve())
        emit(
            _result(
                state=state,
                generation=generation,
                project=project.to_dict(),
                workflow=_status_workflow(state, generation, root, project_store),
            ),
            json_output=json_output,
            message=f"Generation 状态：{generation['status']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("generation-resume")
def generation_resume(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Re-attempt billing for an awaiting_credits generation after the user tops up."""
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        if state.protocol_version != "1.1":
            raise LectureCastError(
                code="manifest_incompatible",
                message="generation-resume 是 v1.1 里程碑计费功能，v1.0 项目不支持。",
                next_action="v1.0 项目请用 director status 查看状态。",
            )
        if state.generation_id is None:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Manifest generation。",
                next_action="先运行 director generate。",
            )
        generation = _make_client(state.payload["server_url"]).resume_generation(
            state.generation_id, protocol_version=state.protocol_version,
        )
        state = state_store.update(state, generation=generation)
        project_store = ProjectStore(directory)
        project = project_store.load()
        if _can_release_manifest(generation, protocol_version=state.protocol_version):
            manifest = generation.get("manifest")
            if not isinstance(manifest, dict):
                raise LectureCastError(
                    code="manifest_incompatible",
                    message="ready generation 没有有效 Manifest。",
                    next_action="保留 generation_id 并联系支持；不要重复扣 credit。",
                )
            project = project_store.save_manifest(
                manifest, expected_revision=project.revision
            )
        root = str(directory.expanduser().resolve())
        emit(
            _result(
                state=state,
                generation=generation,
                project=project.to_dict(),
                workflow=_status_workflow(state, generation, root, project_store),
            ),
            json_output=json_output,
            message=f"Generation resume 完成：billing_state={generation.get('billing_state', 'N/A')}。",
        )
    except LectureCastError as error:
        # Build a command-specific workflow for resume errors (don't use the
        # generic fail() — the user needs structured next_action guidance).
        root = str(directory.expanduser().resolve())
        # §5.5e6 #121: if the v1.3 state carries a pre-signed recovery catalog,
        # present the matching directive first (fail-closed: an unverified or
        # non-matching catalog falls through to _resume_error_workflow).
        recovery_workflow = _recovery_workflow(
            error, state.recovery_catalog, root,
            m2_context=_project_in_m2_context(directory),
        )
        if recovery_workflow is not None:
            emit(
                {"director": state.to_dict(), "error": error.to_dict(), "workflow": recovery_workflow},
                json_output=json_output,
                message=error.message,
            )
            raise typer.Exit(code=1)
        workflow = _resume_error_workflow(error, root)
        if workflow is not None:
            emit(
                {"director": state.to_dict(), "error": error.to_dict(), "workflow": workflow},
                json_output=json_output,
                message=error.message,
            )
            raise typer.Exit(code=1)
        else:
            fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("generation-presenter-plan")
def generation_presenter_plan(
    directory: Path = typer.Argument(Path(".")),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="M2 risk-confirmation credential: confirm you have reviewed the "
        "HeyGen disclosure (heygen-transfer-2026-07-27). Billing is deducted "
        "on create; absent --yes the command refuses.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Request and persist the paid M2 PresenterPlan (digital-human edition).

    M2 is v1.1-only and only meaningful when the Creative Brief asks for a photo
    avatar (avatar=photo). The command collects the independent risk-confirmation
    credential (--yes), re-fetches/reuses the capabilities snapshot, creates the
    plan (which bills presenter_plan credits), verifies + persists it read-only
    (digest-bound to the released Manifest), and persists the provider recovery
    catalog into v1.3 state for M2-context resume errors.
    """
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        if state.protocol_version != "1.1":
            raise LectureCastError(
                code="manifest_incompatible",
                message="generation-presenter-plan 是 v1.1 里程碑计费功能，v1.0 项目不支持。",
                next_action="v1.0 项目请用 director status 查看状态。",
            )
        if state.generation_id is None:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Manifest generation。",
                next_action="先运行 director generate。",
            )
        project_store = ProjectStore(directory)
        project = project_store.load()
        # M2 preconditions: the Brief asked for a photo avatar (never trigger
        # for the M1 own_voice path), and the manifest must already be released.
        if _d13_brief_avatar(project_store) != "photo":
            raise LectureCastError(
                code="m2_not_ready",
                message="Creative Brief 未指定 presenter.avatar=photo，M2 数字人不需要。",
                next_action="该 project 走 M1 own_voice 路径；无需生成 PresenterPlan。",
            )
        # Idempotent re-run: a previously persisted plan short-circuits — no
        # second create_and_charge (no double billing at the CLI layer). This
        # precedes the manifest_ready gate because a persisted plan advances the
        # project to presenter_plan_ready (which is not manifest_ready).
        if project_store.presenter_plan_path.exists():
            root = str(directory.expanduser().resolve())
            reloaded = project_store.load()
            generation = _m2_generation_view(
                generation_id=state.generation_id,
                updated_at=state.payload["updated_at"],
                billing=_m2_charges_from_project(reloaded),
                manifest_digest=reloaded.payload["production_manifest_digest"],
            )
            emit(
                _result(
                    state=state,
                    generation=generation,
                    project=reloaded.to_dict(),
                    workflow=_status_workflow(state, generation, root, project_store),
                ),
                json_output=json_output,
                message="PresenterPlan 已存在；未重复扣费。",
            )
            return
        if project.payload["status"] != "manifest_ready":
            raise LectureCastError(
                code="manifest_incompatible",
                message="M1 Manifest 尚未落盘，无法请求 M2 PresenterPlan。",
                next_action="先运行 director status 保存已签名的 Manifest。",
            )

        # Capabilities: reuse the stored snapshot (with the B1 stale guard) or
        # re-capture, mirroring `generate`. Only the v1.1 capture path is valid
        # for M2 (the M2 gate validates ClientCapabilitiesV1_1).
        adapter_kind = str(state.payload["adapter_kind"])
        adapter_version = str(state.payload["adapter_version"])
        capabilities = _stored_capabilities(
            project_store,
            adapter_kind=adapter_kind,
            adapter_version=adapter_version,
            protocol_version=state.protocol_version,
        )
        if capabilities is not None and state.protocol_version == "1.1":
            if not _stored_heygen_still_live(capabilities, directory):
                capabilities = None
        if capabilities is None:
            capabilities = capture_capabilities_v1_1(
                adapter_kind=adapter_kind,
                adapter_version=adapter_version,
                project_root=directory,
                repo_root=Path(__file__).resolve().parents[3],
                adapter_probe=default_heygen_adapter_probe,
                journal_probe=lambda: default_heygen_journal_probe(directory),
            )
            project_store.save_capabilities(
                capabilities, expected_revision=project.revision
            )
        # §5.5e5d-d D13 payload-omission guard (fail-closed): never forward a
        # snapshot that claims a third_party_processor but is not configured+live.
        capabilities_payload = capabilities.model_dump()
        if (
            "third_party_processors" in capabilities_payload
            and not _d13_heygen_configured(capabilities)
        ):
            raise LectureCastError(
                code="manifest_incompatible",
                message="本地能力快照声明了一个未配置的 third_party_processor（capture 不会产生此状态）。",
                next_action="重新运行 lecturecast project capabilities 采集能力，再 director generation-presenter-plan。",
            )
        # M2 risk-confirmation credential (tech spec §2.2): the user must have
        # reviewed the HeyGen disclosure. --yes is the explicit confirmation —
        # absent it the command refuses before any network call.
        if not yes:
            raise LectureCastError(
                code="approval_required",
                message="M2 PresenterPlan 会产生扣费；需要先向用户展示 HeyGen 披露并取得确认。",
                next_action="展示 heygen-transfer-2026-07-27 披露全文，明确通过后带 --yes 重试。",
            )

        result = _make_client(state.payload["server_url"]).create_presenter_plan(
            state.generation_id,
            capabilities=capabilities_payload,
            approved=yes,
            protocol_version=state.protocol_version,
        )
        plan = result["presenter_plan"]
        billing = result.get("billing") or []
        # Derive the billing snapshot from the server-sent charges (mirror of the
        # server's aggregate_billing_state) so the v1.2/v1.3 state is consistent.
        billing_state, resume_available = derive_billing_state(billing)
        project = project_store.save_presenter_plan(
            plan, expected_revision=project.revision
        )
        generation = _m2_generation_view(
            generation_id=state.generation_id,
            updated_at=plan["created_at"],
            billing=billing,
            manifest_digest=project.payload["production_manifest_digest"],
        )
        generation["billing_state"] = billing_state
        generation["resume_available"] = resume_available
        generation["recovery_catalog"] = result.get("recovery_catalog")
        state = state_store.update(state, generation=generation)
        root = str(directory.expanduser().resolve())
        emit(
            _result(
                state=state,
                generation=result,
                project=project.to_dict(),
                workflow=_status_workflow(state, generation, root, project_store),
            ),
            json_output=json_output,
            message=f"PresenterPlan 已保存：{plan['presenter_plan_id']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("generation-orchestration-plan")
def generation_orchestration_plan(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Request and persist the paid M3 OrchestrationPlan (digital-human edition).

    M3 is v1.1-only and applies when the Creative Brief needs it (photo avatar
    after M2, own_voice, or BGM). It carries NO approval credential (裁决 B): M3
    is local ffmpeg/F5 orchestration with no third-party media transfer, so the
    independent risk-confirmation that M2 requires has no analogue here. The
    command re-fetches/reuses the capabilities snapshot, creates the plan (which
    bills orchestration credits), verifies + persists it read-only (digest-bound
    to the released Manifest), and persists the base recovery catalog into v1.3
    state for M3-context resume errors.
    """
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        if state.protocol_version != "1.1":
            raise LectureCastError(
                code="manifest_incompatible",
                message="generation-orchestration-plan 是 v1.1 里程碑计费功能，v1.0 项目不支持。",
                next_action="v1.0 项目请用 director status 查看状态。",
            )
        if state.generation_id is None:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Manifest generation。",
                next_action="先运行 director generate。",
            )
        project_store = ProjectStore(directory)
        project = project_store.load()
        # M3 applicability: the Brief must need orchestration (photo / own_voice /
        # bgm≠none). A pure M1 project (none + stock + none) never triggers M3.
        if not _brief_m3_applicable(project_store):
            raise LectureCastError(
                code="m3_not_ready",
                message="Creative Brief 未要求 M3 orchestration（photo/own_voice/bgm），无需生成。",
                next_action="该 project 走 M1 纯路径；无需 OrchestrationPlan。",
            )
        # Idempotent re-run: a previously persisted plan short-circuits — no
        # second create_and_charge (no double billing at the CLI layer). This
        # precedes the manifest_ready gate because a persisted plan advances the
        # project to orchestration_plan_ready (which is not manifest_ready).
        if project_store.orchestration_plan_path.exists():
            root = str(directory.expanduser().resolve())
            reloaded = project_store.load()
            generation = _m2_generation_view(
                generation_id=state.generation_id,
                updated_at=state.payload["updated_at"],
                billing=_m3_charges_from_project(reloaded),
                manifest_digest=reloaded.payload["production_manifest_digest"],
            )
            emit(
                _result(
                    state=state,
                    generation=generation,
                    project=reloaded.to_dict(),
                    workflow=_status_workflow(state, generation, root, project_store),
                ),
                json_output=json_output,
                message="OrchestrationPlan 已存在；未重复扣费。",
            )
            return
        # M1 must be persisted before any M3 request. A photo project reaches M3
        # only after M2, whose persisted plan advances the project to
        # presenter_plan_ready — so both statuses are valid entry points
        # (mirrors the M2 gate but accepts the post-M2 state).
        if project.payload["status"] not in {"manifest_ready", "presenter_plan_ready"}:
            raise LectureCastError(
                code="manifest_incompatible",
                message="M1 Manifest 尚未落盘，无法请求 M3 OrchestrationPlan。",
                next_action="先运行 director status 保存已签名的 Manifest。",
            )
        # Photo path precondition (gate ③): M2 must have been created + charged.
        # none+own_voice path has no M2 row and skips this. Refuse before any
        # network call — never send an M3 create for a photo project whose M2 is
        # not settled.
        if _d13_brief_avatar(project_store) == "photo" and project.payload.get(
            "presenter_plan_digest"
        ) is None:
            raise LectureCastError(
                code="m3_not_ready",
                message="photo 项目需先完成 M2 PresenterPlan（已扣费）才能请求 M3 OrchestrationPlan。",
                next_action="先运行 director generation-presenter-plan --yes。",
            )
        # Capabilities: reuse the stored snapshot (with the B1 stale guard) or
        # re-capture, mirroring `generate`. Only the v1.1 capture path is valid
        # for M3 (the M3 gate validates ClientCapabilitiesV1_1).
        adapter_kind = str(state.payload["adapter_kind"])
        adapter_version = str(state.payload["adapter_version"])
        capabilities = _stored_capabilities(
            project_store,
            adapter_kind=adapter_kind,
            adapter_version=adapter_version,
            protocol_version=state.protocol_version,
        )
        if capabilities is not None and state.protocol_version == "1.1":
            if not _stored_heygen_still_live(capabilities, directory):
                capabilities = None
        if capabilities is None:
            capabilities = capture_capabilities_v1_1(
                adapter_kind=adapter_kind,
                adapter_version=adapter_version,
                project_root=directory,
                repo_root=Path(__file__).resolve().parents[3],
                adapter_probe=default_heygen_adapter_probe,
                journal_probe=lambda: default_heygen_journal_probe(directory),
            )
            project_store.save_capabilities(
                capabilities, expected_revision=project.revision
            )

        result = _make_client(state.payload["server_url"]).create_orchestration_plan(
            state.generation_id,
            capabilities=capabilities.model_dump(),
            protocol_version=state.protocol_version,
        )
        plan = result["orchestration_plan"]
        billing = result.get("billing") or []
        # Derive the billing snapshot from the server-sent charges (mirror of the
        # server's aggregate_billing_state) so the v1.2/v1.3 state is consistent.
        billing_state, resume_available = derive_billing_state(billing)
        project = project_store.save_orchestration_plan(
            plan, expected_revision=project.revision
        )
        generation = _m2_generation_view(
            generation_id=state.generation_id,
            updated_at=plan["created_at"],
            billing=billing,
            manifest_digest=project.payload["production_manifest_digest"],
        )
        generation["billing_state"] = billing_state
        generation["resume_available"] = resume_available
        generation["recovery_catalog"] = result.get("recovery_catalog")
        state = state_store.update(state, generation=generation)
        root = str(directory.expanduser().resolve())
        emit(
            _result(
                state=state,
                generation=result,
                project=project.to_dict(),
                workflow=_status_workflow(state, generation, root, project_store),
            ),
            json_output=json_output,
            message=f"OrchestrationPlan 已保存：{plan['orchestration_plan_id']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


def _m2_generation_view(
    *, generation_id: str, updated_at: str, billing: list[dict[str, Any]],
    manifest_digest: str | None,
) -> dict[str, Any]:
    """Build the ready generation view used for the post-M2 status projection
    and the idempotent re-run. The manifest milestone is always charged (M1 was
    released before M2); the presenter_plan milestone is charged iff the plan was
    persisted (create succeeded). ``billing`` is the server-sent charge list on
    the create path, or a synthetic pair derived from persisted project digests
    on the idempotent re-run path."""
    billing_state, resume_available = derive_billing_state(billing)
    return {
        "generation_id": generation_id,
        "status": "ready",
        "updated_at": updated_at,
        "billing_state": billing_state,
        "resume_available": resume_available,
        "milestone_charges": billing,
        "manifest_digest": manifest_digest,
    }


def _m2_charges_from_project(project) -> list[dict[str, Any]]:
    """Synthesize the public charge projection from persisted project digests for
    the idempotent re-run (the create response is no longer available). M1 and M2
    are charged by construction: manifest_ready was required to enter M2, and the
    persisted plan proves the M2 create succeeded. When the project already
    reached M3 (orchestration_plan_digest present), the orchestration charge is
    appended so the post-M2 status projection never re-offers a completed M3
    (mirrors `_m3_charges_from_project`)."""
    charges = [
        {"milestone": "manifest", "artifact_type": "manifest", "status": "charged",
         "artifact_digest": project.payload["production_manifest_digest"], "cost": 10,
         "deducted_credits": 10, "last_error_code": None, "completed_at": None},
        {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "status": "charged",
         "artifact_digest": project.payload["presenter_plan_digest"], "cost": 10,
         "deducted_credits": 10, "last_error_code": None, "completed_at": None},
    ]
    if project.payload.get("orchestration_plan_digest") is not None:
        charges.append(
            {"milestone": "orchestration", "artifact_type": "orchestration_plan", "status": "charged",
             "artifact_digest": project.payload["orchestration_plan_digest"], "cost": 10,
             "deducted_credits": 10, "last_error_code": None, "completed_at": None},
        )
    return charges


def _m3_charges_from_project(project) -> list[dict[str, Any]]:
    """Synthesize the public charge projection from persisted project digests for
    the M3 idempotent re-run. M1 + M2 (when present) + M3 are all charged by
    construction: manifest_ready was required to enter M3, and the persisted plan
    proves the M3 create succeeded. The orchestration charge is always present;
    the presenter_plan charge is included only when an M2 plan exists (none +
    own_voice path has no M2 row)."""
    charges = [
        {"milestone": "manifest", "artifact_type": "manifest", "status": "charged",
         "artifact_digest": project.payload["production_manifest_digest"], "cost": 10,
         "deducted_credits": 10, "last_error_code": None, "completed_at": None},
    ]
    if project.payload.get("presenter_plan_digest") is not None:
        charges.append(
            {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "status": "charged",
             "artifact_digest": project.payload["presenter_plan_digest"], "cost": 10,
             "deducted_credits": 10, "last_error_code": None, "completed_at": None},
        )
    charges.append(
        {"milestone": "orchestration", "artifact_type": "orchestration_plan", "status": "charged",
         "artifact_digest": project.payload["orchestration_plan_digest"], "cost": 10,
         "deducted_credits": 10, "last_error_code": None, "completed_at": None},
    )
    return charges


def _project_in_m2_context(directory: Path) -> bool:
    """Whether the local project has entered a plan phase — i.e. a presenter plan
    (M2) or orchestration plan (M3) was persisted (the plan credit was billed).
    Used to suppress the M1 insufficient_credits recovery directive on the
    resume error path (§2.5: M2/M3 阶段的额度不足不该给 M1 话术).

    The signal is presenter-plan.json / orchestration-plan.json existence, not
    brief avatar: reaching M2/M3 requires the manifest charge to have succeeded,
    so a resume-402 after a plan was persisted can only be about a post-M1
    charge. A photo-avatar project still in M1 (no plan yet) must keep M1 话术 —
    it is correct for its manifest charge."""
    try:
        store = ProjectStore(directory)
        return store.presenter_plan_path.exists() or store.orchestration_plan_path.exists()
    except Exception:
        # Never let the M2-context check break the error path — fail closed to
        # the conservative choice (treat as M2 so M1 话术 is suppressed).
        return True


def _recovery_workflow(
    error: LectureCastError,
    catalog: dict[str, Any] | None,
    root: str,
    *,
    keyring: Any | None = None,
    m2_context: bool = False,
) -> dict[str, Any] | None:
    """Present a recovery directive for a failure_kind if the catalog has one
    (tech spec §7.3/§7.4, Host Conformance Contract). Returns None when the
    catalog is missing / unverified / has no matching directive — the caller
    falls back to _resume_error_workflow / generic fail.

    The catalog is verified HERE (fail-closed, §3 invariant 2): an unverified
    catalog never drives a directive. `keyring` is injectable for tests and
    mirrors verify_recovery_catalog_signature's contract.

    `m2_context` (m2-6 §2.5): once a plan phase is entered (M2 presenter_plan or
    M3 orchestration), an insufficient_credits resume-402 must NOT present the M1
    base-catalog directive (m1_insufficient_credits — that 话术 belongs to the M1
    phase). The provider catalog delivered with the M2 create response has no
    m1_insufficient_credits key, so the lookup would naturally return None —
    BUT a persisted BASE catalog (session/generation response, or the M3 create
    response) does carry the m1 directive. Suppressing the mapping here closes
    that hole so M2/M3 always fall through to _resume_error_workflow's generic
    credit_top_up_required."""
    if catalog is None:
        return None

    try:
        _verify_catalog_signature(catalog, keyring=keyring)
    except LectureCastError:
        return None  # unverified → never present directive 话术

    from ..recovery import failure_kind_for_error, recover_from_failure

    if m2_context and error.code == "insufficient_credits":
        # M2 phase insufficient_credits is not the M1 directive. Return None so
        # the caller falls through to _resume_error_workflow (credit_top_up_required).
        return None
    # Deterministic error→failure_kind: the explicit server-code mapping first
    # (insufficient_credits → m1_insufficient_credits), then a catalog-driven
    # pass-through — if the error code is itself a directive key in the verified
    # catalog (local adapter failures like local_renderer_missing / heygen_*),
    # look it up directly. Non-matching codes resolve to None and fall through
    # (never hard-code a new failure_kind, §3 invariant 5 / §7.5).
    failure_kind = failure_kind_for_error(error) or error.code
    directive = recover_from_failure(failure_kind, catalog)
    if directive is None:
        return None
    is_main_blocker = directive["is_main_blocker"]
    if is_main_blocker:
        phase = "main_blocker_recovery_required"
    else:
        phase = "recovery_directive_required"
    return {
        "phase": phase,
        "policy": "execute_only_returned_next_action",
        "next_action": {
            "id": "director.recovery.decide",
            "kind": "host_choice",
            "question_id": f"recovery_{directive['failure_kind']}",
            "question_label": directive["user_message"],
            "options": [
                {
                    "id": opt["option_id"],
                    "label": opt["label"] + ("（推荐）" if opt["recommended"] else ""),
                }
                for opt in directive["options"]
            ],
            "argv_template": [
                "lecturecast", "director", "recovery", "decide",
                root, "--choice", "<option_id>", "--json",
            ],
            "mutates": True,
            "requires_user_approval": True,
            "steer_back_line": directive["steer_back_line"],
            "do_not": directive.get("do_not") or [],
        },
    }


def _resume_error_workflow(error: LectureCastError, root: str) -> dict[str, Any] | None:
    """Build a command-specific workflow for generation-resume errors.
    Returns None if the error has no structured workflow (use generic fail())."""
    code = error.code
    http_status = error.http_status
    # Malformed v1.1 envelope → fail-closed, no structured workflow.
    if code == "manifest_incompatible":
        return None
    if code == "insufficient_credits" and http_status == 402:
        return {
            "phase": "credit_top_up_required",
            "policy": "execute_only_returned_next_action",
            "next_action": _command_action(
                "director.generation.resume",
                ["lecturecast", "director", "generation-resume", root, "--json"],
                approval=True,
            ),
        }
    if code == "generation_in_progress" and http_status == 409:
        return {
            "phase": "billing_refresh_required",
            "policy": "execute_only_returned_next_action",
            "next_action": _command_action(
                "director.status",
                ["lecturecast", "director", "status", root, "--json"],
            ),
        }
    if http_status is not None and http_status == 409:
        return {
            "phase": "generation_blocked",
            "policy": "execute_only_returned_next_action",
            "next_action": {"id": "workflow.stop", "kind": "stop", "mutates": False},
        }
    if http_status is not None and http_status == 404:
        return {
            "phase": "generation_unavailable",
            "policy": "execute_only_returned_next_action",
            "next_action": {"id": "workflow.stop", "kind": "stop", "mutates": False},
        }
    if http_status is not None and http_status >= 500:
        if error.retryable:
            return {
                "phase": "generation_recovery_required",
                "policy": "execute_only_returned_next_action",
                "next_action": _command_action(
                    "director.status",
                    ["lecturecast", "director", "status", root, "--json"],
                ),
            }
        return {
            "phase": "generation_blocked",
            "policy": "execute_only_returned_next_action",
            "next_action": {"id": "workflow.stop", "kind": "stop", "mutates": False},
        }
    return None  # generic fail()


@app.command("delete")
def delete(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Delete retained cloud content while preserving the local project."""
    try:
        store = DirectorStateStore(directory)
        state = store.load()
        require_project_host_workflow(
            directory, expected_adapter=str(state.payload["adapter_kind"])
        )
        result = _make_client(state.payload["server_url"]).delete_session(
            state.session_id
        )
        state = store.update(
            state,
            session_status="deleted",
            updated_at=str(result["content_deleted_at"]),
        )
        emit(
            {"director": state.to_dict(), "deletion": result},
            json_output=json_output,
            message="云端保留内容已删除；本地项目未删除。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("handoff")
def handoff(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Build a credential-free payload for a fresh agent task."""
    try:
        project = ProjectStore(directory).load()
        project_path = str(Path(directory).expanduser().resolve())
        try:
            director = DirectorStateStore(directory).load().to_dict()
        except LectureCastError as error:
            if error.code != "session_not_found":
                raise
            director = None
        payload = {
            "schema_version": "1.0",
            "project_path": project_path,
            "project_id": project.payload["project_id"],
            "resume_argv": [
                "lecturecast",
                "project",
                "resume",
                project_path,
                "--adapter",
                "<current-host>",
                "--host-contract",
                HOST_WORKFLOW_CONTRACT_VERSION,
                "--json",
            ],
            "director_resume_argv_by_adapter": {
                adapter: [
                    "lecturecast",
                    "director",
                    "resume",
                    project_path,
                    "--adapter",
                    adapter,
                    "--host-contract",
                    HOST_WORKFLOW_CONTRACT_VERSION,
                    "--json",
                ]
                for adapter in sorted(
                    value for value in DIRECTOR_ADAPTER_KINDS if value != "text"
                )
            },
            "prompt": (
                "请读取 LectureCast Skill，并从这个本地项目继续："
                f"{project_path}。先运行 project resume；如存在 Director 状态，"
                "再运行当前宿主对应的 director resume 命令，然后运行 director next/status。"
            ),
            "director": director,
        }
        emit(
            payload,
            json_output=json_output,
            message=payload["prompt"],
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)
