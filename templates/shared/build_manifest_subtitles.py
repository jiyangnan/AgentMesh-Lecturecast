#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from lecturecast.timing import validate_audio_timing_plan
from lecturecast.host_agent import require_project_host_workflow
from subtitle_font import subtitle_font_name


def timestamp(seconds: float, *, ass: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds_value, milliseconds = divmod(milliseconds, 1000)
    if ass:
        return f"{hours}:{minutes:02d}:{seconds_value:02d}.{milliseconds // 10:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds_value:02d},{milliseconds:03d}"


def chunks(text: str, limit: int = 22) -> list[str]:
    parts = [part for part in re.split(r"(?<=[，。！？；])", text.strip()) if part]
    result: list[str] = []
    buffer = ""
    for part in parts:
        if buffer and len(buffer) + len(part) > limit:
            result.append(buffer)
            buffer = part
        else:
            buffer += part
    if buffer:
        result.append(buffer)
    return result or [text.strip()]


def cues(manifest: dict, timing: dict | None = None) -> list[tuple[float, float, str]]:
    fps = manifest["fps"]
    timing_by_section = {}
    if timing is not None:
        validate_audio_timing_plan(manifest, timing)
        timing_by_section = {item["section_id"]: item for item in timing["sections"]}
    result: list[tuple[float, float, str]] = []
    for section in manifest["script"]:
        execution = timing_by_section.get(section["section_id"])
        start_frame = (
            execution["render_start_frame"] if execution is not None else section["start_frame"]
        )
        duration_frames = (
            execution["render_duration_frames"]
            if execution is not None
            else section["duration_frames"]
        )
        start = start_frame / fps
        duration = duration_frames / fps
        texts = chunks(section["narration"])
        total = sum(max(1, len(text)) for text in texts)
        cursor = start
        for text in texts:
            cue_duration = duration * max(1, len(text)) / total
            result.append((cursor, cursor + cue_duration, text))
            cursor += cue_duration
    return result


def ass_header(*, width: int, height: int, font_size: int, margin_v: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{subtitle_font_name()},{font_size},&H00FFFFFF,&H000000FF,&H0029332E,&H00000000,1,0,0,0,100,100,0,0,1,5,1,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def highlight(text: str, keywords: list[str]) -> str:
    safe = text.replace("{", "(").replace("}", ")")
    for keyword in sorted(set(keywords), key=len, reverse=True):
        safe = safe.replace(
            keyword,
            "{\\c&H008C9FC9&\\b1}" + keyword + "{\\c&H00FFFFFF&\\b1}",
        )
    return safe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--timing", type=Path)
    args = parser.parse_args()
    if args.manifest.parent.name == ".lecturecast":
        require_project_host_workflow(args.manifest.parent.parent)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    timing = json.loads(args.timing.read_text(encoding="utf-8")) if args.timing else None
    items = cues(manifest, timing)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    srt = []
    for index, (start, end, text) in enumerate(items, 1):
        srt.append(f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n")
    (args.output_dir / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")

    keywords = manifest["subtitles"].get("keyword_highlights", [])
    safe_percent = manifest["subtitles"].get("safe_area_bottom_percent", 18)
    variants = (
        ("subtitle_landscape.ass", 1920, 1080, 52, round(1080 * safe_percent / 100)),
        ("subtitle_vertical.ass", 1080, 1920, 64, round(1920 * safe_percent / 100)),
    )
    for filename, width, height, font_size, margin_v in variants:
        events = [
            f"Dialogue: 0,{timestamp(start, ass=True)},{timestamp(end, ass=True)},Default,,0,0,0,,{highlight(text, keywords)}"
            for start, end, text in items
        ]
        (args.output_dir / filename).write_text(
            ass_header(width=width, height=height, font_size=font_size, margin_v=margin_v)
            + "\n".join(events)
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
