from __future__ import annotations

import os


API_KEY_ENV = "LECTURECAST_API_KEY"
CORE_URL_ENV = "LECTURECAST_CORE_URL"
DIRECTOR_URL_ENV = "LECTURECAST_DIRECTOR_URL"
DEFAULT_CORE_URL = "https://api.agentmesh360.com"
DEFAULT_DIRECTOR_URL = "https://api.lecturecast.agentmesh360.com"
ACCOUNT_URL = "https://agentmesh360.com/app/"
PRICING_URL = "https://agentmesh360.com/app/#pricing"
MANIFEST_CREDIT_COST = 10
KEYRING_SERVICE = "agentmesh-lecturecast"
KEYRING_USERNAME = "api-key"
HEYGEN_KEYRING_USERNAME = "heygen-api-key"
HEYGEN_API_SETTINGS_URL = "https://app.heygen.com/settings?from=&nav=API"
HEYGEN_API_HELP_URL = "https://help.heygen.com/en/articles/10060327-heygen-api-pricing-explained"
PROJECT_DIRECTORY = ".lecturecast"
PROJECT_SCHEMA_VERSION = "1.0"
CLIENT_VERSION = "0.6.2"

# Director protocol version negotiation (§5.5a). New sessions use the current
# production protocol. Existing sessions ignore this default because their
# protocol is pinned in director-state.json at creation time.
PROTOCOL_VERSION_ENV = "LECTURECAST_PROTOCOL_VERSION"
DEFAULT_PROTOCOL_VERSION = "1.1"
SUPPORTED_PROTOCOL_VERSIONS = ("1.0", "1.1")


def resolve_protocol_version(env: dict[str, str] | None = None) -> str:
    """Resolve the Director protocol version to negotiate. Reads the env var;
    defaults to the current production protocol. Rejects unsupported values
    rather than guessing."""
    sources = env if env is not None else os.environ
    raw = (sources.get(PROTOCOL_VERSION_ENV) or DEFAULT_PROTOCOL_VERSION).strip()
    if raw not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(
            f"unsupported protocol version {raw!r}; supported: "
            f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    return raw
