from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lecturecast.timing import build_audio_timing_plan


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "templates" / "shared"
FIXTURE_DIR = ROOT / "tests" / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"lecturecast_template_{name}", SHARED / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_subtitles_follow_measured_audio_timing(tmp_path: Path) -> None:
    manifest = _fixture("production-manifest-v1.json")
    timing = build_audio_timing_plan(
        manifest,
        section_durations={"hook": 9.5, "demo": 39.0, "ending": 10.0},
        narration_duration_seconds=58.5,
    )
    timing_path = tmp_path / "audio-timing.json"
    timing_path.write_text(json.dumps(timing), encoding="utf-8")
    output_dir = tmp_path / "subtitles"
    environment = os.environ.copy()
    environment["LECTURECAST_SUBTITLE_FONT"] = "Source Han Sans SC"

    subprocess.run(
        [
            sys.executable,
            str(SHARED / "build_manifest_subtitles.py"),
            str(FIXTURE_DIR / "production-manifest-v1.json"),
            str(output_dir),
            "--timing",
            str(timing_path),
        ],
        check=True,
        env=environment,
    )

    srt = (output_dir / "subtitles.srt").read_text(encoding="utf-8")
    assert "00:00:58,500" in srt
    assert "00:01:00,000" not in srt


def test_audio_builder_synthesizes_sections_and_persists_validated_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script("build_manifest_audio.py")
    manifest = _fixture("production-manifest-v1.json")
    output = tmp_path / "build" / "narration.mp3"
    timing_out = tmp_path / "build" / "audio-timing.json"
    measured = {
        "01-hook.mp3": 9.5,
        "02-demo.mp3": 39.0,
        "03-ending.mp3": 10.0,
        "narration.mp3": 58.5,
    }

    async def fake_synthesize(_manifest: dict, _text: str, path: Path) -> None:
        path.write_bytes(b"section")

    def fake_concatenate(inputs: list[Path], path: Path) -> None:
        assert [item.name for item in inputs] == [
            "01-hook.mp3",
            "02-demo.mp3",
            "03-ending.mp3",
        ]
        path.write_bytes(b"narration")

    monkeypatch.setattr(module, "synthesize_section", fake_synthesize)
    monkeypatch.setattr(module, "concatenate_audio", fake_concatenate)
    monkeypatch.setattr(module, "probe_duration", lambda path: measured[path.name])

    timing = asyncio.run(module.build(manifest, output, timing_out))

    assert output.read_bytes() == b"narration"
    assert timing["render_total_frames"] == 1755
    assert json.loads(timing_out.read_text(encoding="utf-8")) == timing


def test_output_validator_rejects_container_padding_after_short_narration(
    tmp_path: Path,
) -> None:
    module = _load_script("validate_manifest_outputs.py")
    manifest = _fixture("production-manifest-v1.json")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    for output in manifest["outputs"]:
        (output_dir / output["filename"]).write_bytes(b"fixture")
    narration = tmp_path / "narration.mp3"
    narration.write_bytes(b"fixture")

    sizes = {output["filename"]: (output["width"], output["height"]) for output in manifest["outputs"]}

    def fake_probe(path: Path) -> dict:
        if path == narration:
            return {"format": {"duration": "10.0"}, "streams": [{"codec_type": "audio"}]}
        width, height = sizes[path.name]
        streams = [{"codec_type": "video", "width": width, "height": height}]
        if path.suffix == ".mp4":
            streams.append({"codec_type": "audio", "duration": "60.0"})
        return {"format": {"duration": "60.0"}, "streams": streams}

    with pytest.raises(ValueError, match="audio coverage mismatch"):
        module.validate_outputs(
            manifest,
            output_dir,
            narration=narration,
            probe_fn=fake_probe,
        )


def test_output_validator_accepts_video_bound_to_measured_narration(tmp_path: Path) -> None:
    module = _load_script("validate_manifest_outputs.py")
    manifest = _fixture("production-manifest-v1.json")
    timing = build_audio_timing_plan(
        manifest,
        section_durations={"hook": 9.5, "demo": 39.0, "ending": 10.0},
        narration_duration_seconds=58.5,
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    for output in manifest["outputs"]:
        (output_dir / output["filename"]).write_bytes(b"fixture")
    narration = tmp_path / "narration.mp3"
    narration.write_bytes(b"fixture")
    sizes = {output["filename"]: (output["width"], output["height"]) for output in manifest["outputs"]}

    def fake_probe(path: Path) -> dict:
        if path == narration:
            return {"format": {"duration": "58.5"}, "streams": [{"codec_type": "audio"}]}
        width, height = sizes[path.name]
        streams = [{"codec_type": "video", "width": width, "height": height}]
        if path.suffix == ".mp4":
            streams.append({"codec_type": "audio", "duration": "58.5"})
        return {"format": {"duration": "58.5"}, "streams": streams}

    assert (
        module.validate_outputs(
            manifest,
            output_dir,
            timing=timing,
            narration=narration,
            probe_fn=fake_probe,
        )
        == 4
    )
