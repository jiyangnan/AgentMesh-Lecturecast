from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .protocol import ProductionManifest, canonical_digest


TIMING_CONTRACT_VERSION = "1.0"
MIN_ACTUAL_TO_PLANNED_RATIO = 0.75
MAX_ACTUAL_TO_PLANNED_RATIO = 1.25

_CJK_LANGUAGE_PREFIXES = ("zh", "ja", "ko")
_CJK_CHARACTER = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class NarrationTimingIssue:
    code: str
    scope: str
    section_id: str | None
    spoken_units: int
    duration_seconds: float
    units_per_minute: float
    minimum_units_per_minute: float
    maximum_units_per_minute: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioTimingError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__(";".join(issues))
        self.issues = tuple(issues)


def _payload(manifest: ProductionManifest | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(manifest, ProductionManifest):
        return manifest.model_dump()
    return dict(manifest)


def is_cjk_language(language: str) -> bool:
    normalized = language.strip().lower()
    return normalized.startswith(_CJK_LANGUAGE_PREFIXES)


def spoken_unit_count(text: str, language: str) -> int:
    if not is_cjk_language(language):
        return len(_WORD.findall(text))
    cjk_units = len(_CJK_CHARACTER.findall(text))
    remainder = _CJK_CHARACTER.sub(" ", text)
    return cjk_units + len(_WORD.findall(remainder))


def density_limits(language: str) -> tuple[float, float]:
    # These are deliberately broad safety rails, not a voice-speed prediction.
    # The exact local TTS duration is measured before rendering.
    return (45.0, 720.0) if is_cjk_language(language) else (45.0, 360.0)


def _density_issue(
    *,
    text: str,
    duration_seconds: float,
    language: str,
    scope: str,
    section_id: str | None,
) -> NarrationTimingIssue | None:
    units = spoken_unit_count(text, language)
    rate = units * 60.0 / duration_seconds
    minimum, maximum = density_limits(language)
    if minimum <= rate <= maximum:
        return None
    return NarrationTimingIssue(
        code="narration_too_sparse" if rate < minimum else "narration_too_dense",
        scope=scope,
        section_id=section_id,
        spoken_units=units,
        duration_seconds=round(duration_seconds, 3),
        units_per_minute=round(rate, 3),
        minimum_units_per_minute=minimum,
        maximum_units_per_minute=maximum,
    )


def narration_timing_issues(
    manifest: ProductionManifest | Mapping[str, Any],
) -> list[NarrationTimingIssue]:
    payload = _payload(manifest)
    fps = float(payload["fps"])
    language = str(payload["voice"]["language"])
    issues: list[NarrationTimingIssue] = []
    total_text: list[str] = []
    total_duration = 0.0
    for section in payload["script"]:
        duration = float(section["duration_frames"]) / fps
        text = str(section["narration"])
        total_text.append(text)
        total_duration += duration
        issue = _density_issue(
            text=text,
            duration_seconds=duration,
            language=language,
            scope="section",
            section_id=str(section["section_id"]),
        )
        if issue is not None:
            issues.append(issue)
    aggregate = _density_issue(
        text="\n".join(total_text),
        duration_seconds=total_duration,
        language=language,
        scope="manifest",
        section_id=None,
    )
    if aggregate is not None:
        issues.append(aggregate)
    return issues


def narration_review(
    manifest: ProductionManifest | Mapping[str, Any],
) -> dict[str, Any]:
    payload = _payload(manifest)
    fps = float(payload["fps"])
    language = str(payload["voice"]["language"])
    issues = narration_timing_issues(payload)
    return {
        "timing_contract_version": TIMING_CONTRACT_VERSION,
        "manifest_id": payload["manifest_id"],
        "manifest_digest": canonical_digest(ProductionManifest.model_validate(payload)),
        "duration_seconds": payload["total_frames"] / fps,
        "timing_passed": not issues,
        "timing_issues": [issue.to_dict() for issue in issues],
        "approval_required": True,
        "script": [
            {
                "section_id": section["section_id"],
                "title": section["title"],
                "narration": section["narration"],
                "planned_duration_seconds": section["duration_frames"] / fps,
                "spoken_units": spoken_unit_count(section["narration"], language),
            }
            for section in payload["script"]
        ],
    }


def build_audio_timing_plan(
    manifest: ProductionManifest | Mapping[str, Any],
    *,
    section_durations: Mapping[str, float],
    narration_duration_seconds: float,
) -> dict[str, Any]:
    payload = _payload(manifest)
    document = ProductionManifest.model_validate(payload)
    fps = int(payload["fps"])
    issues: list[str] = []
    planned_total_seconds = payload["total_frames"] / fps
    total_ratio = narration_duration_seconds / planned_total_seconds
    if not MIN_ACTUAL_TO_PLANNED_RATIO <= total_ratio <= MAX_ACTUAL_TO_PLANNED_RATIO:
        issues.append(
            "manifest:actual_to_planned_ratio="
            f"{total_ratio:.3f}:expected={MIN_ACTUAL_TO_PLANNED_RATIO:.2f}-"
            f"{MAX_ACTUAL_TO_PLANNED_RATIO:.2f}"
        )

    section_frames: list[tuple[dict[str, Any], int]] = []
    for section in payload["script"]:
        section_id = str(section["section_id"])
        actual = section_durations.get(section_id)
        if actual is None or actual <= 0:
            issues.append(f"{section_id}:missing_or_empty_audio")
            continue
        planned = section["duration_frames"] / fps
        ratio = actual / planned
        if not MIN_ACTUAL_TO_PLANNED_RATIO <= ratio <= MAX_ACTUAL_TO_PLANNED_RATIO:
            issues.append(
                f"{section_id}:actual_to_planned_ratio={ratio:.3f}:"
                f"expected={MIN_ACTUAL_TO_PLANNED_RATIO:.2f}-{MAX_ACTUAL_TO_PLANNED_RATIO:.2f}"
            )
        section_frames.append((section, max(1, round(actual * fps))))

    if issues:
        raise AudioTimingError(issues)

    render_total_frames = max(1, math.ceil(narration_duration_seconds * fps))
    raw_total_frames = sum(frames for _, frames in section_frames)
    if section_frames:
        last_section, last_frames = section_frames[-1]
        section_frames[-1] = (
            last_section,
            max(1, last_frames + render_total_frames - raw_total_frames),
        )

    cursor = 0
    sections = []
    for section, duration_frames in section_frames:
        sections.append(
            {
                "section_id": section["section_id"],
                "planned_start_frame": section["start_frame"],
                "planned_duration_frames": section["duration_frames"],
                "render_start_frame": cursor,
                "render_duration_frames": duration_frames,
                "actual_duration_seconds": round(
                    float(section_durations[section["section_id"]]), 6
                ),
            }
        )
        cursor += duration_frames

    plan = {
        "schema_version": "1.0",
        "timing_contract_version": TIMING_CONTRACT_VERSION,
        "manifest_id": payload["manifest_id"],
        "manifest_digest": canonical_digest(document),
        "fps": fps,
        "planned_total_frames": payload["total_frames"],
        "render_total_frames": render_total_frames,
        "narration_duration_seconds": round(narration_duration_seconds, 6),
        "actual_to_planned_ratio": round(total_ratio, 6),
        "sections": sections,
    }
    validate_audio_timing_plan(document, plan)
    return plan


def validate_audio_timing_plan(
    manifest: ProductionManifest | Mapping[str, Any],
    audio_timing: Mapping[str, Any],
) -> None:
    payload = _payload(manifest)
    document = ProductionManifest.model_validate(payload)
    issues: list[str] = []
    expected_digest = canonical_digest(document)
    fps = int(payload["fps"])
    planned_total_frames = int(payload["total_frames"])

    if audio_timing.get("schema_version") != "1.0":
        issues.append("audio_timing_schema_version_mismatch")
    if audio_timing.get("timing_contract_version") != TIMING_CONTRACT_VERSION:
        issues.append("audio_timing_contract_version_mismatch")
    if audio_timing.get("manifest_digest") != expected_digest:
        issues.append("audio_timing_manifest_digest_mismatch")
    if audio_timing.get("manifest_id") != payload["manifest_id"]:
        issues.append("audio_timing_manifest_id_mismatch")
    if int(audio_timing.get("fps", 0)) != fps:
        issues.append("audio_timing_fps_mismatch")
    if int(audio_timing.get("planned_total_frames", 0)) != planned_total_frames:
        issues.append("audio_timing_planned_total_mismatch")

    try:
        narration_duration = float(audio_timing["narration_duration_seconds"])
        render_total_frames = int(audio_timing["render_total_frames"])
        recorded_total_ratio = float(audio_timing["actual_to_planned_ratio"])
    except (KeyError, TypeError, ValueError):
        issues.append("audio_timing_totals_invalid")
        narration_duration = 0.0
        render_total_frames = 0
        recorded_total_ratio = 0.0

    if not math.isfinite(narration_duration) or narration_duration <= 0:
        issues.append("audio_timing_narration_duration_invalid")
        narration_duration = 0.0
    if not math.isfinite(recorded_total_ratio):
        issues.append("audio_timing_total_ratio_invalid")
        recorded_total_ratio = 0.0

    expected_total_ratio = narration_duration / (planned_total_frames / fps)
    if not MIN_ACTUAL_TO_PLANNED_RATIO <= expected_total_ratio <= MAX_ACTUAL_TO_PLANNED_RATIO:
        issues.append(
            "manifest:actual_to_planned_ratio="
            f"{expected_total_ratio:.3f}:expected={MIN_ACTUAL_TO_PLANNED_RATIO:.2f}-"
            f"{MAX_ACTUAL_TO_PLANNED_RATIO:.2f}"
        )
    if abs(recorded_total_ratio - expected_total_ratio) > 0.000_01:
        issues.append("audio_timing_total_ratio_mismatch")
    if render_total_frames != max(1, math.ceil(narration_duration * fps)):
        issues.append("audio_timing_render_total_mismatch")

    timing_sections = audio_timing.get("sections")
    if not isinstance(timing_sections, list):
        issues.append("audio_timing_sections_invalid")
        timing_sections = []
    expected_ids = [str(section["section_id"]) for section in payload["script"]]
    actual_ids = [
        str(section.get("section_id"))
        for section in timing_sections
        if isinstance(section, Mapping)
    ]
    if actual_ids != expected_ids:
        issues.append("audio_timing_section_order_mismatch")

    cursor = 0
    section_duration_sum = 0.0
    for planned, execution in zip(payload["script"], timing_sections):
        section_id = str(planned["section_id"])
        if not isinstance(execution, Mapping):
            issues.append(f"{section_id}:audio_timing_section_invalid")
            continue
        try:
            actual_duration = float(execution["actual_duration_seconds"])
            render_start = int(execution["render_start_frame"])
            render_duration = int(execution["render_duration_frames"])
            recorded_planned_start = int(execution["planned_start_frame"])
            recorded_planned_duration = int(execution["planned_duration_frames"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"{section_id}:audio_timing_section_invalid")
            continue
        if not math.isfinite(actual_duration) or actual_duration <= 0:
            issues.append(f"{section_id}:actual_duration_invalid")
            actual_duration = 0.0
        if recorded_planned_start != int(planned["start_frame"]):
            issues.append(f"{section_id}:planned_start_mismatch")
        if recorded_planned_duration != int(planned["duration_frames"]):
            issues.append(f"{section_id}:planned_duration_mismatch")
        if render_start != cursor or render_duration <= 0:
            issues.append(f"{section_id}:render_timeline_not_contiguous")
        planned_duration = int(planned["duration_frames"]) / fps
        ratio = actual_duration / planned_duration
        if not MIN_ACTUAL_TO_PLANNED_RATIO <= ratio <= MAX_ACTUAL_TO_PLANNED_RATIO:
            issues.append(
                f"{section_id}:actual_to_planned_ratio={ratio:.3f}:"
                f"expected={MIN_ACTUAL_TO_PLANNED_RATIO:.2f}-"
                f"{MAX_ACTUAL_TO_PLANNED_RATIO:.2f}"
            )
        if abs(render_duration - actual_duration * fps) > fps:
            issues.append(f"{section_id}:render_duration_measurement_mismatch")
        cursor += max(0, render_duration)
        section_duration_sum += max(0.0, actual_duration)

    if cursor != render_total_frames:
        issues.append("audio_timing_section_total_mismatch")
    allowed_concat_delta = max(1.0, len(timing_sections) * 0.15)
    if abs(section_duration_sum - narration_duration) > allowed_concat_delta:
        issues.append("audio_timing_concat_duration_mismatch")
    if issues:
        raise AudioTimingError(issues)


def render_timing_from_audio_plan(
    manifest: ProductionManifest | Mapping[str, Any],
    audio_timing: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _payload(manifest)
    validate_audio_timing_plan(payload, audio_timing)

    by_section = {item["section_id"]: item for item in audio_timing["sections"]}
    script_by_section = {item["section_id"]: item for item in payload["script"]}
    scene_timing: dict[str, dict[str, int]] = {}
    for scene in payload["scenes"]:
        section_id = scene["section_id"]
        section = script_by_section.get(section_id)
        execution = by_section.get(section_id)
        if section is None or execution is None:
            raise AudioTimingError([f"{scene['scene_id']}:missing_section_timing"])
        section_start = section["start_frame"]
        section_duration = section["duration_frames"]
        scene_offset = scene["start_frame"] - section_start
        scene_end_offset = scene_offset + scene["duration_frames"]
        if scene_offset < 0 or scene_end_offset > section_duration:
            raise AudioTimingError([f"{scene['scene_id']}:scene_outside_section"])
        scale = execution["render_duration_frames"] / section_duration
        start = execution["render_start_frame"] + round(scene_offset * scale)
        end = execution["render_start_frame"] + round(scene_end_offset * scale)
        scene_timing[scene["scene_id"]] = {
            "start_frame": start,
            "duration_frames": max(1, end - start),
        }

    return {
        "schema_version": "1.0",
        "manifest_digest": audio_timing["manifest_digest"],
        "total_frames": int(audio_timing["render_total_frames"]),
        "scene_timing": scene_timing,
    }
