#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Callable

from lecturecast.timing import validate_audio_timing_plan


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def validate_outputs(
    manifest: dict,
    output_dir: Path,
    *,
    timing: dict | None = None,
    narration: Path | None = None,
    probe_fn: Callable[[Path], dict] = probe,
) -> int:
    fps = manifest["fps"]
    expected_duration = manifest["total_frames"] / fps
    narration_duration: float | None = None
    if timing is not None:
        validate_audio_timing_plan(manifest, timing)
        expected_duration = timing["render_total_frames"] / fps
        narration_duration = float(timing["narration_duration_seconds"])
    if narration is not None:
        narration_metadata = probe_fn(narration)
        probed_narration = float(narration_metadata["format"]["duration"])
        if narration_duration is not None and abs(probed_narration - narration_duration) > 0.5:
            raise ValueError("narration duration changed after timing was measured")
        narration_duration = probed_narration

    count = 0
    for output in manifest["outputs"]:
        path = output_dir / output["filename"]
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing output: {path}")
        metadata = probe_fn(path)
        video_stream = next(
            stream for stream in metadata["streams"] if stream["codec_type"] == "video"
        )
        actual_size = (video_stream["width"], video_stream["height"])
        if actual_size != (output["width"], output["height"]):
            raise ValueError(f"wrong dimensions for {path.name}: {actual_size}")
        if output["kind"] == "video":
            duration = float(metadata["format"]["duration"])
            if abs(duration - expected_duration) > 1.0:
                raise ValueError(f"wrong duration for {path.name}: {duration}")
            audio_streams = [
                stream for stream in metadata["streams"] if stream["codec_type"] == "audio"
            ]
            if narration is not None and not audio_streams:
                raise ValueError(f"missing audio stream for {path.name}")
            if narration_duration is not None and abs(duration - narration_duration) > 1.0:
                raise ValueError(
                    f"audio coverage mismatch for {path.name}: "
                    f"video={duration:.3f}s narration={narration_duration:.3f}s"
                )
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--timing", type=Path)
    parser.add_argument("--narration", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    timing = json.loads(args.timing.read_text(encoding="utf-8")) if args.timing else None
    try:
        count = validate_outputs(
            manifest,
            args.output_dir,
            timing=timing,
            narration=args.narration,
        )
    except (OSError, ValueError, StopIteration, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"validated {count} local outputs including narration coverage")


if __name__ == "__main__":
    main()
