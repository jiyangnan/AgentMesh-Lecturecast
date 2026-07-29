"""HeyGen third-party transfer consent — disclosure + identity models (§5.5e2a).

Pure, side-effect-free domain models (frozen dataclasses with ``__post_init__``
validation, matching the codebase's dataclass style; no pydantic, which isn't a
dependency). The ConsentService that persists decisions arrives in §5.5e2b;
withdraw + the submit consent guard arrive in §5.5e2c. Nothing here touches the
network, the filesystem, the shared Core, or the DB.

Three privacy/funding invariants live here:

1. Operation identity is *derived* deterministically from immutable inputs — the
   caller cannot forge ``operation_id`` / ``idempotency_key`` / ``heygen_title``.
   Timestamps, remote IDs, retry counts and API keys never enter identity.

2. The disclosure proves exactly what leaves the machine for HeyGen. HeyGen
   charges the user's own BYO account; that external cost is carried by
   ``provider_cost_disclosure`` and is kept strictly separate from AgentMesh
   milestone credits — a credit amount is never written into that field.

3. A receipt digest captures one concrete decision for one concrete request, so
   ``decision_at`` / ``operation_id`` / ``request_digest`` / ``decision`` all
   participate in the digest (a replay returns the original digest unchanged).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

# --- canonical serialization + digests ---------------------------------


def canonical_json(obj: dict) -> str:
    """Deterministic JSON: sorted keys, minimal separators, stable unicode,
    NFC-normalized strings (avoids macOS NFD filename digest drift)."""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize(obj):
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def sha256_hex(obj: dict) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_digest(obj: dict) -> str:
    return "sha256:" + sha256_hex(obj)


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def is_digest(value: str) -> bool:
    return bool(_DIGEST_RE.fullmatch(value or ""))


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


# --- disclosed assets + data categories --------------------------------

# Closed vocabularies ---------------------------------------------------

# v1: only portrait-photo + synthetic narration leave for HeyGen. Voice cloning
# happens locally at F5, so HeyGen gets no voice-biometric-template category.
_DATA_CATEGORIES_BY_ASSET: dict[str, tuple[str, ...]] = {
    "portrait_photo": ("portrait_image", "facial_biometric_template"),
    "synthetic_narration_audio": ("synthetic_narration_audio",),
}
ASSET_KINDS = frozenset(_DATA_CATEGORIES_BY_ASSET)
ALL_DATA_CATEGORIES = frozenset(c for cats in _DATA_CATEGORIES_BY_ASSET.values() for c in cats)

# operation_kind ↔ endpoint is closed and consistent. v1 has only the presenter
# video call. Endpoint must carry no query or fragment.
_OPERATION_KIND_ENDPOINT: dict[str, str] = {
    "video": "/v3/videos",
}
OPERATION_KINDS = frozenset(_OPERATION_KIND_ENDPOINT)
ENDPOINTS = frozenset(_OPERATION_KIND_ENDPOINT.values())

# operation_kinds that execute against a signed orchestration plan and so must
# bind its digest (not just the request).
_KINDS_REQUIRING_ORCHESTRATION = frozenset({"video"})

# v1: a single local BYO environment. The caller cannot stuff an API key or
# account name here — only this closed identifier is accepted. Multi-account is
# a future expansion, resolved from a trusted profile source, never a
# caller-supplied raw string.
CREDENTIAL_PROFILE_IDS = frozenset({"heygen_env_default"})

# Basename only: strip path separators and control chars. The disclosure shows
# users a safe filename; the real path never leaves the machine.
_FILENAME_BAD = re.compile(r"[\x00-\x1f]|[/\\]")
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]+$")


@dataclass(frozen=True)
class DisclosedAsset:
    asset_kind: str
    display_filename: str
    asset_digest: str

    def __post_init__(self) -> None:
        if self.asset_kind not in ASSET_KINDS:
            raise ValueError(f"unknown asset_kind: {self.asset_kind!r}")
        name = _nfc(self.display_filename).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        name = _FILENAME_BAD.sub("", name).strip()
        if not name:
            raise ValueError("display_filename must be a non-empty basename")
        object.__setattr__(self, "display_filename", name)
        if not is_digest(self.asset_digest):
            raise ValueError("asset_digest must be sha256:<64 hex>")


@dataclass(frozen=True)
class ThirdPartyTransferDisclosure:
    """What the user is told before a HeyGen transfer, frozen at decision time."""

    provider: str
    operation_kind: str
    disclosure_version: str
    disclosed_assets: tuple[DisclosedAsset, ...]
    data_categories: tuple[str, ...]
    # HeyGen BYO cost independence — NOT AgentMesh milestone credits.
    provider_cost_disclosure: str
    agentmesh_non_processor_disclosure: str

    def __post_init__(self) -> None:
        if self.provider != "heygen":
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.disclosure_version != "heygen-transfer-2026-07-27":
            raise ValueError(f"unknown disclosure_version: {self.disclosure_version!r}")
        if self.operation_kind not in OPERATION_KINDS:
            raise ValueError(f"unknown operation_kind: {self.operation_kind!r}")
        # Freeze mutable inputs to tuples so the digest cannot change post-construction.
        object.__setattr__(self, "disclosed_assets", tuple(self.disclosed_assets))
        object.__setattr__(self, "data_categories", tuple(_nfc(c) for c in self.data_categories))
        object.__setattr__(self, "operation_kind", _nfc(self.operation_kind))
        object.__setattr__(self, "provider_cost_disclosure", _nfc(self.provider_cost_disclosure))
        object.__setattr__(
            self, "agentmesh_non_processor_disclosure", _nfc(self.agentmesh_non_processor_disclosure)
        )
        if not self.disclosed_assets:
            raise ValueError("disclosed_assets must not be empty")
        keys = [(a.asset_kind, a.display_filename, a.asset_digest) for a in self.disclosed_assets]
        if len(set(keys)) != len(keys):
            raise ValueError("disclosed_assets must be unique")
        # Duplicate categories would pass a set-equality check but yield a
        # different receipt digest — reject explicitly.
        if len(self.data_categories) != len(set(self.data_categories)):
            raise ValueError("data_categories must be unique")
        # Cross-field: categories are fully determined by the disclosed assets.
        allowed = set()
        for asset in self.disclosed_assets:
            allowed.update(_DATA_CATEGORIES_BY_ASSET[asset.asset_kind])
        if set(self.data_categories) != allowed:
            raise ValueError(
                f"data_categories {sorted(set(self.data_categories))} must equal "
                f"{sorted(allowed)} for the disclosed assets"
            )
        if not self.provider_cost_disclosure.strip():
            raise ValueError("provider_cost_disclosure is required")
        if not self.agentmesh_non_processor_disclosure.strip():
            raise ValueError("agentmesh_non_processor_disclosure is required")

    def _sorted_assets(self) -> list[dict]:
        return [
            {"kind": a.asset_kind, "filename": a.display_filename, "digest": a.asset_digest}
            for a in sorted(
                self.disclosed_assets,
                key=lambda x: (x.asset_kind, x.display_filename, x.asset_digest),
            )
        ]

    def canonical_payload(
        self,
        *,
        operation_id: str,
        generation_id: str,
        request_digest: str,
        creative_brief_digest: str,
        decision: str,
        decision_at: str,
    ) -> dict:
        """The exact object a receipt digest commits to. ``withdrawn_at`` is
        intentionally excluded — withdrawal is a lifecycle state on the original
        grant receipt, not part of the decision being proven."""
        if decision not in ("granted", "declined"):
            raise ValueError(f"unknown decision: {decision!r}")
        if not operation_id.strip():
            raise ValueError("operation_id is required")
        if not generation_id.strip():
            raise ValueError("generation_id is required")
        if not is_digest(request_digest):
            raise ValueError("request_digest must be sha256:<64 hex>")
        if not is_digest(creative_brief_digest):
            raise ValueError("creative_brief_digest must be sha256:<64 hex>")
        canonical_at = _canonical_decision_at(decision_at)
        return {
            "namespace": "lecturecast.heygen.consent-receipt.v1",
            "operation_id": operation_id,
            "generation_id": generation_id,
            "request_digest": request_digest,
            "creative_brief_digest": creative_brief_digest,
            "provider": self.provider,
            "operation_kind": self.operation_kind,
            "disclosure_version": self.disclosure_version,
            "disclosed_assets": self._sorted_assets(),
            "data_categories": sorted(self.data_categories),
            "provider_cost_disclosure": self.provider_cost_disclosure,
            "agentmesh_non_processor_disclosure": self.agentmesh_non_processor_disclosure,
            "decision": decision,
            "decision_at": canonical_at,
        }


def _canonical_decision_at(value: str) -> str:
    """Parse an ISO-8601 timestamp, require timezone-aware, normalize to UTC at
    second precision. Second precision is safe: the receipt also binds
    operation_id + request_digest, so two grants in the same second still differ."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("decision_at is required")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"decision_at is not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError("decision_at must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- operation identity ------------------------------------------------

OPERATION_IDENTITY_NAMESPACE = "lecturecast.heygen.operation.v1"


@dataclass(frozen=True)
class HeyGenOperationIdentity:
    """Immutable inputs that deterministically identify one HeyGen operation.
    Timestamps, remote IDs, retry counts and API keys are deliberately absent."""

    operation_kind: str
    generation_id: str
    manifest_digest: str
    request_digest: str
    credential_profile_id: str
    endpoint: str | None = None
    provider: str = "heygen"
    orchestration_plan_digest: str | None = None
    segment_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider != "heygen":
            raise ValueError(f"unknown provider: {self.provider!r}")
        kind = _nfc(self.operation_kind).strip()
        if kind not in OPERATION_KINDS:
            raise ValueError(f"unknown operation_kind: {self.operation_kind!r}")
        object.__setattr__(self, "operation_kind", kind)
        # endpoint: closed + consistent with kind, no query/fragment.
        expected_endpoint = _OPERATION_KIND_ENDPOINT[kind]
        endpoint = self.endpoint if self.endpoint is not None else expected_endpoint
        endpoint = _nfc(endpoint)
        if endpoint != expected_endpoint:
            raise ValueError(
                f"endpoint {endpoint!r} not valid for operation_kind {kind!r}"
            )
        parsed = urlparse(endpoint)
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain query or fragment")
        object.__setattr__(self, "endpoint", endpoint)
        for name in ("generation_id",):
            if not getattr(self, name) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(self, "generation_id", _nfc(self.generation_id).strip())
        # credential_profile_id is a closed internal identifier, never a caller-
        # supplied raw string (no API key / account name can leak into the DB).
        if self.credential_profile_id not in CREDENTIAL_PROFILE_IDS:
            raise ValueError(
                f"credential_profile_id must be one of {sorted(CREDENTIAL_PROFILE_IDS)}"
            )
        for name in ("manifest_digest", "request_digest"):
            if not is_digest(getattr(self, name)):
                raise ValueError(f"{name} must be sha256:<64 hex>")
        if self.orchestration_plan_digest is not None and not is_digest(self.orchestration_plan_digest):
            raise ValueError("orchestration_plan_digest must be sha256:<64 hex> when set")
        # Operations that execute against the signed M3 plan must bind it — the
        # request alone is not enough.
        if (
            self.operation_kind in _KINDS_REQUIRING_ORCHESTRATION
            and self.orchestration_plan_digest is None
        ):
            raise ValueError(
                f"orchestration_plan_digest is required for {self.operation_kind!r} operations"
            )
        if self.segment_id is not None:
            sid = _nfc(self.segment_id).strip()
            if not sid or not _STABLE_ID_RE.fullmatch(sid):
                raise ValueError("segment_id must be a stable id")
            object.__setattr__(self, "segment_id", sid)

    def identity_payload(self) -> dict:
        return {
            "namespace": OPERATION_IDENTITY_NAMESPACE,
            "provider": self.provider,
            "operation_kind": self.operation_kind,
            "endpoint": self.endpoint,
            "generation_id": self.generation_id,
            "manifest_digest": self.manifest_digest,
            "orchestration_plan_digest": self.orchestration_plan_digest,
            "segment_id": self.segment_id,
            "request_digest": self.request_digest,
            "credential_profile_id": self.credential_profile_id,
        }


@dataclass(frozen=True)
class PreparedOperation:
    """Output of :func:`prepare_operation` — a deterministic, DB-ready identity
    with no side effects. ``record_decision`` (e2b) persists it atomically with
    the user's consent decision."""

    operation_id: str
    idempotency_key: str
    heygen_title: str
    identity: HeyGenOperationIdentity


def prepare_operation(identity: HeyGenOperationIdentity) -> PreparedOperation:
    """Derive a deterministic operation_id / idempotency_key / heygen_title from
    an immutable identity. Pure: same identity ⇒ same outputs, always.

    operation_id uses 128 bits (32 hex) of the identity digest — enough that a
    truncation collision (same short operation_id, different full
    idempotency_key → permanent local conflict) is infeasible."""
    hexdigest = sha256_hex(identity.identity_payload())
    short = hexdigest[:32]
    operation_id = f"lc_hg_{short}"
    return PreparedOperation(
        operation_id=operation_id,
        idempotency_key=f"lc-hg-{hexdigest}",
        heygen_title=f"lecturecast:{operation_id}",
        identity=identity,
    )
