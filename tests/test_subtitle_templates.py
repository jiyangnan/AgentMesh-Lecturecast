from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "templates" / "shared"


def _load_subtitle_font_module():
    spec = importlib.util.spec_from_file_location(
        "lecturecast_template_subtitle_font",
        SHARED / "subtitle_font.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_subtitle_font_has_cjk_platform_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LECTURECAST_SUBTITLE_FONT", raising=False)
    module = _load_subtitle_font_module()

    assert module.subtitle_font_name(system="Darwin") == "Arial Unicode MS"
    assert module.subtitle_font_name(system="Windows") == "Microsoft YaHei"
    assert module.subtitle_font_name(system="Linux") == "Noto Sans CJK SC"


def test_subtitle_font_accepts_safe_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LECTURECAST_SUBTITLE_FONT", "  Source Han Sans SC  ")
    module = _load_subtitle_font_module()

    assert module.subtitle_font_name(system="Darwin") == "Source Han Sans SC"


@pytest.mark.parametrize("value", ["Bad,Font", "Bad\nFont", "Bad\rFont"])
def test_subtitle_font_rejects_ass_field_injection(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LECTURECAST_SUBTITLE_FONT", value)
    module = _load_subtitle_font_module()

    with pytest.raises(ValueError, match="single ASS font family"):
        module.subtitle_font_name()


def test_manifest_subtitle_generator_uses_override(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    environment = os.environ.copy()
    environment["LECTURECAST_SUBTITLE_FONT"] = "Source Han Sans SC"

    subprocess.run(
        [
            sys.executable,
            str(SHARED / "build_manifest_subtitles.py"),
            str(ROOT / "tests" / "fixtures" / "production-manifest-v1.json"),
            str(output_dir),
        ],
        check=True,
        env=environment,
    )

    for filename in ("subtitle_landscape.ass", "subtitle_vertical.ass"):
        content = (output_dir / filename).read_text(encoding="utf-8")
        assert "Style: Default,Source Han Sans SC," in content
        assert "Microsoft YaHei" not in content


@pytest.mark.parametrize(
    ("script_name", "output_name"),
    [
        ("srt_to_ass.py", "subtitle.ass"),
        ("srt_to_ass_vertical.py", "subtitle_vertical.ass"),
    ],
)
def test_community_subtitle_generators_use_override(
    tmp_path: Path, script_name: str, output_name: str
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "subtitle.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n中文字幕验证\n",
        encoding="utf-8",
    )
    shutil.copy2(SHARED / "subtitle_font.py", tmp_path / "subtitle_font.py")
    shutil.copy2(SHARED / script_name, tmp_path / script_name)
    environment = os.environ.copy()
    environment["LECTURECAST_SUBTITLE_FONT"] = "Source Han Sans SC"

    subprocess.run([sys.executable, str(tmp_path / script_name)], check=True, env=environment)

    content = (assets / output_name).read_text(encoding="utf-8")
    assert "Style: Default,Source Han Sans SC," in content
    assert "中文字幕验证" in content


def test_all_official_ass_generators_use_shared_font_selection() -> None:
    for filename in (
        "srt_to_ass.py",
        "srt_to_ass_vertical.py",
        "build_manifest_subtitles.py",
    ):
        content = (SHARED / filename).read_text(encoding="utf-8")
        assert "subtitle_font_name()" in content
        assert "Style: Default,Microsoft YaHei," not in content
