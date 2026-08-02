"""V1.1 capability capture (§5.5b): F5 + HeyGen BYO detection.

F5 / HeyGen detection is FAIL-CLOSED until the adapters + journal ship in
§5.5e: the default runtime/adapter/journal probes return False, so production
never claims an unexecutable capability (which the server would bill M2/M3 on).
Tests inject True probes to exercise the declaration logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lecturecast.capabilities import (
    F5_MODEL_PATH_ENV,
    HEYGEN_API_KEY_ENV,
    capture_capabilities_v1_1,
    default_heygen_adapter_probe,
    default_heygen_journal_probe,
    f5_available,
    heygen_processor,
)
from lecturecast.protocol import ClientCapabilitiesV1_1


_TRUE = lambda: True  # noqa: E731


# ---- f5_available (fail-closed without the runtime probe) ----

def test_f5_absent_when_no_model_path() -> None:
    assert f5_available(env={}, runtime_probe=_TRUE) is False


def test_f5_fail_closed_without_runtime_even_with_model(tmp_path: Path) -> None:
    """A model file alone is NOT enough — the F5 runtime/adapter probe must
    pass. Default probe fails closed (no adapter shipped yet)."""
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(env={F5_MODEL_PATH_ENV: str(model)}) is False  # default probe


def test_f5_present_only_when_model_and_runtime(tmp_path: Path) -> None:
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(model)}, runtime_probe=_TRUE,
    ) is True


def test_f5_absent_when_model_missing_even_with_runtime(tmp_path: Path) -> None:
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(tmp_path / "nope.pth")}, runtime_probe=_TRUE,
    ) is False


# ---- heygen_processor (fail-closed without adapter + journal) ----

def test_heygen_none_when_no_key() -> None:
    assert heygen_processor(env={}, adapter_probe=_TRUE, journal_probe=_TRUE) is None


def test_heygen_fail_closed_without_adapter_and_journal() -> None:
    """A key alone is NOT enough — adapter + journal probes must pass. Defaults
    fail closed (neither shipped yet)."""
    assert heygen_processor(env={HEYGEN_API_KEY_ENV: "sk"}) is None


def test_heygen_declared_when_key_adapter_journal_no_secret_uploaded() -> None:
    proc = heygen_processor(
        env={HEYGEN_API_KEY_ENV: "sk_secret_value"},
        adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    assert proc == {
        "provider": "heygen",
        "api_version": "v3",
        "configured": True,
        "credential_mode": "byo_local",
        # §5.5e5c: operations reflect locked primitives — asset_delete/video_delete
        # added (DeletionCoordinator + adapters shipped); avatar_delete omitted
        # (no dedicated primitive). features add title_query (query_videos_by_title)
        # + read_only_auth_check (get_asset docstring "doctor only" backed).
        "operations": [
            "direct_asset_upload",
            "photo_avatar",
            "prerecorded_audio_lipsync",
            "asset_delete",
            "video_delete",
        ],
        "features": ["idempotency_24h", "title_query", "read_only_auth_check"],
    }
    assert "sk_secret_value" not in repr(proc)
    assert "verified" not in proc


# ---- §5.5e5c real probes (production wiring: director.py + project.py) ----

def test_default_adapter_probe_imports_shipped_modules() -> None:
    """The real adapter probe returns True: the three HeyGen adapter modules +
    key classes are shipped (§5.5e5 locked)."""
    assert default_heygen_adapter_probe() is True


def test_default_adapter_probe_fail_closed_on_missing_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a shipped module is missing the expected class, the probe fails closed
    (module importability alone is NOT enough — §3.1 over-report guard)."""
    import lecturecast.heygen_asset_adapter as mod

    monkeypatch.setattr(mod, "HeyGenAssetAdapter", None)
    assert default_heygen_adapter_probe() is False


def _init_test_db(tmp_path: Path, head: int | None = None) -> Path:
    """Create a real (empty) SQLite journal DB at the expected path so the
    read-only probe can open it; optionally set PRAGMA user_version."""
    import sqlite3

    runtime = tmp_path / ".lecturecast" / "runtime"
    runtime.mkdir(parents=True)
    db = runtime / "heygen-operations.db"
    conn = sqlite3.connect(str(db))
    if head is not None:
        conn.executescript(f"PRAGMA user_version = {head};")
    conn.close()
    return db


def test_default_journal_probe_missing_db_is_ready(tmp_path: Path) -> None:
    """A not-yet-initialized journal is ready — the first op auto-inits via
    init_database (locked, idempotent). The probe is read-only (never creates)."""
    assert default_heygen_journal_probe(tmp_path) is True
    assert not (tmp_path / ".lecturecast" / "runtime" / "heygen-operations.db").exists()


def test_default_journal_probe_head_current_is_ready(tmp_path: Path) -> None:
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _init_test_db(tmp_path, head=_SCHEMA_VERSION)
    assert default_heygen_journal_probe(tmp_path) is True


def test_default_journal_probe_head_below_is_ready(tmp_path: Path) -> None:
    """head < _SCHEMA_VERSION is ready — init_database auto-migrates on first op."""
    _init_test_db(tmp_path, head=3)
    assert default_heygen_journal_probe(tmp_path) is True


def test_default_journal_probe_head_ahead_is_not_ready(tmp_path: Path) -> None:
    """head > _SCHEMA_VERSION is refuse-downgrade (genuinely incompatible) → False."""
    from lecturecast.heygen_journal import _SCHEMA_VERSION

    _init_test_db(tmp_path, head=_SCHEMA_VERSION + 1)
    assert default_heygen_journal_probe(tmp_path) is False


def test_m1_independent_of_heygen_config() -> None:
    """§5.5e5c C7 / spec §2.6 line 489: with no HEYGEN_API_KEY, third_party_processors
    is absent even when the real adapter + journal probes pass — the key is gated
    inside heygen_processor. The v1.1 capability still captures + round-trips,
    so the M1 base path (edge-tts, no avatar) is unaffected by HeyGen config."""
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
        adapter_probe=default_heygen_adapter_probe,
        journal_probe=lambda: default_heygen_journal_probe(Path("/tmp")),
    )
    payload = caps.model_dump()
    assert payload.get("third_party_processors") in (None, [])
    assert payload["schema_version"] == "1.1"


# ---- capture_capabilities_v1_1 end-to-end (fail-closed by default) ----

def _probe_runner(args: Any) -> Any:
    import subprocess

    return subprocess.CompletedProcess(args=args, returncode=0, stdout="1.0.0", stderr="")


def test_capture_v1_1_fail_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no real F5/HeyGen adapters shipped, capture reports NEITHER — the
    server must not bill M2/M3 on an unexecutable capability."""
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={F5_MODEL_PATH_ENV: "/tmp/f5.pth", HEYGEN_API_KEY_ENV: "sk"}, path_probe=Path,
    )
    payload = caps.model_dump()
    assert isinstance(caps, ClientCapabilitiesV1_1)
    assert "f5" not in payload["tts_engines"]
    assert payload.get("third_party_processors") in (None, [])


def test_capture_v1_1_reports_f5_and_heygen_when_probes_pass(tmp_path: Path) -> None:
    model = tmp_path / "f5.pth"
    model.write_bytes(b"x")
    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={F5_MODEL_PATH_ENV: str(model), HEYGEN_API_KEY_ENV: "sk_live"},
        path_probe=Path, runtime_probe=_TRUE, adapter_probe=_TRUE, journal_probe=_TRUE,
    )
    payload = caps.model_dump()
    assert "f5" in payload["tts_engines"]
    assert payload["third_party_processors"][0]["provider"] == "heygen"
    assert payload["third_party_processors"][0]["configured"] is True
    assert "sk_live" not in repr(payload)


def test_v1_1_capabilities_round_trip_through_dispatcher() -> None:
    """A saved v1.1 capability reloads via parse_client_capabilities (not the
    v1.0 strict schema)."""
    from lecturecast.protocol import parse_client_capabilities

    caps = capture_capabilities_v1_1(
        adapter_kind="codex", adapter_version="1.0.0", runner=_probe_runner,
        env={}, path_probe=Path,
    )
    payload = caps.model_dump()
    reloaded = parse_client_capabilities(payload)
    assert isinstance(reloaded, ClientCapabilitiesV1_1)
    assert reloaded.model_dump()["schema_version"] == "1.1"


def test_f5_fail_closed_when_file_unreadable(tmp_path: Path) -> None:
    """An unreadable model file must fail closed. Use the injected
    readable_probe (portable across platforms) rather than chmod 000."""
    model = tmp_path / "f5_model.pth"
    model.write_bytes(b"x")
    assert f5_available(
        env={F5_MODEL_PATH_ENV: str(model)},
        runtime_probe=_TRUE,
        readable_probe=lambda _: False,
    ) is False


def test_stored_capabilities_version_mismatch_returns_none(tmp_path: Path) -> None:
    """_stored_capabilities returns None when the saved caps' schema_version
    doesn't match the requested protocol_version (forcing re-capture)."""
    import json

    from lecturecast.commands.director import _stored_capabilities
    from lecturecast.project import ProjectStore

    store = ProjectStore(tmp_path)
    store.init(name="T")
    v10_caps = json.loads(
        (Path(__file__).parent / "fixtures" / "client-capabilities-v1.json").read_text()
    )
    from lecturecast.protocol import ClientCapabilities
    store.save_capabilities(ClientCapabilities.model_validate(v10_caps), expected_revision=1)

    # Requesting v1.1 with stored v1.0 caps → None (re-capture).
    adapter = v10_caps["adapter"]
    result = _stored_capabilities(
        store, adapter_kind=adapter["kind"], adapter_version=adapter["version"],
        protocol_version="1.1",
    )
    assert result is None

    # Requesting v1.0 with stored v1.0 caps → returns the document.
    result_v10 = _stored_capabilities(
        store, adapter_kind=adapter["kind"], adapter_version=adapter["version"],
        protocol_version="1.0",
    )
    assert result_v10 is not None
    assert result_v10.model_dump()["schema_version"] == "1.0"


def test_v1_1_brief_projectstore_round_trip(tmp_path: Path) -> None:
    """Real ProjectStore durable round-trip: init → save_brief(V1.1)
    → new instance → load → verify CreativeBriefV1_1 + digest match."""
    import json

    from lecturecast.protocol import CreativeBriefV1_1, canonical_digest, parse_creative_brief
    from lecturecast.project import ProjectStore

    brief_payload = json.loads(
        (Path(__file__).parent / "fixtures" / "creative-brief-v1_1.json").read_text()
    )
    brief = CreativeBriefV1_1.model_validate(brief_payload)

    store = ProjectStore(tmp_path)
    store.init(name="T")
    store.save_brief(brief, expected_revision=1)

    # New store instance reads from disk.
    store2 = ProjectStore(tmp_path)
    loaded = parse_creative_brief(
        json.loads(store2.brief_path.read_text(encoding="utf-8"))
    )
    assert isinstance(loaded, CreativeBriefV1_1)
    assert loaded.model_dump()["schema_version"] == "1.1"
    assert canonical_digest(loaded) == canonical_digest(brief)
