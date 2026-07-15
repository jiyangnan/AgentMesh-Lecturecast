from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lecturecast.errors import LectureCastError
from lecturecast.project import ProjectStore, atomic_write_json


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_project_persists_brief_manifest_and_resumes_across_processes(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    initialized = store.init(name="Agent handoff project", project_id="project_handoff")
    with_capabilities = store.save_capabilities(
        _fixture("client-capabilities-v1.json"), expected_revision=initialized.revision
    )
    with_brief = store.save_brief(
        _fixture("creative-brief-v1.json"), expected_revision=with_capabilities.revision
    )
    with_manifest = store.save_manifest(
        _fixture("production-manifest-v1.json"), expected_revision=with_brief.revision
    )

    assert with_manifest.payload["status"] == "manifest_ready"
    assert store.capabilities_path.is_file()
    assert os.stat(store.manifest_path).st_mode & 0o222 == 0

    command = [
        sys.executable,
        "-c",
        (
            "import json,sys; from lecturecast.project import ProjectStore; "
            "print(json.dumps(ProjectStore(sys.argv[1]).load().to_dict(), sort_keys=True))"
        ),
        str(tmp_path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    resumed = json.loads(result.stdout)

    assert resumed == with_manifest.to_dict()


def test_manifest_is_idempotent_but_cannot_be_overwritten(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    state = store.init(name="Immutable manifest")
    manifest = _fixture("production-manifest-v1.json")
    saved = store.save_manifest(manifest, expected_revision=state.revision)
    original_bytes = store.manifest_path.read_bytes()

    assert store.save_manifest(manifest, expected_revision=saved.revision) == saved
    changed = json.loads(json.dumps(manifest))
    changed["script"][0]["title"] = "tampered"
    with pytest.raises(LectureCastError, match="不可覆盖"):
        store.save_manifest(changed, expected_revision=saved.revision)

    assert store.manifest_path.read_bytes() == original_bytes


def test_local_overrides_never_modify_manifest_original(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    state = store.init(name="Overrides")
    state = store.save_manifest(
        _fixture("production-manifest-v1.json"), expected_revision=state.revision
    )
    original = store.manifest_path.read_bytes()

    updated = store.save_overrides(
        {"subtitles": {"safe_area_bottom_percent": 20}},
        expected_revision=state.revision,
    )

    assert updated.revision == state.revision + 1
    assert store.manifest_path.read_bytes() == original
    assert _fixture("production-manifest-v1.json")["signature"]


def test_revision_conflict_requires_reload(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    state = store.init(name="Concurrent agents")
    updated = store.save_brief(_fixture("creative-brief-v1.json"), expected_revision=state.revision)

    with pytest.raises(LectureCastError) as captured:
        store.save_manifest(
            _fixture("production-manifest-v1.json"), expected_revision=state.revision
        )

    assert captured.value.code == "project_revision_conflict"
    assert captured.value.retryable
    assert updated.revision == 2


def test_atomic_write_interruption_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"value": "before"})
    previous = target.read_bytes()

    def interrupted(_source: Path, _target: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr("lecturecast.project.os.replace", interrupted)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_json(target, {"value": "after"})

    assert target.read_bytes() == previous
    assert not list(tmp_path.glob(".state.json.*"))


def test_old_project_has_actionable_migration_hint(tmp_path: Path) -> None:
    directory = tmp_path / ".lecturecast"
    directory.mkdir()
    (directory / "project.json").write_text(
        json.dumps({"schema_version": "0.1", "project_id": "legacy"}), encoding="utf-8"
    )

    with pytest.raises(LectureCastError) as captured:
        ProjectStore(tmp_path).load()

    assert captured.value.code == "client_upgrade_required"
    assert "迁移" in captured.value.next_action


def test_project_metadata_never_contains_api_key_or_absolute_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "lc_live_project_secret"
    monkeypatch.setenv("LECTURECAST_API_KEY", secret)
    ProjectStore(tmp_path).init(name="Shareable")

    combined = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".lecturecast").glob("*.json")
    )
    assert secret not in combined
    assert str(tmp_path) not in combined


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "lc_live_forbidden"},
        {"asset": {"path": "/Users/example/private/source.mov"}},
    ],
)
def test_shareable_overrides_reject_credentials_and_absolute_paths(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    store = ProjectStore(tmp_path)
    state = store.init(name="Safe overrides")
    state = store.save_manifest(
        _fixture("production-manifest-v1.json"), expected_revision=state.revision
    )

    with pytest.raises(LectureCastError, match="不能"):
        store.save_overrides(overrides, expected_revision=state.revision)
