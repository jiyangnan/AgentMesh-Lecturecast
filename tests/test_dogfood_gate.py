from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from typer.testing import CliRunner

from lecturecast import manifest as manifest_module
from lecturecast import dogfood as dogfood_module
from lecturecast.cli import app
from lecturecast.director import DirectorStateStore
from lecturecast.dogfood import (
    begin_dogfood,
    build_dogfood_receipt,
    capture_render_evidence,
    evaluate_dogfood_gate,
    load_dogfood_session,
    record_event_if_active,
    require_fresh_task_if_active,
    require_interaction_mode_if_active,
    verify_release_binding,
    write_dogfood_receipt,
)
from lecturecast.errors import LectureCastError
from lecturecast.manifest import PublicKeyRing, SigningKey
from lecturecast.project import ProjectStore
from lecturecast.protocol import (
    ClientCapabilities,
    canonical_digest,
    manifest_signing_bytes,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
NOW = "2026-07-15T12:00:00Z"
runner = CliRunner()


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _production_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: str,
    suffix: str,
    total_frames: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], PublicKeyRing, Ed25519PrivateKey]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    capabilities = _fixture("client-capabilities-v1.json")
    capabilities["capabilities_id"] = f"caps_{suffix}"
    capabilities["adapter"] = {"kind": adapter, "version": "1.0.0"}
    capability_digest = canonical_digest(ClientCapabilities.model_validate(capabilities))
    document = _fixture("production-manifest-v1.json")
    document["manifest_id"] = f"manifest_{suffix}"
    document["generation_id"] = f"generation_{suffix}"
    document["capability_digest"] = capability_digest
    if total_frames is not None:
        document["total_frames"] = total_frames
        document["script"] = [document["script"][0]]
        document["script"][0].update({"start_frame": 0, "duration_frames": total_frames})
        document["scenes"] = [document["scenes"][0]]
        document["scenes"][0].update({"start_frame": 0, "duration_frames": total_frames})
    private_key = Ed25519PrivateKey.generate()
    key_id = "lecturecast-prod-e2e-v1"
    document["signature"] = {"algorithm": "Ed25519", "key_id": key_id, "value": ""}
    document["signature"]["value"] = base64.b64encode(
        private_key.sign(manifest_signing_bytes(document))
    ).decode()
    created_at = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    key = SigningKey(
        key_id=key_id,
        algorithm="Ed25519",
        public_key=base64.b64encode(
            private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode(),
        status="current",
        not_before=(created_at - timedelta(days=365)).isoformat(),
        not_after=(created_at + timedelta(days=365)).isoformat(),
    )
    keyring = PublicKeyRing([key])
    keyring_path = tmp_path / f"keyring-{suffix}.json"
    keyring_path.write_text(json.dumps(keyring.to_dict()), encoding="utf-8")
    monkeypatch.setattr(manifest_module, "KEYRING_PATH", keyring_path)
    return capabilities, document, keyring, private_key


def _release_files(
    root: Path,
    *,
    keyring: PublicKeyRing,
    private_key: Ed25519PrivateKey,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    wheel = root / "lecturecast-0.3.0-py3-none-any.whl"
    package_root = Path(dogfood_module.__file__).resolve().parent
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for candidate in sorted(package_root.rglob("*")):
            if candidate.is_file() and candidate.suffix in {".py", ".json"}:
                relative = candidate.relative_to(package_root).as_posix()
                archive.write(candidate, f"lecturecast/{relative}")
        archive.writestr(
            "lecturecast-0.3.0.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: lecturecast\nVersion: 0.3.0\n",
        )
    wheel_digest = f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    current = datetime.now(UTC)
    published = current - timedelta(days=8)
    key = keyring.keys[0]
    evidence = {
        "schema_version": "signing-public-first-check.v1",
        "ready": True,
        "key_id": key.key_id,
        "fingerprint": PublicKeyRing.public_key_fingerprint(key),
        "checked_at": current.isoformat().replace("+00:00", "Z"),
        "minimum_publication_lead_days": 7,
        "publication_lead_seconds": int((current - published).total_seconds()),
        "key_window": {
            "not_before": key.not_before,
            "not_after": key.not_after,
        },
        "public_release": {
            "package": "lecturecast",
            "version": "0.3.0",
            "commit": "a" * 40,
            "published_at": published.isoformat().replace("+00:00", "Z"),
            "wheel_sha256": wheel_digest,
        },
    }
    signing_bytes = json.dumps(
        {
            "schema_version": "signing-public-first-attestation.v1",
            "evidence": evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    attestation = {
        "schema_version": "signing-public-first-attestation.v1",
        "evidence": evidence,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": key.key_id,
            "value": base64.b64encode(private_key.sign(signing_bytes)).decode(),
        },
    }
    attestation_path = root / "public-first-attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
    return attestation_path, wheel


def _ready_project(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: str,
    suffix: str,
    total_frames: int | None = None,
) -> tuple[dict[str, Any], PublicKeyRing, Path, Path]:
    capabilities, manifest, keyring, private_key = _production_documents(
        root,
        monkeypatch,
        adapter=adapter,
        suffix=suffix,
        total_frames=total_frames,
    )
    store = ProjectStore(root)
    state = store.init(name=f"Dogfood {suffix}", project_id=f"project_{suffix}")
    asset = store.assets_directory / "screen" / "home.png"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"local-screen-fixture")
    state = store.save_capabilities(capabilities, expected_revision=state.revision)
    store.save_manifest(manifest, expected_revision=state.revision)
    director = DirectorStateStore(root)
    director_state = director.create(
        server_url="https://director.example.test/v1",
        session={
            "session_id": f"dir_{suffix}",
            "status": "confirmed",
            "brief_version": 1,
            "catalog_version": "2026-07-15.1",
            "updated_at": NOW,
        },
        adapter_kind=adapter,
        adapter_version="1.0.0",
    )
    director.update(
        director_state,
        generation_id=f"generation_{suffix}",
        generation_status="ready",
    )
    attestation, wheel = _release_files(
        root / "release",
        keyring=keyring,
        private_key=private_key,
    )
    return manifest, keyring, attestation, wheel


def _write_outputs(root: Path, manifest: dict[str, Any]) -> Path:
    output = root / "output"
    output.mkdir()
    for index, item in enumerate(manifest["outputs"], 1):
        (output / item["filename"]).write_bytes(f"output-{index}".encode())
    return output


def _probe_for(manifest: dict[str, Any]):
    by_name = {item["filename"]: item for item in manifest["outputs"]}

    def probe(path: Path) -> dict[str, Any]:
        item = by_name[path.name]
        payload: dict[str, Any] = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": item["width"],
                    "height": item["height"],
                }
            ],
            "format": {},
        }
        if item["kind"] == "video":
            payload["format"]["duration"] = manifest["total_frames"] / manifest["fps"]
        return payload

    return probe


def _record_full_prefix(root: Path, *, adapter: str, suffix: str) -> None:
    values = {
        "adapter": adapter,
        "session_id": f"dir_{suffix}",
    }
    record_event_if_active(root, "director_start", status="collecting_decisions", **values)
    record_event_if_active(
        root,
        "decision_answer",
        status="ready_to_confirm",
        interaction_mode="native_choice",
        **values,
    )
    record_event_if_active(root, "brief_confirm", status="confirmed", **values)
    record_event_if_active(
        root,
        "generation_request",
        generation_id=f"generation_{suffix}",
        status="queued",
        **values,
    )
    record_event_if_active(
        root,
        "generation_status",
        generation_id=f"generation_{suffix}",
        status="ready",
        **values,
    )


def _native_receipt(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: str,
    suffix: str,
) -> dict[str, Any]:
    document, keyring, attestation, wheel = _ready_project(
        root, monkeypatch, adapter=adapter, suffix=suffix
    )
    begin_dogfood(
        root,
        run_id=f"run_{suffix}",
        run_kind="native_full",
        expected_adapter=adapter,
        public_first_attestation_path=attestation,
        public_wheel_path=wheel,
        keyring=keyring,
    )
    _record_full_prefix(root, adapter=adapter, suffix=suffix)
    capture_render_evidence(
        root,
        _write_outputs(root, document),
        keyring=keyring,
        probe_runner=_probe_for(document),
    )
    return build_dogfood_receipt(root, completed_at="2026-07-17T12:00:00Z")


def test_native_receipt_binds_real_project_signature_and_four_local_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _native_receipt(tmp_path, monkeypatch, adapter="codex", suffix="codex")

    assert receipt["run_kind"] == "native_full"
    assert receipt["adapters_seen"] == ["codex"]
    assert receipt["interaction_modes_seen"] == ["native_choice"]
    assert receipt["generation_id"] == "generation_codex"
    assert receipt["signature_key_id"] == "lecturecast-prod-e2e-v1"
    assert receipt["signature_key_status"] == "current"
    assert len(receipt["outputs"]) == 4
    assert all(item["sha256"].startswith("sha256:") for item in receipt["outputs"])
    serialized = json.dumps(receipt, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "director.example.test" not in serialized

    path = tmp_path / "receipt.json"
    write_dogfood_receipt(path, receipt)
    before = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(LectureCastError, match="已存在"):
        write_dogfood_receipt(path, receipt)
    assert path.read_bytes() == before


def test_active_run_requires_truthful_interaction_and_fresh_task_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, keyring, private_key = _production_documents(
        tmp_path / "release-source",
        monkeypatch,
        adapter="codex",
        suffix="guards",
    )
    attestation, wheel = _release_files(
        tmp_path / "release",
        keyring=keyring,
        private_key=private_key,
    )
    store = ProjectStore(tmp_path)
    store.init(name="Guards", project_id="project_guards")
    begin_dogfood(
        tmp_path,
        run_id="run_guards",
        run_kind="handoff",
        expected_adapter="codex",
        public_first_attestation_path=attestation,
        public_wheel_path=wheel,
        keyring=keyring,
    )

    with pytest.raises(LectureCastError, match="交互来源"):
        require_interaction_mode_if_active(tmp_path, None, adapter="codex")
    assert (
        require_interaction_mode_if_active(
            tmp_path,
            "native_choice",
            adapter="codex",
        )
        == "native_choice"
    )
    with pytest.raises(LectureCastError, match="新 Agent task"):
        require_fresh_task_if_active(
            tmp_path,
            adapter_changed=True,
            fresh_task=False,
        )


def test_handoff_and_text_receipts_enforce_distinct_non_paid_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_root = tmp_path / "handoff"
    document, keyring, attestation, wheel = _ready_project(
        handoff_root,
        monkeypatch,
        adapter="openclaw",
        suffix="handoff",
    )
    begin_dogfood(
        handoff_root,
        run_id="run_handoff",
        run_kind="handoff",
        expected_adapter="codex",
        public_first_attestation_path=attestation,
        public_wheel_path=wheel,
        keyring=keyring,
    )
    record_event_if_active(
        handoff_root,
        "director_start",
        adapter="codex",
        session_id="dir_handoff",
        status="collecting_decisions",
    )
    record_event_if_active(
        handoff_root,
        "decision_answer",
        adapter="codex",
        session_id="dir_handoff",
        status="collecting_decisions",
        interaction_mode="native_choice",
    )
    record_event_if_active(
        handoff_root,
        "director_handoff",
        adapter="codex",
        session_id="dir_handoff",
        status="collecting_decisions",
    )
    record_event_if_active(
        handoff_root,
        "director_resume",
        adapter="claude-code",
        session_id="dir_handoff",
        status="collecting_decisions",
        adapter_changed=True,
        fresh_task=True,
    )
    record_event_if_active(
        handoff_root,
        "brief_confirm",
        adapter="claude-code",
        session_id="dir_handoff",
        status="confirmed",
    )
    record_event_if_active(
        handoff_root,
        "director_handoff",
        adapter="claude-code",
        session_id="dir_handoff",
        status="confirmed",
    )
    record_event_if_active(
        handoff_root,
        "director_resume",
        adapter="openclaw",
        session_id="dir_handoff",
        status="confirmed",
        adapter_changed=True,
        fresh_task=True,
    )
    record_event_if_active(
        handoff_root,
        "generation_request",
        adapter="openclaw",
        session_id="dir_handoff",
        generation_id="generation_handoff",
        status="queued",
    )
    record_event_if_active(
        handoff_root,
        "generation_status",
        adapter="openclaw",
        session_id="dir_handoff",
        generation_id="generation_handoff",
        status="ready",
    )
    capture_render_evidence(
        handoff_root,
        _write_outputs(handoff_root, document),
        keyring=keyring,
        probe_runner=_probe_for(document),
    )
    handoff = build_dogfood_receipt(handoff_root)
    assert handoff["adapters_seen"] == ["codex", "claude-code", "openclaw"]

    text_root = tmp_path / "text"
    ProjectStore(text_root).init(name="Text", project_id="project_text")
    begin_dogfood(
        text_root,
        run_id="run_text",
        run_kind="text_fallback",
        expected_adapter="text",
        public_first_attestation_path=attestation,
        public_wheel_path=wheel,
        keyring=keyring,
    )
    record_event_if_active(
        text_root,
        "director_start",
        adapter="text",
        session_id="dir_text",
        status="collecting_decisions",
    )
    record_event_if_active(
        text_root,
        "decision_answer",
        adapter="text",
        session_id="dir_text",
        status="ready_to_confirm",
        interaction_mode="text_fallback",
    )
    text = build_dogfood_receipt(text_root)
    assert text["generation_id"] is None
    assert text["outputs"] == []


def _clone_receipt(
    receipt: dict[str, Any],
    *,
    adapter: str,
    suffix: str,
) -> dict[str, Any]:
    cloned = copy.deepcopy(receipt)
    cloned["run_id"] = f"run_{suffix}"
    cloned["project_id"] = f"project_{suffix}"
    cloned["expected_adapter"] = adapter
    cloned["adapters_seen"] = [adapter]
    cloned["generation_id"] = f"generation_{suffix}"
    cloned["manifest_digest"] = "sha256:" + hashlib_for(suffix)
    cloned["journal_digest"] = "sha256:" + hashlib_for(f"journal-{suffix}")
    return cloned


def hashlib_for(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def test_gate_requires_exact_five_receipt_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = _native_receipt(tmp_path / "codex", monkeypatch, adapter="codex", suffix="codex")
    claude = _clone_receipt(codex, adapter="claude-code", suffix="claude")
    openclaw = _clone_receipt(codex, adapter="openclaw", suffix="openclaw")
    handoff = _clone_receipt(codex, adapter="codex", suffix="handoff-gate")
    handoff["run_kind"] = "handoff"
    handoff["adapters_seen"] = ["codex", "claude-code", "openclaw"]
    handoff["event_counts"] = {
        **handoff["event_counts"],
        "director_handoff": 2,
        "director_resume": 2,
    }
    text = _clone_receipt(codex, adapter="text", suffix="text-gate")
    text.update(
        {
            "run_kind": "text_fallback",
            "adapters_seen": ["text"],
            "interaction_modes_seen": ["text_fallback"],
            "generation_id": None,
            "capability_digest": None,
            "manifest_digest": None,
            "signature_key_id": None,
            "signature_key_status": None,
            "outputs": [],
            "event_counts": {"director_start": 1, "decision_answer": 1},
        }
    )

    report = evaluate_dogfood_gate([codex, claude, openclaw, handoff, text])

    assert report["ready"] is True
    assert report["receipt_count"] == 5
    assert len(report["checks"]) == 6
    assert report["evidence_digest"].startswith("sha256:")

    duplicate = copy.deepcopy(claude)
    duplicate["generation_id"] = codex["generation_id"]
    failed = evaluate_dogfood_gate([codex, duplicate, openclaw, handoff, text])
    assert failed["ready"] is False
    assert next(item for item in failed["checks"] if item["id"] == "matrix.unique_runs")[
        "status"
    ] == "failed"


def test_dogfood_cli_begin_and_status_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, keyring, private_key = _production_documents(
        tmp_path / "release-source",
        monkeypatch,
        adapter="text",
        suffix="cli",
    )
    attestation, wheel = _release_files(
        tmp_path / "release",
        keyring=keyring,
        private_key=private_key,
    )
    ProjectStore(tmp_path).init(name="CLI", project_id="project_cli_dogfood")

    begun = runner.invoke(
        app,
        [
            "dogfood",
            "begin",
            str(tmp_path),
            "--run-id",
            "run_cli_dogfood",
            "--run-kind",
            "text_fallback",
            "--adapter",
            "text",
            "--public-first-attestation",
            str(attestation),
            "--public-wheel",
            str(wheel),
            "--json",
        ],
    )
    status_result = runner.invoke(app, ["dogfood", "status", str(tmp_path), "--json"])

    assert begun.exit_code == 0, begun.output
    assert status_result.exit_code == 0, status_result.output
    payload = json.loads(status_result.stdout)
    assert payload["event_count"] == 0
    assert str(tmp_path) not in status_result.stdout
    session = load_dogfood_session(tmp_path)
    assert "server_url" not in json.dumps(session)


def test_release_binding_rejects_tampered_wheel_and_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, keyring, private_key = _production_documents(
        tmp_path / "release-source",
        monkeypatch,
        adapter="codex",
        suffix="release-negative",
    )
    attestation, wheel = _release_files(
        tmp_path / "release",
        keyring=keyring,
        private_key=private_key,
    )

    binding = verify_release_binding(attestation, wheel, keyring=keyring)

    assert binding["version"] == "0.3.0"
    assert binding["commit"] == "a" * 40
    assert str(tmp_path) not in json.dumps(binding)

    tampered_wheel = tmp_path / "tampered.whl"
    tampered_wheel.write_bytes(wheel.read_bytes() + b"tamper")
    with pytest.raises(LectureCastError, match="不一致"):
        verify_release_binding(attestation, tampered_wheel, keyring=keyring)

    tampered_attestation = tmp_path / "tampered-attestation.json"
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["evidence"]["public_release"]["commit"] = "b" * 40
    tampered_attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LectureCastError, match="签名无效"):
        verify_release_binding(tampered_attestation, wheel, keyring=keyring)


def test_real_remotion_four_output_fixture_passes_capture_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_value = os.environ.get("LECTURECAST_REAL_RENDER_FIXTURE")
    if fixture_value is None:
        pytest.skip("set LECTURECAST_REAL_RENDER_FIXTURE for local render integration")
    fixture_root = Path(fixture_value)
    sources = {
        "demo-bilibili.mp4": fixture_root / "director-landscape.mp4",
        "demo-xiaohongshu.mp4": fixture_root / "director-vertical.mp4",
        "demo-cover-landscape.png": fixture_root / "director-cover-landscape.png",
        "demo-cover-vertical.png": fixture_root / "director-cover-vertical.png",
    }
    if not all(path.is_file() for path in sources.values()):
        pytest.fail("real Remotion fixture is incomplete")
    manifest, keyring, attestation, wheel = _ready_project(
        tmp_path,
        monkeypatch,
        adapter="codex",
        suffix="real-render",
        total_frames=30,
    )
    begin_dogfood(
        tmp_path,
        run_id="run_real-render",
        run_kind="native_full",
        expected_adapter="codex",
        public_first_attestation_path=attestation,
        public_wheel_path=wheel,
        keyring=keyring,
    )
    _record_full_prefix(tmp_path, adapter="codex", suffix="real-render")
    output_root = tmp_path / "real-output"
    output_root.mkdir()
    for filename, source in sources.items():
        shutil.copy2(source, output_root / filename)

    result = capture_render_evidence(tmp_path, output_root, keyring=keyring)
    receipt = build_dogfood_receipt(tmp_path)

    assert result["output_count"] == 4
    assert len(receipt["outputs"]) == 4
    assert all(item["bytes"] > 1000 for item in receipt["outputs"])
    assert all(item["sha256"].startswith("sha256:") for item in receipt["outputs"])
