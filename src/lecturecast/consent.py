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
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from lecturecast.heygen_journal import _chmod_secure, _utc_now, init_database

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


# ===========================================================================
# ConsentService — persists decisions atomically (§5.5e2b)
# ===========================================================================
# record_decision opens its own short-lived connection, wraps the operation +
# receipt + consent pointer in one BEGIN IMMEDIATE transaction, and tightens
# file permissions afterward. Nothing is held across calls. withdraw and the
# submit consent guard arrive in §5.5e2c.

# Fixed disclosure template for disclosure_version "heygen-transfer-2026-07-27".
# record_decision rejects any disclosure whose cost / non-processor text does
# not equal these — a caller cannot inject arbitrary wording ("x") into a
# receipt. The asset list is data (what is uploaded); the policy text is frozen.
CANONICAL_PROVIDER_COST_DISCLOSURE = (
    "HeyGen is a third-party service you access with your own HeyGen account. "
    "HeyGen may charge your own account for this transfer. AgentMesh360 does not "
    "pay, advance, or reimburse any HeyGen cost. AgentMesh milestone credits are "
    "separate and are never applied to HeyGen charges."
)
CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE = (
    "AgentMesh360 is a non-processor: it does not proxy, record, host, bill, or "
    "retain HeyGen media. Your portrait photo and the selected narration are sent "
    "to HeyGen; the F5 reference is never uploaded. You must hold the portrait "
    "and voice rights, and complete HeyGen's own consent and biometric flow. "
    "Provider terms, training, subprocessors, cross-border transfer, and "
    "deletion are managed in your HeyGen account."
)
_RECEIPT_NAMESPACE = "lecturecast.heygen.consent-receipt.v1"
_RUNTIME_DB = Path(".lecturecast") / "runtime" / "heygen-operations.db"


class ConsentError(RuntimeError):
    """Base for consent persistence errors."""


class ConsentConflictError(ConsentError):
    """An operation with this identity already exists with different immutable
    fields — the caller is trying to re-key an operation."""


class ConsentDisclosureDriftError(ConsentError):
    """The disclosure for an already-recorded decision changed, or the supplied
    text is not the trusted canonical template."""


class ConsentStateError(ConsentError):
    """The requested decision transition is not allowed from the current state."""


@dataclass(frozen=True)
class ConsentDecisionResult:
    operation_id: str
    receipt_digest: str
    decision: str          # "granted" | "declined"
    status: str            # receipt status after the call
    consented_at: str      # canonical UTC the decision was recorded
    idempotent: bool       # True if an identical decision was already on file


class ConsentService:
    """Persists HeyGen third-party-transfer consent decisions in the per-project
    journal. Construct with the project directory; each call is self-contained."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._db_path = self._project_dir / _RUNTIME_DB

    def record_decision(
        self,
        *,
        prepared: PreparedOperation,
        disclosure: ThirdPartyTransferDisclosure,
        decision: str,
        creative_brief_digest: str,
        decision_at: str,
    ) -> ConsentDecisionResult:
        if decision not in ("granted", "declined"):
            raise ValueError(f"unknown decision: {decision!r}")
        self._check_cross_object(prepared, disclosure)
        self._check_trusted_text(disclosure)
        payload = disclosure.canonical_payload(
            operation_id=prepared.operation_id,
            generation_id=prepared.identity.generation_id,
            request_digest=prepared.identity.request_digest,
            creative_brief_digest=creative_brief_digest,
            decision=decision,
            decision_at=decision_at,
        )
        desired_digest = sha256_digest(payload)
        content_no_time = {k: v for k, v in payload.items() if k != "decision_at"}
        consented_at = payload["decision_at"]
        assets_json = json.dumps(payload["disclosed_assets"])
        categories_json = json.dumps(payload["data_categories"])

        conn = init_database(self._project_dir)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._record_in_tx(
                    conn, prepared, disclosure, decision, desired_digest,
                    content_no_time, consented_at, assets_json, categories_json,
                    payload,
                )
                conn.execute("COMMIT")
            except Exception:
                _rollback(conn)
                raise
        finally:
            _chmod_secure(self._db_path)
            conn.close()
        return result

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _check_cross_object(prepared: PreparedOperation,
                            disclosure: ThirdPartyTransferDisclosure) -> None:
        if disclosure.provider != prepared.identity.provider:
            raise ConsentDisclosureDriftError(
                "disclosure.provider does not match identity.provider"
            )
        if disclosure.operation_kind != prepared.identity.operation_kind:
            raise ConsentDisclosureDriftError(
                "disclosure.operation_kind does not match identity.operation_kind"
            )

    @staticmethod
    def _check_trusted_text(disclosure: ThirdPartyTransferDisclosure) -> None:
        if disclosure.provider_cost_disclosure != CANONICAL_PROVIDER_COST_DISCLOSURE:
            raise ConsentDisclosureDriftError(
                "provider_cost_disclosure is not the canonical disclosure template"
            )
        if disclosure.agentmesh_non_processor_disclosure != CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE:
            raise ConsentDisclosureDriftError(
                "agentmesh_non_processor_disclosure is not the canonical disclosure template"
            )

    def _record_in_tx(self, conn, prepared, disclosure, decision, desired_digest,
                      content_no_time, consented_at, assets_json, categories_json,
                      payload) -> ConsentDecisionResult:
        now = _utc_now()
        existing_op = conn.execute(
            "SELECT * FROM heygen_operations WHERE operation_id = ? OR idempotency_key = ?",
            (prepared.operation_id, prepared.idempotency_key),
        ).fetchone()
        if existing_op is not None:
            self._verify_immutable(existing_op, prepared)

        existing_rc = conn.execute(
            "SELECT * FROM heygen_consent_receipts WHERE operation_id = ?",
            (prepared.operation_id,),
        ).fetchone()

        if existing_rc is not None:
            return self._apply_existing(
                conn, prepared, decision, desired_digest, content_no_time,
                consented_at, assets_json, categories_json, payload,
                existing_rc, now,
            )

        # Fresh decision: insert operation (if absent) + receipt.
        op_status = "submit_pending" if decision == "granted" else "cancelled"
        consent_ptr = desired_digest if decision == "granted" else None
        if existing_op is None:
            conn.execute(
                "INSERT INTO heygen_operations (operation_id, kind, endpoint, "
                "segment_id, generation_id, manifest_digest, "
                "orchestration_plan_digest, request_digest, idempotency_key, "
                "heygen_title, credential_profile_id, consent_receipt_digest, "
                "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    prepared.operation_id, prepared.identity.operation_kind,
                    prepared.identity.endpoint, prepared.identity.segment_id,
                    prepared.identity.generation_id, prepared.identity.manifest_digest,
                    prepared.identity.orchestration_plan_digest,
                    prepared.identity.request_digest, prepared.idempotency_key,
                    prepared.heygen_title, prepared.identity.credential_profile_id,
                    consent_ptr, op_status, now, now,
                ),
            )
        else:
            conn.execute(
                "UPDATE heygen_operations SET status = ?, consent_receipt_digest = ?, "
                "updated_at = ? WHERE operation_id = ?",
                (op_status, consent_ptr, now, prepared.operation_id),
            )
        self._insert_receipt(
            conn, desired_digest, prepared, payload, decision, consented_at,
            assets_json, categories_json, now,
        )
        return ConsentDecisionResult(
            operation_id=prepared.operation_id, receipt_digest=desired_digest,
            decision=decision, status=decision, consented_at=consented_at,
            idempotent=False,
        )

    def _apply_existing(self, conn, prepared, decision, desired_digest,
                        content_no_time, consented_at, assets_json,
                        categories_json, payload, existing_rc, now):
        existing_status = existing_rc["status"]
        if existing_status == decision:
            stored = self._stored_content_no_time(existing_rc)
            if canonical_json(stored) == canonical_json(content_no_time):
                # Idempotent replay — return the original, do not touch timestamps.
                return ConsentDecisionResult(
                    operation_id=prepared.operation_id,
                    receipt_digest=existing_rc["receipt_digest"],
                    decision=decision, status=existing_status,
                    consented_at=existing_rc["consented_at"], idempotent=True,
                )
            raise ConsentDisclosureDriftError(
                "disclosure changed for an already-recorded decision"
            )
        if existing_status == "withdrawn":
            raise ConsentStateError(
                "cannot record a decision on a withdrawn receipt; start a new operation"
            )
        if existing_status == "declined" and decision == "granted":
            self._require_pristine(conn, prepared.operation_id)
            self._update_receipt(
                conn, desired_digest, prepared, payload, "granted", consented_at,
                assets_json, categories_json, now,
            )
            conn.execute(
                "UPDATE heygen_operations SET status = 'submit_pending', "
                "consent_receipt_digest = ?, updated_at = ? WHERE operation_id = ?",
                (desired_digest, now, prepared.operation_id),
            )
            return ConsentDecisionResult(
                operation_id=prepared.operation_id, receipt_digest=desired_digest,
                decision="granted", status="granted", consented_at=consented_at,
                idempotent=False,
            )
        if existing_status == "granted" and decision == "declined":
            raise ConsentStateError(
                "a granted decision cannot be flipped to declined; withdraw it instead"
            )
        raise ConsentStateError(
            f"inconsistent existing receipt status: {existing_status!r}"
        )

    @staticmethod
    def _verify_immutable(row: sqlite3.Row, prepared: PreparedOperation) -> None:
        expected = {
            "kind": prepared.identity.operation_kind,
            "endpoint": prepared.identity.endpoint,
            "segment_id": prepared.identity.segment_id,
            "generation_id": prepared.identity.generation_id,
            "manifest_digest": prepared.identity.manifest_digest,
            "orchestration_plan_digest": prepared.identity.orchestration_plan_digest,
            "request_digest": prepared.identity.request_digest,
            "idempotency_key": prepared.idempotency_key,
            "heygen_title": prepared.heygen_title,
            "credential_profile_id": prepared.identity.credential_profile_id,
        }
        for col, value in expected.items():
            if row[col] != value:
                raise ConsentConflictError(
                    f"immutable field {col!r} differs for operation "
                    f"{prepared.operation_id}: stored={row[col]!r} new={value!r}"
                )

    @staticmethod
    def _require_pristine(conn, operation_id: str) -> None:
        row = conn.execute(
            "SELECT submit_attempts, lease_owner FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return
        if row["submit_attempts"] > 0 or row["lease_owner"] is not None:
            raise ConsentStateError(
                "operation is not pristine (already submitted or leased); "
                "cannot change a declined decision to granted"
            )

    @staticmethod
    def _stored_content_no_time(row: sqlite3.Row) -> dict:
        return {
            "namespace": _RECEIPT_NAMESPACE,
            "operation_id": row["operation_id"],
            "generation_id": row["generation_id"],
            "request_digest": row["request_digest"],
            "creative_brief_digest": row["creative_brief_digest"],
            "provider": row["provider"],
            "operation_kind": row["operation_kind"],
            "disclosure_version": row["disclosure_version"],
            "disclosed_assets": json.loads(row["disclosed_assets_json"]),
            "data_categories": json.loads(row["data_categories_json"]),
            "provider_cost_disclosure": row["provider_cost_disclosure"],
            "agentmesh_non_processor_disclosure": row["agentmesh_non_processor_disclosure"],
            "decision": row["status"],
        }

    @staticmethod
    def _insert_receipt(conn, receipt_digest, prepared, payload, status,
                        consented_at, assets_json, categories_json, now) -> None:
        conn.execute(
            "INSERT INTO heygen_consent_receipts (receipt_digest, operation_id, "
            "disclosure_version, generation_id, request_digest, "
            "creative_brief_digest, provider, operation_kind, "
            "disclosed_assets_json, data_categories_json, "
            "provider_cost_disclosure, agentmesh_non_processor_disclosure, "
            "status, consented_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_digest, prepared.operation_id, payload["disclosure_version"],
                prepared.identity.generation_id, prepared.identity.request_digest,
                payload["creative_brief_digest"], payload["provider"],
                payload["operation_kind"], assets_json, categories_json,
                payload["provider_cost_disclosure"],
                payload["agentmesh_non_processor_disclosure"], status, consented_at, now,
            ),
        )

    @staticmethod
    def _update_receipt(conn, receipt_digest, prepared, payload, status,
                        consented_at, assets_json, categories_json, now) -> None:
        # Update the PK (receipt_digest) plus lifecycle/content columns. Safe
        # because a declined receipt's digest is referenced nowhere.
        conn.execute(
            "UPDATE heygen_consent_receipts SET receipt_digest = ?, "
            "disclosure_version = ?, request_digest = ?, creative_brief_digest = ?, "
            "provider = ?, operation_kind = ?, disclosed_assets_json = ?, "
            "data_categories_json = ?, provider_cost_disclosure = ?, "
            "agentmesh_non_processor_disclosure = ?, status = ?, consented_at = ?, "
            "withdrawn_at = NULL WHERE operation_id = ?",
            (
                receipt_digest, payload["disclosure_version"],
                prepared.identity.request_digest, payload["creative_brief_digest"],
                payload["provider"], payload["operation_kind"], assets_json,
                categories_json, payload["provider_cost_disclosure"],
                payload["agentmesh_non_processor_disclosure"], status, consented_at,
                prepared.operation_id,
            ),
        )


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
