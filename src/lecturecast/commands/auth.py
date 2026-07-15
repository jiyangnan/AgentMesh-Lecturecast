from __future__ import annotations

import typer

from ..auth import auth_status, delete_stored_api_key, save_api_key
from ..config import API_KEY_ENV
from ..errors import LectureCastError
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("login")
def login(json_output: bool = typer.Option(False, "--json")) -> None:
    """Store an API Key in the OS credential store using hidden input."""
    try:
        api_key = typer.prompt("LectureCast API Key", hide_input=True, confirmation_prompt=False)
        status = save_api_key(api_key)
        emit(
            status.to_dict(),
            json_output=json_output,
            message="API Key 已安全保存到系统凭证存储。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show credential availability without revealing the credential."""
    try:
        value = auth_status()
        emit(
            value.to_dict(),
            json_output=json_output,
            message=f"认证状态：{'已配置' if value.configured else '未配置'}；来源：{value.source or '-'}。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("logout")
def logout(json_output: bool = typer.Option(False, "--json")) -> None:
    """Delete the stored key; an environment override remains active."""
    try:
        delete_stored_api_key()
        emit(
            {"deleted": True, "environment_variable": API_KEY_ENV},
            json_output=json_output,
            message=f"系统凭证已删除。若设置了 {API_KEY_ENV}，环境变量仍然生效。",
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)

