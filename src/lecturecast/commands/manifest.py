from __future__ import annotations

import json
from pathlib import Path

import typer

from ..capabilities import capture_capabilities
from ..commercial import require_commercial_access
from ..errors import LectureCastError
from ..host_agent import require_project_host_workflow
from ..manifest import inspect_manifest, load_manifest, verify_manifest
from ..preflight import run_preflight
from ..project import ProjectStore
from ..protocol import ClientCapabilities
from ..timing import narration_review
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


def _load_capabilities(path: Path) -> ClientCapabilities:
    return ClientCapabilities.model_validate(json.loads(path.read_text(encoding="utf-8")))


@app.command("inspect")
def inspect(
    manifest_path: Path,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect a Manifest summary without executing it."""
    try:
        result = inspect_manifest(load_manifest(manifest_path))
        emit(result, json_output=json_output, message=json.dumps(result, ensure_ascii=False, indent=2))
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("verify")
def verify(
    manifest_path: Path,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify schema and Ed25519 signature."""
    try:
        result = verify_manifest(load_manifest(manifest_path)).to_dict()
        emit(result, json_output=json_output, message="Manifest 签名有效。")
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("review")
def review(
    project_root: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the complete signed script and timing review before local production."""
    try:
        store = ProjectStore(project_root)
        store.load()
        manifest = load_manifest(store.manifest_path)
        verification = verify_manifest(manifest).to_dict()
        result = {
            **narration_review(manifest),
            "signature": verification,
            "approval": store.manifest_approval_status(),
            "workflow": {
                "phase": "script_approval_required",
                "policy": "execute_only_returned_next_action",
                "next_action": {
                    "id": "manifest.approve",
                    "kind": "command",
                    "argv": [
                        "lecturecast",
                        "manifest",
                        "approve",
                        str(project_root.expanduser().resolve()),
                        "--confirm-reviewed-script",
                        "--json",
                    ],
                    "mutates": True,
                    "requires_user_approval": True,
                },
            },
        }
        emit(
            result,
            json_output=json_output,
            message=json.dumps(result, ensure_ascii=False, indent=2),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("approve")
def approve(
    project_root: Path = typer.Argument(Path(".")),
    confirm_reviewed_script: bool = typer.Option(False, "--confirm-reviewed-script"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Record explicit local approval of the exact signed script digest."""
    try:
        require_project_host_workflow(project_root)
        require_commercial_access()
        if not confirm_reviewed_script:
            raise LectureCastError(
                code="brief_not_ready",
                message="尚未确认已向用户展示完整脚本。",
                next_action="先运行 manifest review，向用户展示全文；明确通过后再带 --confirm-reviewed-script。",
            )
        store = ProjectStore(project_root)
        state = store.load()
        updated, approval = store.approve_manifest(expected_revision=state.revision)
        render_argv = [
            "bash",
            str(
                Path(__file__).resolve().parents[3]
                / "templates"
                / "shared"
                / "build_manifest_video.sh"
            ),
            str(project_root.expanduser().resolve()),
        ]
        emit(
            {
                "project": updated.to_dict(),
                "approval": approval,
                "workflow": {
                    "phase": "local_render_ready",
                    "policy": "execute_only_returned_next_action",
                    "next_action": {
                        "id": "render.local",
                        "kind": "command",
                        "argv": render_argv,
                        "mutates": True,
                        "requires_user_approval": False,
                    },
                },
            },
            json_output=json_output,
            message="完整脚本已按 Manifest digest 批准，可以进入本地配音与渲染。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("approval")
def approval(
    project_root: Path = typer.Argument(Path(".")),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fail closed unless the exact current signed script was explicitly approved."""
    try:
        require_project_host_workflow(project_root)
        require_commercial_access()
        status = ProjectStore(project_root).manifest_approval_status()
        if not status["approved"]:
            raise LectureCastError(
                code="brief_not_ready",
                message="当前签名 Manifest 的完整脚本尚未获得明确批准。",
                next_action="运行 lecturecast manifest review，展示全文并取得通过后再运行 manifest approve。",
            )
        emit(
            {
                **status,
                "workflow": {
                    "phase": "local_render_ready",
                    "policy": "execute_only_returned_next_action",
                    "next_action": {
                        "id": "render.local",
                        "kind": "command",
                        "argv": [
                            "bash",
                            str(
                                Path(__file__).resolve().parents[3]
                                / "templates"
                                / "shared"
                                / "build_manifest_video.sh"
                            ),
                            str(project_root.expanduser().resolve()),
                        ],
                        "mutates": True,
                        "requires_user_approval": False,
                    },
                },
            },
            json_output=json_output,
            message="Manifest 完整脚本批准记录有效。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("preflight")
def preflight(
    manifest_path: Path,
    capabilities_path: Path | None = typer.Option(None, "--capabilities"),
    project_root: Path | None = typer.Option(None, "--project-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify that this client can execute a signed Manifest without rendering."""
    try:
        root = project_root
        if root is None and manifest_path.parent.name == ".lecturecast":
            root = manifest_path.parent.parent
        if root is not None:
            require_project_host_workflow(root)
        require_commercial_access()
        capabilities = (
            _load_capabilities(capabilities_path)
            if capabilities_path is not None
            else capture_capabilities(
                project_root=project_root or Path.cwd(),
                repo_root=Path(__file__).resolve().parents[3],
            )
        )
        result = run_preflight(
            load_manifest(manifest_path), capabilities, project_root=root
        ).to_dict()
        emit(result, json_output=json_output, message="Manifest preflight 已通过。")
    except LectureCastError as error:
        fail(error, json_output=json_output)
    except Exception as exc:
        fail(
            LectureCastError(
                code="manifest_incompatible",
                message="Capabilities 文件无效。",
                next_action="请重新运行 lecturecast doctor --json 生成能力信息。",
                cause=type(exc).__name__,
            ),
            json_output=json_output,
        )
