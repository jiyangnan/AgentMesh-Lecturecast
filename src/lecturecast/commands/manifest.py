from __future__ import annotations

import json
from pathlib import Path

import typer

from ..capabilities import capture_capabilities
from ..commercial import require_commercial_access
from ..errors import LectureCastError
from ..manifest import inspect_manifest, load_manifest, verify_manifest
from ..preflight import run_preflight
from ..protocol import ClientCapabilities
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


@app.command("preflight")
def preflight(
    manifest_path: Path,
    capabilities_path: Path | None = typer.Option(None, "--capabilities"),
    project_root: Path | None = typer.Option(None, "--project-root"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify that this client can execute a signed Manifest without rendering."""
    try:
        require_commercial_access()
        capabilities = (
            _load_capabilities(capabilities_path)
            if capabilities_path is not None
            else capture_capabilities(
                project_root=project_root or Path.cwd(),
                repo_root=Path(__file__).resolve().parents[3],
            )
        )
        root = project_root
        if root is None and manifest_path.parent.name == ".lecturecast":
            root = manifest_path.parent.parent
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
