#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import urllib.request
from pathlib import Path

from lecturecast.protocol import ProductionManifest, canonical_digest
from lecturecast.host_agent import require_project_host_workflow
from lecturecast.timing import AudioTimingError, build_audio_timing_plan


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError(f"empty audio: {path.name}")
    return duration


def minimax_audio(text: str, voice_id: str) -> bytes | None:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        return None
    request = urllib.request.Request(
        "https://api.minimaxi.com/v1/t2a_v2",
        data=json.dumps(
            {
                "model": "speech-02-hd",
                "text": text,
                "stream": False,
                "voice_setting": {
                    "voice_id": voice_id,
                    "speed": 1.0,
                    "vol": 1,
                    "pitch": 0,
                },
                "audio_setting": {
                    "sample_rate": 32000,
                    "bitrate": 128000,
                    "format": "mp3",
                    "channel": 1,
                },
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        response = json.loads(urllib.request.urlopen(request, timeout=60).read())
        if response.get("base_resp", {}).get("status_code") == 0:
            return bytes.fromhex(response["data"]["audio"])
    except Exception:
        return None
    return None


async def synthesize_section(manifest: dict, text: str, output: Path) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise SystemExit("edge-tts is required for local narration: pip install edge-tts") from exc
    voice = manifest["voice"]
    if voice["engine"] == "minimax":
        audio = minimax_audio(text, voice["voice_id"])
        if audio is not None:
            output.write_bytes(audio)
            return
        print("MiniMax BYOK failed; falling back to Edge without exposing the key.")
    rate = int(voice.get("rate_percent", 0))
    communicator = edge_tts.Communicate(
        text,
        voice["voice_id"] if voice["engine"] == "edge" else "zh-CN-YunjianNeural",
        rate=f"{rate:+d}%",
    )
    await communicator.save(str(output))


def concatenate_audio(inputs: list[Path], output: Path) -> None:
    command = ["ffmpeg", "-y"]
    for item in inputs:
        command.extend(["-i", str(item)])
    labels = "".join(f"[{index}:a]" for index in range(len(inputs)))
    command.extend(
        [
            "-filter_complex",
            f"{labels}concat=n={len(inputs)}:v=0:a=1[a]",
            "-map",
            "[a]",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output),
        ]
    )
    subprocess.run(command, check=True)


def reusable_timing(path: Path, *, manifest_digest: str, output: Path) -> dict | None:
    if not path.is_file() or not output.is_file() or output.stat().st_size <= 0:
        return None
    try:
        timing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if timing.get("manifest_digest") != manifest_digest:
        return None
    if not timing.get("sections") or not timing.get("render_total_frames"):
        return None
    return timing


async def build(manifest: dict, output: Path, timing_out: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    section_dir = output.parent / "audio-sections"
    section_dir.mkdir(parents=True, exist_ok=True)
    section_files: list[Path] = []
    durations: dict[str, float] = {}
    for index, section in enumerate(manifest["script"], 1):
        section_path = section_dir / f"{index:02d}-{section['section_id']}.mp3"
        await synthesize_section(manifest, section["narration"], section_path)
        duration = probe_duration(section_path)
        section_files.append(section_path)
        durations[section["section_id"]] = duration

    concatenate_audio(section_files, output)
    narration_duration = probe_duration(output)
    try:
        timing = build_audio_timing_plan(
            manifest,
            section_durations=durations,
            narration_duration_seconds=narration_duration,
        )
    except AudioTimingError as exc:
        raise SystemExit(
            "local narration does not match the signed Manifest timeline: "
            + "; ".join(exc.issues)
            + ". Stop before rendering and request a corrected Manifest."
        ) from None
    timing_out.parent.mkdir(parents=True, exist_ok=True)
    timing_out.write_text(
        json.dumps(timing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return timing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--timing-out", type=Path)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    if args.manifest.parent.name == ".lecturecast":
        require_project_host_workflow(args.manifest.parent.parent)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["voice"]["engine"] not in {"edge", "minimax"}:
        raise SystemExit("unsupported local voice engine")
    timing_out = args.timing_out or args.output.with_name("audio-timing.json")
    digest = canonical_digest(ProductionManifest.model_validate(manifest))
    if args.reuse and reusable_timing(timing_out, manifest_digest=digest, output=args.output):
        print(f"reusing local narration and measured timing: {args.output}")
        return
    asyncio.run(build(manifest, args.output, timing_out))


if __name__ == "__main__":
    main()
