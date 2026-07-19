from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from lecturecast.capabilities import capture_capabilities, doctor_report


def _write_remotion_package(root: Path, version: str, *, template: bool = False) -> None:
    prefix = root / "templates" if template else root
    package = prefix / "remotion" / "node_modules" / "remotion" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": version}), encoding="utf-8")


def _runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    argv = list(command)
    if argv == ["node", "--version"]:
        output = "v22.23.1\n"
    elif argv == ["ffmpeg", "-version"]:
        output = "ffmpeg version 8.1.2\n"
    elif argv == ["ffmpeg", "-buildconf"]:
        output = "configuration: --enable-libass\n"
    elif argv == ["ffmpeg", "-hide_banner", "-filters"]:
        output = " .. ass               V->V       Render ASS subtitles\n"
    else:
        raise AssertionError(f"unexpected command: {argv}")
    return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")


def test_project_remotion_runtime_takes_precedence_over_package_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "episode"
    repo = tmp_path / "checkout"
    _write_remotion_package(project, "4.0.479")
    _write_remotion_package(repo, "4.0.100", template=True)
    monkeypatch.setattr(
        "lecturecast.capabilities.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"node", "ffmpeg"} else None,
    )

    capabilities = capture_capabilities(
        project_root=project,
        repo_root=repo,
        runner=_runner,
    )
    runtime = capabilities.model_dump()["runtime"]

    assert runtime["remotion_version"] == "4.0.479"
    assert runtime["has_libass"] is True
    assert runtime["can_render_locally"] is True


def test_libass_requires_a_real_subtitle_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_remotion_package(tmp_path, "4.0.479")
    monkeypatch.setattr(
        "lecturecast.capabilities.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in {"node", "ffmpeg"} else None,
    )

    def no_filter_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = _runner(command)
        if list(command) == ["ffmpeg", "-hide_banner", "-filters"]:
            return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")
        return result

    capabilities = capture_capabilities(project_root=tmp_path, runner=no_filter_runner)
    report = doctor_report(capabilities)

    assert report["ready"] is False
    assert "ffmpeg-libass" in report["missing"]
    assert any("ffmpeg-full" in action for action in report["next_actions"])
