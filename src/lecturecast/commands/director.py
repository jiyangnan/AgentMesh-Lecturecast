from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import typer

from ..capabilities import capture_capabilities
from ..director import (
    DIRECTOR_ADAPTER_KINDS,
    DirectorClient,
    DirectorState,
    DirectorStateStore,
    load_source_file,
    normalize_adapter_identity,
    resolve_server_url,
)
from ..errors import LectureCastError
from ..project import ProjectStore
from ..protocol import ClientCapabilities, canonical_digest
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)
brief_app = typer.Typer(no_args_is_help=True, add_completion=False)
app.add_typer(brief_app, name="brief", help="Show or confirm the server-backed Creative Brief.")


def _make_client(server_url: str) -> DirectorClient:
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
        return normalize_adapter_identity(kind, version)
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
) -> dict[str, Any]:
    payload: dict[str, Any] = {"director": state.to_dict()}
    if session is not None:
        payload["session"] = session
        payload["decision_card_set"] = session.get("decision_card_set")
    if generation is not None:
        payload["generation"] = generation
    if project is not None:
        payload["project"] = project
    return payload


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
    adapter: str = typer.Option("text", "--adapter"),
    adapter_version: str = typer.Option("1.0.0", "--adapter-version"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create a cloud Director Session bound to an existing local project."""
    try:
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
        session = _make_client(server_url).create_session(load_source_file(source))
        state = state_store.create(
            server_url=server_url,
            session=session,
            adapter_kind=adapter,
            adapter_version=adapter_version,
        )
        emit(
            _result(state=state, session=session),
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
        session = _make_client(state.payload["server_url"]).get_session(state.session_id)
        state = store.update(state, session=session)
        emit(
            _result(state=state, session=session),
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
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Rebind an existing Director project to the current agent without network use."""
    try:
        project = ProjectStore(directory).load()
        adapter, adapter_version = _adapter(adapter, adapter_version)
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
            "credit_deducted": False,
            "capabilities_policy": "refresh_before_generate_on_adapter_mismatch",
        }
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
        session = _make_client(state.payload["server_url"]).answer(
            state.session_id,
            question_id=question_id,
            option_id=option_id,
            catalog_version=catalog_version or str(state.payload["catalog_version"]),
            custom_text=_read_custom_text(custom_text_file),
        )
        state = store.update(state, session=session)
        emit(
            _result(state=state, session=session),
            json_output=json_output,
            message=f"已提交 {question_id}={option_id}。",
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
        session = _make_client(state.payload["server_url"]).get_session(state.session_id)
        state = store.update(state, session=session)
        if session.get("brief") is None:
            raise LectureCastError(
                code="brief_not_ready",
                message="Creative Brief 尚未形成。",
                next_action="继续处理 decision_card_set 后重试。",
            )
        emit(
            _result(state=state, session=session),
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
        client = _make_client(state.payload["server_url"])
        if expected_brief_version is None:
            current = client.get_session(state.session_id)
            expected_brief_version = int(current["brief_version"])
        session = client.confirm_brief(
            state.session_id,
            expected_brief_version=expected_brief_version,
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
            _result(state=state, session=session, project=project.to_dict()),
            json_output=json_output,
            message="Creative Brief 已确认并保存；本步骤没有扣 credit。",
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
) -> ClientCapabilities | None:
    project = store.load()
    if project.payload["capability_digest"] is None:
        return None
    try:
        document = ClientCapabilities.model_validate_json(
            store.capabilities_path.read_text(encoding="utf-8")
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
    return document


@app.command("generate")
def generate(
    directory: Path = typer.Argument(Path(".")),
    generation_id: str | None = typer.Option(None, "--generation-id"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Reserve one stable generation ID, then request the paid Manifest once."""
    try:
        state_store = DirectorStateStore(directory)
        state = state_store.load()
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
        )
        if capabilities is None:
            capabilities = capture_capabilities(
                adapter_kind=adapter_kind,
                adapter_version=adapter_version,
                repo_root=Path(__file__).resolve().parents[3],
            )
            project = project_store.load()
            project_store.save_capabilities(
                capabilities, expected_revision=project.revision
            )

        generation = _make_client(state.payload["server_url"]).create_generation(
            state.session_id,
            generation_id=selected_id,
            expected_brief_version=int(state.payload["brief_version"]),
            capabilities=capabilities.model_dump(),
        )
        state = state_store.update(state, generation=generation)
        emit(
            _result(state=state, generation=generation),
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
        if state.generation_id is None:
            raise LectureCastError(
                code="session_not_found",
                message="本地项目还没有 Manifest generation。",
                next_action="先运行 director generate。",
            )
        generation = _make_client(state.payload["server_url"]).get_generation(
            state.generation_id
        )
        state = state_store.update(state, generation=generation)
        project_store = ProjectStore(directory)
        project = project_store.load()
        if generation["status"] == "ready":
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
        emit(
            _result(state=state, generation=generation, project=project.to_dict()),
            json_output=json_output,
            message=f"Generation 状态：{generation['status']}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        _unexpected(exc, json_output=json_output)


@app.command("delete")
def delete(
    directory: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Delete retained cloud content while preserving the local project."""
    try:
        store = DirectorStateStore(directory)
        state = store.load()
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
                    "--json",
                ]
                for adapter in sorted(DIRECTOR_ADAPTER_KINDS)
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
