"""RecoveryDirectiveCatalog consumption helpers (tech spec §7.3/§7.4).

The client maps a local failure to a failure_kind deterministically, looks the
directive up in a pre-signed catalog, and presents its message/options/steer_back
as a structured workflow. The caller verifies the catalog signature BEFORE any
lookup (fail-closed: never act on an unverified catalog, §3 invariant 2).
"""
from __future__ import annotations

from typing import Any

from .errors import LectureCastError
from .protocol import RecoveryDirectiveCatalog


# Server error codes whose semantics unambiguously map to a base-catalog
# failure_kind (tech spec §7.3: "client adapter 本地把报错确定性映射成
# failure_kind"). Anything ambiguous/unknown maps to None — the catalog is the
# source of truth, never a hard-coded new kind (§3 invariant 5 / §7.5).
_ERROR_CODE_TO_FAILURE_KIND: dict[str, str] = {
    "insufficient_credits": "m1_insufficient_credits",
    "manifest_signature_invalid": "m1_manifest_signing_failed",
}


def failure_kind_for_error(error: LectureCastError) -> str | None:
    """Deterministically map a server/client error to a catalog failure_kind.

    Returns None when the code has no unambiguous catalog directive (the caller
    falls through to the existing error workflow / generic fail).
    """
    return _ERROR_CODE_TO_FAILURE_KIND.get(error.code)


def recover_from_failure(
    failure_kind: str,
    catalog: RecoveryDirectiveCatalog | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Look up a directive for a failure_kind in a RecoveryDirectiveCatalog.

    Returns None when the catalog is missing/None or has no directive for this
    failure_kind (catalog-driven client: never hard-code new failure_kinds,
    §3 invariant 5). The caller is responsible for signature verification
    BEFORE this lookup (fail-closed: never act on an unverified catalog)."""
    if catalog is None:
        return None
    document = (
        catalog
        if isinstance(catalog, RecoveryDirectiveCatalog)
        else RecoveryDirectiveCatalog.model_validate(catalog)
    )
    directives = document.payload.get("directives") or {}
    if not isinstance(directives, dict):
        return None
    directive = directives.get(failure_kind)
    if not isinstance(directive, dict):
        return None
    return {
        "failure_kind": directive["failure_kind"],
        "is_main_blocker": directive["is_main_blocker"],
        "user_message": directive["user_message"],
        "options": directive["options"],
        "steer_back_line": directive["steer_back_line"],
        "external_handoff": directive.get("external_handoff"),
        "do_not": directive.get("do_not"),
    }
