"""M3 CLI consumption (§2.6 m3-6c): the `director generation-orchestration-plan`
command — NO approval gate (裁决 B), M3 applicability gate, photo→M2-charged
precondition, create→save→verify, and idempotent re-run. The signature
verification path is exercised for real (a per-test keyring with a known private
key is installed as the default so `save_orchestration_plan`'s verify call
passes)."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.director import DirectorStateStore
from lecturecast.project import ProjectStore
from lecturecast.protocol import manifest_signing_bytes

FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()
NOW = "2026-08-04T12:00:00Z"
NOW_DT = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
PLACEHOLDER = "A" * 86 + "=="

# Digest chain in tests/fixtures/*-v1.json (canonical bytes, compact JSON).
FIXTURE_CAPS_DIGEST = "sha256:7c9a59784ea31fdc37c087c9b29eeee8257dcf461619d5503f7fcebfbfca4755"
FIXTURE_MANIFEST_DIGEST = "sha256:c4d3b972066c7b107bfdb7870c11eeaf03d6528af16d80c5e1a8cba0f543115d"
FIXTURE_BRIEF_DIGEST = "sha256:8ab66ff5434cf525d908e46eae5ff6c01f7ae5739f2659103dd6640ff68b6e14"


@pytest.fixture(autouse=True)
def allow_commercial_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.director.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.director.require_project_host_workflow",
        lambda *a, **kw: None,
    )


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _orchestration_plan(*, key_id: str = "m3_cli_key_v1") -> dict:
    return {
        "schema_version": "1.1",
        "orchestration_plan_id": "op_cli_001",
        "generation_id": "gen_m3_cli",
        "production_manifest_digest": FIXTURE_MANIFEST_DIGEST,
        "brief_digest": FIXTURE_BRIEF_DIGEST,
        "capability_digest": FIXTURE_CAPS_DIGEST,
        "component_catalog_digest": "sha256:" + "e" * 64,
        "presenter_plan_digest": None,
        "bgm_enabled": False,
        "bgm_genre": "none",
        "speed": 1.25,
        "ffmpeg_overlay_template_id": "overlay.template.v1",
        "timing_placeholder_contract": "{scenes[0].total_frames}",
        "voice_orchestration": {
            "ref_audio_uri": "asset://voice/ref.wav",
            "ref_text": "示例参考文本",
            "device": "mps",
            "seed": 42,
            "nfe_step": 16,
            "chunk_split_regex": "[。！？]",
            "gap_between_chunks_s": 0.1,
            "sample_rate": 24000,
        },
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
        "created_at": NOW,
        "content_expires_at": (NOW_DT + timedelta(days=30)).isoformat(),
    }


def _sign_plan(plan: dict, private_key: Ed25519PrivateKey) -> dict:
    signed = copy.deepcopy(plan)
    signed["signature"] = {
        "algorithm": "Ed25519",
        "key_id": signed["signature"]["key_id"],
        "value": "",
    }
    signature = private_key.sign(manifest_signing_bytes(signed))
    signed["signature"]["value"] = base64.b64encode(signature).decode()
    return signed


@pytest.fixture
def m3_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a per-test keyring (known private key) as the DEFAULT keyring so
    `save_orchestration_plan`'s keyring=None verify call passes for real. It also
    carries fixture_key_v1 so `_release_manifest_project`'s manifest verify
    (signed by fixture_key_v1) passes while the M3 keyring is installed."""
    key_id = "m3_cli_key_v1"
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    fixture_ring = json.loads((FIXTURE_DIR / "signing-keyring-v1.json").read_text(encoding="utf-8"))
    fixture_key_v1 = next(
        k for k in fixture_ring["keys"] if k["key_id"] == "fixture_key_v1"
    )
    fixture_key_v1 = {**fixture_key_v1, "status": "previous"}
    ring_path = tmp_path / "m3-cli-keyring.json"
    ring_path.write_text(
        json.dumps({
            "keyring_version": "1.0",
            "keys": [
                {
                    "key_id": key_id, "algorithm": "Ed25519", "public_key": public_key,
                    "status": "current",
                    "not_before": "2026-01-01T00:00:00Z",
                    "not_after": "2030-01-01T00:00:00Z",
                },
                fixture_key_v1,
            ],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("lecturecast.manifest.KEYRING_PATH", ring_path)
    return key_id, private_key


class FakeM3Client:
    def __init__(
        self,
        *,
        plan: dict,
        billing: list[dict],
        recovery_catalog: dict | None,
    ) -> None:
        self.plan = plan
        self.billing = billing
        self.recovery_catalog = recovery_catalog
        self.calls: list[dict] = []

    def create_orchestration_plan(
        self, generation_id: str, *, capabilities: dict, protocol_version: str = "1.0",
    ) -> dict:
        self.calls.append({
            "generation_id": generation_id, "capabilities": capabilities,
            "protocol_version": protocol_version,
        })
        return {
            "orchestration_plan": self.plan,
            "billing": self.billing,
            "recovery_catalog": self.recovery_catalog,
        }


def _full_billing() -> list[dict]:
    return [
        {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
         "status": "charged", "artifact_digest": FIXTURE_MANIFEST_DIGEST,
         "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
        {"milestone": "orchestration", "artifact_type": "orchestration_plan", "cost": 10,
         "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
         "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
    ]


def _release_manifest_project(tmp_path: Path) -> Path:
    """init → save_capabilities(v1.0) → save_brief → save_manifest → director
    state (v1.1, confirmed, generation locked). Matches the real fixture digest
    chain (manifest binds caps 7c9a… / brief 8ab6…)."""
    from lecturecast.protocol import ClientCapabilities, CreativeBrief, ProductionManifest

    store = ProjectStore(tmp_path)
    store.init(name="M3 CLI")
    state = store.save_capabilities(
        ClientCapabilities.model_validate(_fixture("client-capabilities-v1.json")),
        expected_revision=store.load().revision,
    )
    state = store.save_brief(
        CreativeBrief.model_validate(_fixture("creative-brief-v1.json")),
        expected_revision=state.revision,
    )
    store.save_manifest(
        ProductionManifest.model_validate(_fixture("production-manifest-v1.json")),
        expected_revision=state.revision,
    )
    dstore = DirectorStateStore(tmp_path)
    state = dstore.create(
        server_url="https://director.example.test",
        session={"session_id": "dir_m3_001", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    dstore.update(state, generation_id="gen_m3_cli", generation_status="ready")
    return tmp_path


def _invoke(root: Path) -> object:
    # M3 has NO --yes flag (裁决 B). Only the M3-appropriate flags exist.
    return runner.invoke(app, ["director", "generation-orchestration-plan", str(root), "--json"])


def _install_v1_1_caps(project_store: ProjectStore) -> None:
    project = project_store.load()
    project_store.save_capabilities(
        _fixture("client-capabilities-v1_1.json"), expected_revision=project.revision
    )


def _m3_caps_model():
    from lecturecast.protocol import ClientCapabilitiesV1_1

    return ClientCapabilitiesV1_1.model_validate(_fixture("client-capabilities-v1_1.json"))


def _setup_m3_applicable(
    monkeypatch: pytest.MonkeyPatch, *, applicable: bool = True, avatar: str | None = "photo",
) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.director._brief_m3_applicable",
        lambda store: applicable,
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: avatar
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_capabilities",
        lambda *a, **k: _m3_caps_model(),
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_heygen_still_live", lambda *a, **k: True
    )


def test_generation_orchestration_plan_creates_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """own_voice path (no M2): the command creates the M3 plan, persists it
    read-only with a digest, upgrades director state, and projects the post-M3
    workflow (script review). No approval credential is sent."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=True, avatar=None)

    result = _invoke(root)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(client.calls) == 1
    # No approval credential in the M3 call (裁决 B).
    assert "approval" not in client.calls[0]
    # Plan persisted read-only with digest; project advanced.
    assert (root / ".lecturecast" / "orchestration-plan.json").is_file()
    project = ProjectStore(root).load()
    assert project.payload["status"] == "orchestration_plan_ready"
    assert project.payload["orchestration_plan_digest"] == payload["project"]["orchestration_plan_digest"]
    # Workflow: M3 complete → script review (not a re-offer of create).
    assert payload["workflow"]["phase"] == "script_review_required"
    assert payload["workflow"]["next_action"]["id"] == "manifest.review"


def test_generation_orchestration_plan_photo_requires_m2_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """gate ③: a photo-avatar project whose M2 presenter plan is NOT persisted
    must refuse before any network call (m3_not_ready)."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=True, avatar="photo")

    result = _invoke(root)

    assert result.exit_code != 0
    assert client.calls == []
    assert not (root / ".lecturecast" / "orchestration-plan.json").exists()
    assert "m3_not_ready" in result.output or "M3" in result.output


def test_generation_orchestration_plan_photo_with_m2_persisted_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """photo path after M2: a project whose presenter plan is persisted
    (status=presenter_plan_ready) must reach the M3 create — the manifest_ready
    gate must accept the post-M2 state, and gate ③ (M2 charged) must pass."""
    from lecturecast.protocol import PresenterPlanV1_1

    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    # Persist a real M2 presenter plan (signed with the same per-test key) so the
    # project advances to presenter_plan_ready and gate ③ passes.
    presenter = {
        "schema_version": "1.1",
        "presenter_plan_id": "pp_photo_001",
        "generation_id": "gen_m3_cli",
        "production_manifest_digest": FIXTURE_MANIFEST_DIGEST,
        "brief_digest": FIXTURE_BRIEF_DIGEST,
        "capability_digest": FIXTURE_CAPS_DIGEST,
        "component_catalog_digest": "sha256:" + "e" * 64,
        "avatar": "photo",
        "presenter_mode": "three_segment",
        "segments": [{"segment_id": "seg_open", "script_chunk_ids": [0], "label": "开场"}],
        "pip_style": {
            "size_px": 320, "corner_radius_px": 16,
            "position": "bottom-right", "margin_right_px": 24, "margin_bottom_px": 24,
        },
        "heygen": {
            "base_url": "https://api.heygen.com",
            "auth_header": "X-Api-Key",
            "key_source": "user_provided_local",
            "key_env_var": "HEYGEN_API_KEY",
            "assets_endpoint": "/v3/assets",
            "videos_endpoint": "/v3/videos",
            "asset_id_field": "data.asset_id",
            "status_field": "data.status",
            "url_field": "data.video_url",
            "poll_interval_s": 15,
            "poll_max_attempts": 24,
        },
        "signature": {"algorithm": "Ed25519", "key_id": key_id, "value": PLACEHOLDER},
        "created_at": NOW,
        "content_expires_at": (NOW_DT + timedelta(days=30)).isoformat(),
    }
    signed_presenter = _sign_plan(presenter, private_key)
    store = ProjectStore(root)
    project = store.load()
    store.save_presenter_plan(
        PresenterPlanV1_1.model_validate(signed_presenter),
        expected_revision=project.revision,
    )
    assert store.load().payload["status"] == "presenter_plan_ready"

    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=True, avatar="photo")

    result = _invoke(root)

    assert result.exit_code == 0, result.output
    assert len(client.calls) == 1
    assert (root / ".lecturecast" / "orchestration-plan.json").is_file()
    assert ProjectStore(root).load().payload["status"] == "orchestration_plan_ready"


def test_generation_orchestration_plan_rejects_non_applicable_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """A pure-M1 brief (none avatar + stock voice + no bgm) must fail closed —
    no M3 create for a project that doesn't need orchestration."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=False, avatar=None)

    result = _invoke(root)

    assert result.exit_code != 0
    assert client.calls == []
    assert not (root / ".lecturecast" / "orchestration-plan.json").exists()


def test_generation_orchestration_plan_rejects_v1_0_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """M3 is v1.1-only: a v1.0 project must fail closed before any network call."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    dstore = DirectorStateStore(root)
    state = dstore.load()
    payload = dict(state.to_dict())
    payload.pop("protocol_version")
    payload["schema_version"] = "1.0"
    dstore.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _invoke(root)

    assert result.exit_code != 0
    assert client.calls == []
    assert "1.1" in result.output


def test_generation_orchestration_plan_idempotent_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """Re-running after a successful create must NOT call the server again
    (the persisted plan short-circuits) — no double charge at the CLI layer."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=True, avatar=None)

    first = _invoke(root)
    assert first.exit_code == 0, first.output
    assert len(client.calls) == 1

    second = _invoke(root)
    assert second.exit_code == 0, second.output
    assert len(client.calls) == 1  # no new create call


def test_generation_orchestration_plan_persists_recovery_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m3_keyring,
) -> None:
    """The M3 create response's base recovery catalog must be persisted into the
    v1.3 state so M3-context resume errors present the base directives."""
    key_id, private_key = m3_keyring
    root = _release_manifest_project(tmp_path)
    catalog = {
        "catalog_version": "recovery_base_v1", "directives": {
            "m1_insufficient_credits": {
                "failure_kind": "m1_insufficient_credits", "is_main_blocker": True,
                "user_message": "额度不足", "steer_back_line": "…",
                "do_not": [], "options": [{
                    "option_id": "op1", "label": "充值", "recommended": True,
                }],
            },
        },
        "signature": {"algorithm": "Ed25519", "key_id": "x", "value": PLACEHOLDER},
    }
    client = FakeM3Client(
        plan=_sign_plan(_orchestration_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=catalog,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    _setup_m3_applicable(monkeypatch, applicable=True, avatar=None)

    result = _invoke(root)

    assert result.exit_code == 0, result.output
    state = DirectorStateStore(root).load()
    assert state.payload["schema_version"] == "1.3"
    assert state.recovery_catalog["catalog_version"] == "recovery_base_v1"
