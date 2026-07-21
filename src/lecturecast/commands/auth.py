from __future__ import annotations

import typer

from ..auth import auth_status as local_auth_status
from ..auth import delete_stored_api_key, get_api_key, save_api_key
from ..commercial import CommercialClient, missing_commercial_access
from ..config import ACCOUNT_URL, API_KEY_ENV, PRICING_URL
from ..errors import LectureCastError
from .output import emit, fail


app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("login")
def login(json_output: bool = typer.Option(False, "--json")) -> None:
    """Validate and store a universal AgentMesh360 API Key."""
    try:
        api_key = typer.prompt(
            "AgentMesh360 API Key", hide_input=True, confirmation_prompt=False
        )
        access = CommercialClient(api_key=api_key).access()
        status = save_api_key(api_key)
        emit(
            {
                **status.to_dict(),
                "valid": True,
                "cloud_access": access.to_dict(),
                "next_suggested": (
                    "lecturecast onboard --json" if access.usable else PRICING_URL
                ),
            },
            json_output=json_output,
            message=(
                "AgentMesh360 API Key 已验证并安全保存。"
                if access.usable
                else "API Key 已验证并保存；请先开通付费权限或补充 credits。"
            ),
        )
    except LectureCastError as error:
        fail(error, json_output=json_output)


@app.command("status")
def status(json_output: bool = typer.Option(False, "--json")) -> None:
    """Verify the bound account without revealing the credential."""
    try:
        value = local_auth_status()
        key = get_api_key()
        access = CommercialClient(api_key=key).access() if key else missing_commercial_access()
        emit(
            {
                **value.to_dict(),
                "valid": access.valid,
                "cloud_access": access.to_dict(),
                "next_suggested": (
                    "lecturecast onboard --json"
                    if access.usable
                    else "lecturecast auth login"
                    if key is None
                    else PRICING_URL
                ),
            },
            json_output=json_output,
            message=(
                "AgentMesh360 商业账户已绑定。"
                if access.usable
                else f"尚未获得 LectureCast 商业访问权限。账户中心：{ACCOUNT_URL}"
            ),
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
