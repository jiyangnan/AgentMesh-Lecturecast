"""M2 CLI consumption (§2.6 m2-6c): the `director generation-presenter-plan`
command — approval gate, capability gathering, create→save→verify, recovery
catalog persistence, and idempotent re-run. The signature verification path is
exercised for real (a per-test keyring with a known private key is installed as
the default so `save_presenter_plan`'s verify call passes)."""

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
from lecturecast.protocol import PresenterPlanV1_1, manifest_signing_bytes

FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()
NOW = "2026-08-04T12:00:00Z"
NOW_DT = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
PLACEHOLDER = "A" * 86 + "=="

# Digest chain in tests/fixtures/*-v1.json (canonical bytes, compact JSON).
FIXTURE_CAPS_DIGEST = "sha256:7c9a59784ea31fdc37c087c9b29eeee8257dcf461619d5503f7fcebfbfca4755"
FIXTURE_MANIFEST_DIGEST = "sha256:c4d3b972066c7b107bfdb7870c11eeaf03d6528af16d80c5e1a8cba0f543115d"


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


def _presenter_plan(*, key_id: str = "m2_cli_key_v1") -> dict:
    return {
        "schema_version": "1.1",
        "presenter_plan_id": "pp_cli_001",
        "generation_id": "gen_m2_cli",
        "production_manifest_digest": FIXTURE_MANIFEST_DIGEST,
        "brief_digest": "sha256:8ab66ff5434cf525d908e46eae5ff6c01f7ae5739f2659103dd6640ff68b6e14",
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
def m2_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a per-test keyring (known private key) as the DEFAULT keyring so
    `save_presenter_plan`'s keyring=None verify call passes for real. It also
    carries fixture_key_v1 so `_release_manifest_project`'s manifest verify
    (signed by fixture_key_v1) passes while the M2 keyring is installed."""
    key_id = "m2_cli_key_v1"
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    fixture_ring = json.loads((FIXTURE_DIR / "signing-keyring-v1.json").read_text(encoding="utf-8"))
    fixture_key_v1 = next(
        k for k in fixture_ring["keys"] if k["key_id"] == "fixture_key_v1"
    )
    # The per-test key must be the single "current" key; fixture_key_v1 is
    # demoted to "previous" (verify accepts current|previous) so the ring has
    # exactly one current key (PublicKeyRing invariant).
    fixture_key_v1 = {**fixture_key_v1, "status": "previous"}
    ring_path = tmp_path / "m2-cli-keyring.json"
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


class FakeM2Client:
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

    def create_presenter_plan(
        self, generation_id: str, *, capabilities: dict, approved: bool,
        protocol_version: str = "1.0",
    ) -> dict:
        self.calls.append({
            "generation_id": generation_id, "capabilities": capabilities,
            "approved": approved, "protocol_version": protocol_version,
        })
        return {
            "presenter_plan": self.plan,
            "billing": self.billing,
            "recovery_catalog": self.recovery_catalog,
        }


def _full_billing() -> list[dict]:
    return [
        {"milestone": "manifest", "artifact_type": "manifest", "cost": 10,
         "status": "charged", "artifact_digest": FIXTURE_MANIFEST_DIGEST,
         "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
        {"milestone": "presenter_plan", "artifact_type": "presenter_plan", "cost": 10,
         "status": "charged", "artifact_digest": "sha256:" + "b" * 64,
         "deducted_credits": 10, "last_error_code": None, "completed_at": NOW},
    ]


def _release_manifest_project(tmp_path: Path) -> Path:
    """init → save_capabilities(v1.0) → save_brief → save_manifest → director
    state (v1.1, confirmed, generation locked). Matches the real fixture digest
    chain (manifest binds caps 7c9a… / brief 8ab6…)."""
    from lecturecast.protocol import ClientCapabilities, CreativeBrief, ProductionManifest

    store = ProjectStore(tmp_path)
    store.init(name="M2 CLI")
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
        session={"session_id": "dir_m2_001", "status": "confirmed", "brief_version": 1,
                 "catalog_version": "cv", "updated_at": NOW},
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    dstore.update(state, generation_id="gen_m2_cli", generation_status="ready")
    return tmp_path


def _invoke(root: Path, *, yes: bool) -> object:
    argv = ["director", "generation-presenter-plan", str(root)]
    if yes:
        argv.append("--yes")
    argv.append("--json")
    return runner.invoke(app, argv)


def _install_v1_1_caps(project_store: ProjectStore) -> None:
    """Save a v1.1 capabilities snapshot claiming HeyGen configured (digest NOT
    bound to the manifest — the _stored_capabilities probe is monkeypatched so
    the stored file is bypassed; only the model is reused)."""
    project = project_store.load()
    project_store.save_capabilities(
        _fixture("client-capabilities-v1_1.json"), expected_revision=project.revision
    )


def _m2_caps_model():
    from lecturecast.protocol import ClientCapabilitiesV1_1

    return ClientCapabilitiesV1_1.model_validate(_fixture("client-capabilities-v1_1.json"))


def test_generation_presenter_plan_requires_yes_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """The M2 approval credential is a hard cost gate: without --yes the command
    must refuse (no create call, no plan persisted)."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: "photo"
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_capabilities",
        lambda *a, **k: _m2_caps_model(),
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_heygen_still_live", lambda *a, **k: True
    )

    result = _invoke(root, yes=False)

    assert result.exit_code != 0, result.output
    assert client.calls == []
    assert not (root / ".lecturecast" / "presenter-plan.json").exists()


def test_generation_presenter_plan_creates_and_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """With --yes the command creates the plan, persists it read-only with a
    digest, upgrades director state to v1.3 with the provider recovery catalog,
    and projects the post-M2 workflow (script review, NOT re-offer create)."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    catalog = {
        "catalog_version": "recovery_provider_v1", "directives": {
            "heygen_key_invalid": {
                "failure_kind": "heygen_key_invalid", "is_main_blocker": False,
                "user_message": "HeyGen key 无效", "steer_back_line": "…",
                "do_not": [], "options": [{
                    "option_id": "op1", "label": "更新 key", "recommended": True,
                }],
            },
        },
        "signature": {"algorithm": "Ed25519", "key_id": "x", "value": PLACEHOLDER},
    }
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=catalog,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: "photo"
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_capabilities",
        lambda *a, **k: _m2_caps_model(),
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_heygen_still_live", lambda *a, **k: True
    )

    result = _invoke(root, yes=True)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(client.calls) == 1
    assert client.calls[0]["approved"] is True
    assert client.calls[0]["capabilities"]["third_party_processors"][0]["configured"] is True
    # Plan persisted read-only with digest; project advanced.
    assert (root / ".lecturecast" / "presenter-plan.json").is_file()
    project = ProjectStore(root).load()
    assert project.payload["status"] == "presenter_plan_ready"
    assert project.payload["presenter_plan_digest"] == payload["project"]["presenter_plan_digest"]
    # v1.3 state: provider recovery catalog persisted.
    state = DirectorStateStore(root).load()
    assert state.payload["schema_version"] == "1.3"
    assert state.recovery_catalog["catalog_version"] == "recovery_provider_v1"
    # Workflow: M2 complete → script review (not a re-offer of create).
    assert payload["workflow"]["phase"] == "script_review_required"
    assert payload["workflow"]["next_action"]["id"] == "manifest.review"


def test_generation_presenter_plan_rejects_v1_0_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """M2 is v1.1-only: a v1.0 project must fail closed before any network call."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    dstore = DirectorStateStore(root)
    state = dstore.load()
    # Construct a genuine v1.0 state: schema_version=1.0 with the frozen 13-key
    # shape (no protocol_version key) — what create(protocol_version="1.0")
    # would have written.
    payload = dict(state.to_dict())
    payload.pop("protocol_version")
    payload["schema_version"] = "1.0"
    dstore.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = _invoke(root, yes=True)

    assert result.exit_code != 0
    assert client.calls == []
    assert "v1.1" in result.output or "1.1" in result.output


def test_generation_presenter_plan_rejects_non_photo_avatar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """M2 must not trigger for an own_voice (avatar=none) project."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: None
    )

    result = _invoke(root, yes=True)

    assert result.exit_code != 0
    assert client.calls == []


def test_generation_presenter_plan_idempotent_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """Re-running after a successful create must NOT call the server again
    (the persisted plan short-circuits) — no double charge at the CLI layer."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: "photo"
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_capabilities",
        lambda *a, **k: _m2_caps_model(),
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_heygen_still_live", lambda *a, **k: True
    )

    first = _invoke(root, yes=True)
    assert first.exit_code == 0, first.output
    assert len(client.calls) == 1

    second = _invoke(root, yes=True)
    assert second.exit_code == 0, second.output
    assert len(client.calls) == 1  # no new create call


def test_generation_presenter_plan_payload_omission_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m2_keyring,
) -> None:
    """Fail-closed at the upload boundary: a snapshot claiming a present-but-
    not-configured third_party_processor must NOT be forwarded to create."""
    key_id, private_key = m2_keyring
    root = _release_manifest_project(tmp_path)
    client = FakeM2Client(
        plan=_sign_plan(_presenter_plan(key_id=key_id), private_key),
        billing=_full_billing(), recovery_catalog=None,
    )
    monkeypatch.setattr("lecturecast.commands.director._make_client", lambda _url: client)
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_brief_avatar", lambda store: "photo"
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_capabilities",
        lambda *a, **k: _m2_caps_model(),
    )
    monkeypatch.setattr(
        "lecturecast.commands.director._stored_heygen_still_live", lambda *a, **k: True
    )
    # The snapshot CLAIMS heygen configured, but live liveness disagrees.
    monkeypatch.setattr(
        "lecturecast.commands.director._d13_heygen_configured", lambda caps: False
    )

    result = _invoke(root, yes=True)

    assert result.exit_code != 0
    assert client.calls == []
