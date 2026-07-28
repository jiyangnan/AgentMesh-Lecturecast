"""V1.1 capability capture (§5.5b): F5 + HeyGen BYO detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lecturecast.capabilities import (
    F5_MODEL_PATH_ENV,
    HEYGEN_API_KEY_ENV,
    capture_capabilities_v1_1,
    f5_available,
    heygen_processor,
)
from lecturecast.protocol import ClientCapabilitiesV1_1


# ---- f5_available (presence-only, injectable probe) ----

def test_f5_absent_when_no_model_path() -> None:
    assert f5_available(env={}) is False


def test_f5_absent_when_model_missing(tmp_path: Path) -> None:
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(tmp_path / "nope.pth")}, path_probe=Path
    ) is False


def test_f5_present_when_model_file_exists(tmp_path: Path) -> None:
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(env={F5_MODEL_PATH_ENV: str(model)}, path_probe=Path) is True


def test_f5_blank_path_treated_as_absent() -> None:
    assert f5_available(env={F5_MODEL_PATH_ENV: "   "}) is False


# ---- heygen_processor (configured presence, NO key uploaded) ----

def test_heygen_none_when_no_key() -> None:
    assert heygen_processor(env={}) is None


def test_heygen_none_when_blank_key() -> None:
    assert heygen_processor(env={HEYGEN_API_KEY_ENV: "  "}) is None


def test_heygen_declared_when_key_present_no_secret_uploaded() -> None:
    proc = heygen_processor(env={HEYGEN_API_KEY_ENV: "sk_secret_value"})
    assert proc == {
        "provider": "heygen",
        "api_version": "v3",
        "configured": True,
        "credential_mode": "byo_local",
        "operations": ["direct_asset_upload", "photo_avatar"],
        "features": ["idempotency_24h"],
    }
    # The API key is never echoed into the declared capability.
    dumped = repr(proc)
    assert "sk_secret_value" not in dumped
    assert "verified" not in proc  # no verified field — preflight stays local


# ---- capture_capabilities_v1_1 end-to-end (mocked probes) ----

def _probe_runner(args: Any) -> Any:
    """A stand-in runner that reports the local toolchain as present so the v1.0
    base payload is complete without a real node/ffmpeg install."""
    import subprocess

    return subprocess.CompletedProcess(args=args, returncode=0, stdout="1.0.0", stderr="")


def test_capture_v1_1_without_f5_or_heygen(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
    )
    assert isinstance(caps, ClientCapabilitiesV1_1)
    payload = caps.model_dump()
    assert payload["schema_version"] == "1.1"
    assert "f5" not in payload["tts_engines"]
    assert payload.get("third_party_processors") in (None, [])
    assert payload["supported_artifact_versions"]["presenter_plan"] == ["1.1"]


def test_capture_v1_1_with_f5_and_heygen(tmp_path: Path) -> None:
    model = tmp_path / "f5.pth"
    model.write_bytes(b"x")
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={F5_MODEL_PATH_ENV: str(model), HEYGEN_API_KEY_ENV: "sk_live"},
        path_probe=Path,
    )
    payload = caps.model_dump()
    assert "f5" in payload["tts_engines"]
    assert payload["third_party_processors"][0]["provider"] == "heygen"
    assert payload["third_party_processors"][0]["configured"] is True
    # No secret leaks into the captured capability blob.
    assert "sk_live" not in repr(payload)
