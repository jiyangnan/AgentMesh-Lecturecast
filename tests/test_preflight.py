from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lecturecast.capabilities import capture_capabilities
from lecturecast.errors import LectureCastError
from lecturecast.preflight import run_preflight


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_signed_fixture_passes_preflight_with_bound_capabilities() -> None:
    result = run_preflight(
        _fixture("production-manifest-v1.json"),
        _fixture("client-capabilities-v1.json"),
    )

    assert result.passed
    assert all(check.passed for check in result.checks)


def test_preflight_rejects_incompatible_components() -> None:
    capabilities = copy.deepcopy(_fixture("client-capabilities-v1.json"))
    capabilities["components"].remove("product.ui_focus.v1")

    with pytest.raises(LectureCastError) as captured:
        run_preflight(_fixture("production-manifest-v1.json"), capabilities)

    assert captured.value.code == "manifest_incompatible"
    assert "capability_digest" in captured.value.message
    assert "components" in captured.value.message


def test_capability_capture_can_truthfully_report_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lecturecast.capabilities.shutil.which", lambda _command: None)

    capabilities = capture_capabilities(repo_root=tmp_path)
    payload = capabilities.model_dump()

    assert len(payload["components"]) == 11
    assert payload["runtime"]["can_render_locally"] is False
    assert payload["runtime"]["node_version"] is None
    assert payload["runtime"]["ffmpeg_version"] is None
