#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected_duration = manifest["total_frames"] / manifest["fps"]
    for output in manifest["outputs"]:
        path = args.output_dir / output["filename"]
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f"missing output: {path}")
        metadata = probe(path)
        video_stream = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
        actual_size = (video_stream["width"], video_stream["height"])
        if actual_size != (output["width"], output["height"]):
            raise SystemExit(f"wrong dimensions for {path.name}: {actual_size}")
        if output["kind"] == "video":
            duration = float(metadata["format"]["duration"])
            if abs(duration - expected_duration) > 1.0:
                raise SystemExit(f"wrong duration for {path.name}: {duration}")
    print(f"validated {len(manifest['outputs'])} local outputs")


if __name__ == "__main__":
    main()

