"""Local config at ~/.lecturecast/config.toml — stores license key + endpoints."""
import os
import sys
from pathlib import Path

import tomli_w
from platformdirs import user_config_dir

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


DEFAULT_API = "https://api.lecturecast.agentmesh360.com"


def config_path() -> Path:
    # Use stable ~/.lecturecast for visibility; platformdirs is fallback if user disabled it
    home = Path(os.path.expanduser("~/.lecturecast"))
    home.mkdir(parents=True, exist_ok=True)
    return home / "config.toml"


def load() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def save(data: dict) -> None:
    with open(config_path(), "wb") as f:
        tomli_w.dump(data, f)


def get_api_base() -> str:
    return os.environ.get("LECTURECAST_API") or load().get("api_base") or DEFAULT_API


def get_token() -> str | None:
    return os.environ.get("LECTURECAST_TOKEN") or load().get("token")


def get_minimax_key() -> str | None:
    """BYOK MiniMax key, read from the user's env only.

    Intentionally NOT read from (or written to) config.toml — a third-party TTS
    key shouldn't be persisted by this CLI. Set it for the session to upgrade
    voiceover from the free Edge voice to MiniMax:  export MINIMAX_API_KEY=...
    """
    return os.environ.get("MINIMAX_API_KEY")
