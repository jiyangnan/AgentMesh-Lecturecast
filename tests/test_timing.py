from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecturecast.timing import (
    AudioTimingError,
    build_audio_timing_plan,
    narration_review,
    narration_timing_issues,
    render_timing_from_audio_plan,
    spoken_unit_count,
    validate_audio_timing_plan,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_spoken_units_are_language_aware() -> None:
    assert spoken_unit_count("中文 narration 123", "zh-CN") == 4
    assert spoken_unit_count("A real narration test", "en-US") == 4


def test_release_fixture_has_plausible_static_narration_timing() -> None:
    manifest = _fixture("production-manifest-v1.json")

    assert narration_timing_issues(manifest) == []
    review = narration_review(manifest)
    assert review["timing_passed"] is True
    assert review["approval_required"] is True
    assert review["script"][0]["narration"] == manifest["script"][0]["narration"]


def test_canary_silent_tail_manifest_is_rejected_before_render() -> None:
    manifest = _fixture("production-manifest-v1.json")
    manifest["total_frames"] = 10_800
    durations = [900, 1800, 2700, 3600, 1800]
    narrations = [
        "先看结果：用几分钟掌握生产链路最小测试最值得带走的变化。",
        "团队常面临协作脱节、版本混乱和重复劳动等问题。",
        "最小测试通过精简流程、明确节点和自动化检查来提前暴露风险。",
        "通用决策框架帮助团队决定继续、调整或终止。",
        "总结一下：先验证再扩大规模，并根据实际业务数据调整参数。",
    ]
    manifest["script"] = []
    manifest["scenes"] = []
    cursor = 0
    for index, (duration, narration) in enumerate(zip(durations, narrations), 1):
        section_id = f"section_{index}"
        manifest["script"].append(
            {
                "section_id": section_id,
                "title": section_id,
                "learning_objective": "理解本节内容。",
                "narration": narration,
                "start_frame": cursor,
                "duration_frames": duration,
            }
        )
        cursor += duration

    issues = narration_timing_issues(manifest)

    assert issues
    assert any(issue.code == "narration_too_sparse" for issue in issues)
    assert any(issue.scope == "manifest" for issue in issues)


def test_audio_timing_plan_fails_closed_on_38_second_audio_for_360_seconds() -> None:
    manifest = _fixture("production-manifest-v1.json")
    manifest["total_frames"] = 10_800
    for section in manifest["script"]:
        section["duration_frames"] *= 6
        section["start_frame"] *= 6

    with pytest.raises(AudioTimingError, match="actual_to_planned_ratio"):
        build_audio_timing_plan(
            manifest,
            section_durations={"hook": 6.0, "demo": 25.0, "ending": 6.44},
            narration_duration_seconds=37.44,
        )


def test_audio_timing_plan_drives_scene_frames_and_total_duration() -> None:
    manifest = _fixture("production-manifest-v1.json")
    timing = build_audio_timing_plan(
        manifest,
        section_durations={"hook": 9.5, "demo": 39.0, "ending": 10.0},
        narration_duration_seconds=58.5,
    )
    render_timing = render_timing_from_audio_plan(manifest, timing)

    assert timing["render_total_frames"] == 1755
    assert render_timing["total_frames"] == 1755
    assert render_timing["scene_timing"]["hook_outcome"] == {
        "start_frame": 0,
        "duration_frames": 285,
    }
    assert render_timing["scene_timing"]["ending_summary"]["duration_frames"] == 300


def test_audio_timing_plan_rejects_local_ratio_or_timeline_tampering() -> None:
    manifest = _fixture("production-manifest-v1.json")
    timing = build_audio_timing_plan(
        manifest,
        section_durations={"hook": 9.5, "demo": 39.0, "ending": 10.0},
        narration_duration_seconds=58.5,
    )
    timing["narration_duration_seconds"] = 1.0
    timing["render_total_frames"] = 30

    with pytest.raises(AudioTimingError, match="actual_to_planned_ratio"):
        validate_audio_timing_plan(manifest, timing)
