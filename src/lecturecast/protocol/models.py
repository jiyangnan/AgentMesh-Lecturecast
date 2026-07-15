from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
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


def _load_schema(filename: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def _validate_schema(filename: str, payload: dict[str, Any]) -> None:
    validator = Draft202012Validator(_load_schema(filename), format_checker=FormatChecker())
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

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "ProtocolDocument":
        document = copy.deepcopy(payload)
        _validate_schema(cls.schema_filename, document)
        cls._validate_semantics(document)
        return cls(document)

    @classmethod
    def model_validate_json(cls, content: str | bytes) -> "ProtocolDocument":
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
