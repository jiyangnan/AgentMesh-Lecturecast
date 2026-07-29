"""HeyGen third-party transfer consent — disclosure + identity models (§5.5e2a).

Pure, side-effect-free domain models (frozen dataclasses with ``__post_init__``
validation, matching the codebase's dataclass style). The ConsentService that
persists decisions arrives in §5.5e2b; withdraw + the submit consent guard
arrive in §5.5e2c. Nothing here touches the network, the filesystem, the shared
Core, or the DB.

Three privacy/funding invariants live here:

1. Operation identity is *derived* deterministically from immutable inputs — the
   caller cannot forge ``operation_id`` / ``idempotency_key`` / ``heygen_title``.
   Timestamps, remote IDs, retry counts and API keys never enter identity.

2. The disclosure proves exactly what leaves the machine for HeyGen, and that
   AgentMesh360 neither proxies, records, hosts, nor pays HeyGen. HeyGen charges
   the user's own BYO account; that external cost is kept strictly separate from
   AgentMesh milestone credits and is never written into ``provider_cost_disclosure``.

3. A receipt digest captures one concrete decision for one concrete request, so
   ``consented_at`` / ``operation_id`` / ``request_digest`` / ``decision`` all
   participate in the digest (a replay returns the original digest unchanged).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

# --- canonical serialization + digests ---------------------------------


def canonical_json(obj: dict) -> str:
    """Deterministic JSON: sorted keys, minimal separators, stable unicode."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(obj: dict) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_digest(obj: dict) -> str:
    return "sha256:" + sha256_hex(obj)


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def is_digest(value: str) -> bool:
    return bool(_DIGEST_RE.fullmatch(value or ""))


# --- disclosed assets --------------------------------------------------

AssetKind = Literal["portrait_photo", "synthetic_narration_audio"]

# Basename only: strip path separators and control chars. The disclosure shows
# users a safe filename; the real path never leaves the machine.
_FILENAME_BAD = re.compile(r"[\x00-\x1f]|[/\\]")

_DATA_CATEGORIES = {
    "portrait_image",
    "voice_audio",
    "facial_biometric_template",
    "voice_biometric_template",
    "synthesized_speech_audio",
}


@dataclass(frozen=True)
class DisclosedAsset:
    asset_kind: str
    display_filename: str
    asset_digest: str

    def __post_init__(self) -> None:
        if self.asset_kind not in ("portrait_photo", "synthetic_narration_audio"):
            raise ValueError(f"unknown asset_kind: {self.asset_kind!r}")
        name = self.display_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        name = _FILENAME_BAD.sub("", name).strip()
        if not name:
            raise ValueError("display_filename must be a non-empty basename")
        object.__setattr__(self, "display_filename", name)
        if not is_digest(self.asset_digest):
            raise ValueError("asset_digest must be sha256:<64 hex>")


Provider = Literal["heygen"]
DisclosureVersion = Literal["heygen-transfer-2026-07-27"]
ConsentDecision = Literal["granted", "declined"]


@dataclass(frozen=True)
class ThirdPartyTransferDisclosure:
    """What the user is told before a HeyGen transfer, frozen at decision time."""

    provider: str
    operation_kind: str
    disclosure_version: str
    disclosed_assets: list[DisclosedAsset]
    data_categories: list[str]
    # HeyGen BYO cost independence — NOT AgentMesh milestone credits.
    provider_cost_disclosure: str
    agentmesh_non_processor_disclosure: str

    def __post_init__(self) -> None:
        if self.provider != "heygen":
            raise ValueError(f"unknown provider: {self.provider!r}")
        if self.disclosure_version != "heygen-transfer-2026-07-27":
            raise ValueError(f"unknown disclosure_version: {self.disclosure_version!r}")
        if not self.operation_kind.strip():
            raise ValueError("operation_kind is required")
        if not self.disclosed_assets:
            raise ValueError("disclosed_assets must not be empty")
        keys = [(a.asset_kind, a.display_filename, a.asset_digest) for a in self.disclosed_assets]
        if len(set(keys)) != len(keys):
            raise ValueError("disclosed_assets must be unique")
        if not self.data_categories:
            raise ValueError("data_categories must not be empty")
        if len(set(self.data_categories)) != len(self.data_categories):
            raise ValueError("data_categories must be unique")
        for category in self.data_categories:
            if category not in _DATA_CATEGORIES:
                raise ValueError(f"unknown data_category: {category!r}")
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
            "decision_at": decision_at,
        }


# --- operation identity ------------------------------------------------

OPERATION_IDENTITY_NAMESPACE = "lecturecast.heygen.operation.v1"


@dataclass(frozen=True)
class HeyGenOperationIdentity:
    """Immutable inputs that deterministically identify one HeyGen operation.
    Timestamps, remote IDs, retry counts and API keys are deliberately absent."""

    operation_kind: str
    endpoint: str
    generation_id: str
    manifest_digest: str
    request_digest: str
    credential_profile_id: str
    provider: str = "heygen"
    orchestration_plan_digest: str | None = None
    segment_id: str | None = None

    def __post_init__(self) -> None:
        if self.provider != "heygen":
            raise ValueError(f"unknown provider: {self.provider!r}")
        for name in ("operation_kind", "endpoint", "generation_id", "credential_profile_id"):
            if not getattr(self, name) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty")
        for name in ("manifest_digest", "request_digest"):
            if not is_digest(getattr(self, name)):
                raise ValueError(f"{name} must be sha256:<64 hex>")

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
    an immutable identity. Pure: same identity ⇒ same outputs, always."""
    hexdigest = sha256_hex(identity.identity_payload())
    short = hexdigest[:20]
    operation_id = f"lc_hg_{short}"
    return PreparedOperation(
        operation_id=operation_id,
        idempotency_key=f"lc-hg-{hexdigest}",
        heygen_title=f"lecturecast:{operation_id}",
        identity=identity,
    )
