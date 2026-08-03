from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_DIR = Path(__file__).with_name("schemas")
FORBIDDEN_EXECUTABLE_KEYS = {
    "command",
    "component_source",
    "exec",
    "executable",
    "javascript",
    "module_url",
    "python",
    "shell",
    "tsx",
}
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class ProtocolValidationError(ValueError):
    """Raised when a Director protocol document cannot be executed safely."""


def _load_schema(directory: Path, filename: str) -> dict[str, Any]:
    return json.loads((directory / filename).read_text(encoding="utf-8"))


def _validate_schema(schema_path: Path, payload: dict[str, Any]) -> None:
    validator = Draft202012Validator(_load_schema(schema_path.parent, schema_path.name), format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "document"
        raise ProtocolValidationError(f"{location}: {first.message}")


def _ensure_unique(values: list[str], *, label: str) -> None:
    if len(values) != len(set(values)):
        raise ProtocolValidationError(f"duplicate {label}")


def _reject_unsafe_json(value: Any, *, location: str = "props") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_EXECUTABLE_KEYS:
                raise ProtocolValidationError(f"executable field is not allowed at {location}.{key}")
            _reject_unsafe_json(child, location=f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_json(child, location=f"{location}[{index}]")
        return
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if "\x00" in value:
            raise ProtocolValidationError(f"NUL byte is not allowed at {location}")
        if (
            normalized.startswith(("../", "~/", "/"))
            or "/../" in normalized
            or normalized.endswith("/..")
        ):
            raise ProtocolValidationError(f"unsafe local path is not allowed at {location}")
        if _WINDOWS_ABSOLUTE.match(value):
            raise ProtocolValidationError(f"absolute local path is not allowed at {location}")


def _validate_asset_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in {"asset", "https"}:
        raise ProtocolValidationError("asset URI must use asset:// or https://")
    if not parsed.netloc:
        raise ProtocolValidationError("asset URI must include a host or asset namespace")
    _reject_unsafe_json(uri, location="asset.uri")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_decision_card_set(payload: dict[str, Any]) -> None:
    questions = payload["questions"]
    _ensure_unique([question["question_id"] for question in questions], label="question_id")
    for question in questions:
        _ensure_unique([option["option_id"] for option in question["options"]], label="option_id")
        if question["selection_mode"] == "single" and (
            question["min_selections"],
            question["max_selections"],
        ) != (1, 1):
            raise ProtocolValidationError("single-select questions require min=max=1")
        if question["min_selections"] > question["max_selections"]:
            raise ProtocolValidationError("min_selections cannot exceed max_selections")
        if question["max_selections"] > len(question["options"]):
            raise ProtocolValidationError("max_selections cannot exceed option count")


def _validate_creative_brief(payload: dict[str, Any]) -> None:
    _ensure_unique([output["output_id"] for output in payload["outputs"]], label="output_id")
    _ensure_unique(
        [constraint["constraint_id"] for constraint in payload["constraints"]],
        label="constraint_id",
    )
    palette = [color.lower() for color in payload["visual"]["palette"]]
    _ensure_unique(palette, label="palette color")


def _validate_client_capabilities(payload: dict[str, Any]) -> None:
    for field_name in (
        "supported_manifest_versions",
        "components",
        "aspect_ratios",
        "output_formats",
        "tts_engines",
    ):
        _ensure_unique(payload[field_name], label="capability value")


def _validate_production_manifest(payload: dict[str, Any]) -> None:
    sections = payload["script"]
    scenes = payload["scenes"]
    outputs = payload["outputs"]
    total_frames = payload["total_frames"]

    _ensure_unique([section["section_id"] for section in sections], label="section_id")
    _ensure_unique([scene["scene_id"] for scene in scenes], label="scene_id")
    _ensure_unique([output["output_id"] for output in outputs], label="output_id")
    _ensure_unique([output["filename"] for output in outputs], label="output filename")
    section_ids = {section["section_id"] for section in sections}

    for section in sections:
        if section["start_frame"] + section["duration_frames"] > total_frames:
            raise ProtocolValidationError(f"section {section['section_id']} exceeds total_frames")
    for scene in scenes:
        if scene["section_id"] not in section_ids:
            raise ProtocolValidationError(f"scene {scene['scene_id']} references an unknown section")
        if scene["start_frame"] + scene["duration_frames"] > total_frames:
            raise ProtocolValidationError(f"scene {scene['scene_id']} exceeds total_frames")
        _reject_unsafe_json(scene["props"])
        _ensure_unique([asset["asset_id"] for asset in scene["assets"]], label="asset_id")
        for asset in scene["assets"]:
            _validate_asset_uri(asset["uri"])

    failed_error = any(
        not check["passed"] and check["severity"] == "error"
        for check in payload["quality"]["checks"]
    )
    _ensure_unique(
        [check["check_id"] for check in payload["quality"]["checks"]],
        label="check_id",
    )
    if payload["quality"]["passed"] == failed_error:
        raise ProtocolValidationError("quality passed flag does not match error checks")
    if _parse_datetime(payload["content_expires_at"]) <= _parse_datetime(payload["created_at"]):
        raise ProtocolValidationError("content_expires_at must be later than created_at")


@dataclass(frozen=True)
class ProtocolDocument:
    _payload: dict[str, Any] = field(repr=False)
    schema_filename: ClassVar[str]
    # Directory holding this version's schema bundle. v1.0 documents use the
    # flat SCHEMA_DIR; v1.1 documents override this to SCHEMA_DIR / "v1.1".
    schema_dir: ClassVar[Path] = SCHEMA_DIR

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> Self:
        document = copy.deepcopy(payload)
        _validate_schema(cls.schema_dir / cls.schema_filename, document)
        cls._validate_semantics(document)
        return cls(document)

    @classmethod
    def model_validate_json(cls, content: str | bytes) -> Self:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ProtocolValidationError("protocol document must be a JSON object")
        return cls.model_validate(payload)

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        del payload

    def model_dump(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)

    @property
    def payload(self) -> dict[str, Any]:
        return self.model_dump()


@dataclass(frozen=True)
class DecisionCardSet(ProtocolDocument):
    schema_filename: ClassVar[str] = "decision-card-set.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        _validate_decision_card_set(payload)


@dataclass(frozen=True)
class CreativeBrief(ProtocolDocument):
    schema_filename: ClassVar[str] = "creative-brief.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        _validate_creative_brief(payload)


@dataclass(frozen=True)
class ClientCapabilities(ProtocolDocument):
    schema_filename: ClassVar[str] = "client-capabilities.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        _validate_client_capabilities(payload)


@dataclass(frozen=True)
class ProductionManifest(ProtocolDocument):
    schema_filename: ClassVar[str] = "production-manifest.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        _validate_production_manifest(payload)


# --- Digital-human edition (v1.1) ------------------------------------------
# These load the server-published v1.1 schema bundle from schemas/v1.1/. The
# semantic validators are shared with v1.0 (they check version-agnostic
# uniqueness/constraint invariants and do not reject v1.1's additive fields).

_V1_1_SCHEMA_DIR = SCHEMA_DIR / "v1.1"


@dataclass(frozen=True)
class DecisionCardSetV1_1(DecisionCardSet):
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR


@dataclass(frozen=True)
class CreativeBriefV1_1(CreativeBrief):
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR


@dataclass(frozen=True)
class ClientCapabilitiesV1_1(ClientCapabilities):
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        super()._validate_semantics(payload)
        # supported_artifact_versions is an object keyed by artifact type →
        # version array; each array must be unique.
        versions = payload.get("supported_artifact_versions")
        if isinstance(versions, dict):
            for artifact, version_list in versions.items():
                if isinstance(version_list, list):
                    _ensure_unique(version_list, label=f"{artifact} version")
        processors = payload.get("third_party_processors") or []
        for processor in processors:
            if not isinstance(processor, dict):
                continue
            for field_name in ("operations", "features"):
                _ensure_unique(processor.get(field_name, []), label=f"processor {field_name}")
        _ensure_unique(
            [p.get("provider") for p in processors if isinstance(p, dict)],
            label="provider",
        )


@dataclass(frozen=True)
class PresenterPlanV1_1(ProtocolDocument):
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "presenter-plan.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        _ensure_unique(
            [seg.get("segment_id") for seg in payload.get("segments", []) if isinstance(seg, dict)],
            label="segment_id",
        )


@dataclass(frozen=True)
class OrchestrationPlanV1_1(ProtocolDocument):
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "orchestration-plan.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        del payload


@dataclass(frozen=True)
class ManifestGenerationOutV1_1(ProtocolDocument):
    """Server generation response for v1.1 sessions — includes milestone
    billing projection (milestone_charges / billing_state / resume_available).
    Does NOT carry ledger_id / idempotency_key / lease fields."""

    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "manifest-generation-out.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        # Defense-in-depth: the schema (additionalProperties:false) already
        # rejects unknown fields, but if a future model change accidentally
        # adds a sensitive field, this catches it. Use ProtocolValidationError
        # (not assert) so -O doesn't strip it.
        for sensitive in ("ledger_id", "idempotency_key", "external_id", "lease_owner", "lease_fence"):
            if sensitive in payload:
                raise ProtocolValidationError(f"sensitive field leaked into generation response: {sensitive}")
        # milestone_charges: unique milestones, cost > 0, ordered subset of
        # MILESTONE_ORDER.
        charges = payload.get("milestone_charges") or []
        seen_milestones: list[str] = []
        _canonical_order = ("manifest", "presenter_plan", "orchestration")
        last_idx = -1
        for charge in charges:
            ms = charge.get("milestone")
            if ms in seen_milestones:
                raise ProtocolValidationError(f"duplicate milestone in charges: {ms}")
            seen_milestones.append(ms)
            cost = charge.get("cost", 0)
            if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
                raise ProtocolValidationError(f"non-positive or invalid cost for milestone {ms}: {cost!r}")
            if ms in _canonical_order:
                idx = _canonical_order.index(ms)
                if idx <= last_idx:
                    raise ProtocolValidationError(f"milestone {ms} out of canonical order")
                last_idx = idx
        # billing_state must be one of the known values.
        billing_state = payload.get("billing_state")
        if billing_state is not None:
            if billing_state not in (
                "in_progress", "awaiting_credits", "partially_charged", "charged", "blocked",
            ):
                raise ProtocolValidationError(f"unknown billing_state: {billing_state}")


def documents_for_protocol_version(protocol_version: str) -> dict[str, type[ProtocolDocument]]:
    """Return the model classes for the given protocol version. The Director
    client uses these to parse server responses (cards/brief/capabilities) and
    artifacts (presenter/orchestration plans)."""
    if protocol_version == "1.1":
        return {
            "decision_card_set": DecisionCardSetV1_1,
            "creative_brief": CreativeBriefV1_1,
            "client_capabilities": ClientCapabilitiesV1_1,
            "presenter_plan": PresenterPlanV1_1,
            "orchestration_plan": OrchestrationPlanV1_1,
            "production_manifest": ProductionManifest,
            "manifest_generation_out": ManifestGenerationOutV1_1,
            "recovery_catalog": RecoveryDirectiveCatalog,
        }
    if protocol_version == "1.0":
        return {
            "decision_card_set": DecisionCardSet,
            "creative_brief": CreativeBrief,
            "client_capabilities": ClientCapabilities,
            "production_manifest": ProductionManifest,
        }
    raise ValueError(f"unsupported protocol version: {protocol_version!r}")


def parse_client_capabilities(payload: dict[str, Any]) -> ProtocolDocument:
    """Durable-load dispatcher: pick the ClientCapabilities model by
    schema_version. Used by ProjectStore / preflight so a saved v1.1 capability
    is not rejected by the v1.0 strict schema on resume."""
    if payload.get("schema_version") == "1.1":
        return ClientCapabilitiesV1_1.model_validate(payload)
    return ClientCapabilities.model_validate(payload)


def parse_creative_brief(payload: dict[str, Any]) -> ProtocolDocument:
    """Durable-load dispatcher: pick the CreativeBrief model by schema_version."""
    if payload.get("schema_version") == "1.1":
        return CreativeBriefV1_1.model_validate(payload)
    return CreativeBrief.model_validate(payload)


@dataclass(frozen=True)
class RecoveryDirectiveCatalog(ProtocolDocument):
    """Pre-signed recovery-directive catalog (tech spec §7.3).

    Delivered with the v1.1 session (DirectorSessionOutV1_1.recovery_catalog)
    and the v1.1 generation view (ManifestGenerationOutV1_1.recovery_catalog).
    The client maps a local failure to a failure_kind, looks it up here, and
    presents the directive's message/options/steer_back (Host Conformance
    Contract §7.4). Catalog is advisory — never gates billing."""

    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "recovery-directive-catalog.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        # Defense-in-depth: directives object keys must be valid failure_kind
        # strings and match their directive's failure_kind (self-consistency).
        # The schema already enforces shape; this catches a key↔value mismatch
        # (a swap that a re-signed catalog would otherwise mask).
        directives = payload.get("directives")
        if not isinstance(directives, dict):
            return
        for key, directive in directives.items():
            if not isinstance(directive, dict):
                continue
            directive_failure_kind = directive.get("failure_kind")
            if isinstance(directive_failure_kind, str) and directive_failure_kind != key:
                raise ProtocolValidationError(
                    f"directives key {key!r} does not match failure_kind {directive_failure_kind!r}"
                )


@dataclass(frozen=True)
class ErrorEnvelopeV1_1(ProtocolDocument):
    """v1.1 error envelope — validates the server's error response against
    the vendored v1.1 schema (strict code set, strict retryable bool)."""
    schema_dir: ClassVar[Path] = _V1_1_SCHEMA_DIR
    schema_filename: ClassVar[str] = "error-envelope.schema.json"

    @classmethod
    def _validate_semantics(cls, payload: dict[str, Any]) -> None:
        # Strict retryable: must be literal bool (schema allows bool; this is
        # defense-in-depth against a future schema relaxation).
        retryable = payload.get("retryable")
        if type(retryable) is not bool:
            raise ProtocolValidationError(
                f"retryable must be a literal bool, got {type(retryable).__name__}"
            )
