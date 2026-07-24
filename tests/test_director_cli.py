from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.director import (
    DirectorClient,
    DirectorStateStore,
    load_source_file,
    normalize_server_url,
    probe_director,
)
from lecturecast.errors import LectureCastError
from lecturecast.project import ProjectStore
from lecturecast.protocol import ClientCapabilities


FIXTURE_DIR = Path(__file__).parent / "fixtures"
runner = CliRunner()
NOW = "2026-07-15T12:00:00Z"


@pytest.fixture(autouse=True)
def allow_commercial_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.director.require_commercial_access", lambda: None
    )


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _session(
    *,
    status: str,
    card: dict[str, Any] | None,
    brief: dict[str, Any] | None,
    brief_version: int,
) -> dict[str, Any]:
    brief_fixture = _fixture("creative-brief-v1.json")
    return {
        "session_id": "dir_demo_001",
        "status": status,
        "catalog_version": brief_fixture["catalog_version"],
        "brief_version": brief_version,
        "brief_digest": None,
        "source": brief_fixture["source"],
        "decision_card_set": card,
        "brief": brief,
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": "2026-08-14T12:00:00Z",
        "content_deleted_at": None,
    }


class FakeDirectorClient:
    def __init__(self, *, fail_first_generation: bool = False) -> None:
        self.card = _fixture("decision-card-set-v1.json")
        self.brief = _fixture("creative-brief-v1.json")
        self.manifest = _fixture("production-manifest-v1.json")
        self.current = _session(
            status="collecting_decisions",
            card=self.card,
            brief=None,
            brief_version=0,
        )
        self.generation_ids: list[str] = []
        self.generation_capabilities: list[dict[str, Any]] = []
        self.fail_first_generation = fail_first_generation
        self.generation_failures = 0

    def create_session(self, source: dict[str, Any]) -> dict[str, Any]:
        assert set(source) == {"source_type", "title", "summary", "language"}
        return dict(self.current)

    def get_session(self, session_id: str) -> dict[str, Any]:
        assert session_id == "dir_demo_001"
        return dict(self.current)

    def answer(
        self,
        session_id: str,
        *,
        question_id: str,
        option_id: str,
        catalog_version: str,
        custom_text: str | None = None,
    ) -> dict[str, Any]:
        del custom_text
        assert session_id == "dir_demo_001"
        question = next(
            item for item in self.card["questions"] if item["question_id"] == question_id
        )
        assert option_id in {item["option_id"] for item in question["options"]}
        assert catalog_version == self.card["catalog_version"]
        self.current = _session(
            status="ready_to_confirm",
            card=None,
            brief=None,
            brief_version=0,
        )
        return dict(self.current)

    def confirm_brief(
        self, session_id: str, *, expected_brief_version: int
    ) -> dict[str, Any]:
        assert session_id == "dir_demo_001"
        assert expected_brief_version == 0
        self.current = _session(
            status="confirmed",
            card=None,
            brief=self.brief,
            brief_version=1,
        )
        return dict(self.current)

    def create_generation(
        self,
        session_id: str,
        *,
        generation_id: str,
        expected_brief_version: int,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        assert session_id == "dir_demo_001"
        assert expected_brief_version == 1
        assert capabilities["adapter"]["kind"] in {
            "codex",
            "claude-code",
            "openclaw",
            "text",
        }
        self.generation_capabilities.append(capabilities)
        self.generation_ids.append(generation_id)
        if self.fail_first_generation and self.generation_failures == 0:
            self.generation_failures += 1
            raise LectureCastError(
                code="core_unavailable",
                message="temporary",
                next_action="retry",
                retryable=True,
            )
        return {
            "generation_id": generation_id,
            "status": "queued",
            "updated_at": NOW,
            "manifest": None,
            "deducted_credits": 10,
        }

    def get_generation(self, generation_id: str) -> dict[str, Any]:
        assert generation_id == "generation_demo_001"
        return {
            "generation_id": generation_id,
            "status": "ready",
            "updated_at": NOW,
            "manifest": self.manifest,
            "deducted_credits": 10,
        }

    def delete_session(self, session_id: str) -> dict[str, Any]:
        assert session_id == "dir_demo_001"
        return {
            "session_id": session_id,
            "deleted": True,
            "content_deleted_at": NOW,
        }


def _init_project(path: Path, adapter: str = "codex") -> Path:
    created = runner.invoke(
        app,
        [
            "project",
            "init",
            str(path),
            "--name",
            "Director CLI",
            "--adapter",
            adapter,
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    source = path / "source-summary.json"
    source.write_text(
        json.dumps(_fixture("creative-brief-v1.json")["source"], ensure_ascii=False),
        encoding="utf-8",
    )
    return source


@pytest.fixture(autouse=True)
def _allow_project_cli_after_test_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )


def _start(path: Path, source: Path, adapter: str = "codex") -> dict[str, Any]:
    result = runner.invoke(
        app,
        [
            "director",
            "start",
            str(path),
            "--source",
            str(source),
            "--server",
            "https://director.example.test",
            "--adapter",
            adapter,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _confirm(path: Path, client: FakeDirectorClient) -> None:
    question = client.card["questions"][0]
    answered = runner.invoke(
        app,
        [
            "director",
            "answer",
            str(path),
            "--question-id",
            question["question_id"],
            "--option-id",
            question["options"][0]["option_id"],
            "--json",
        ],
    )
    assert answered.exit_code == 0, answered.output
    assert json.loads(answered.stdout)["workflow"]["next_action"]["id"] == (
        "director.brief.show"
    )
    confirmed = runner.invoke(
        app, ["director", "brief", "confirm", str(path), "--json"]
    )
    assert confirmed.exit_code == 0, confirmed.output
    confirmed_payload = json.loads(confirmed.stdout)
    assert confirmed_payload["session"]["status"] == "confirmed"
    assert confirmed_payload["workflow"]["next_action"] == {
        "id": "director.generate",
        "kind": "command",
        "argv": [
            "lecturecast",
            "director",
            "generate",
            str(path.resolve()),
            "--json",
        ],
        "mutates": True,
        "requires_user_approval": True,
        "credit_cost": 10,
    }


def _save_fixture_capabilities(path: Path) -> None:
    store = ProjectStore(path)
    project = store.load()
    store.save_capabilities(
        ClientCapabilities.model_validate(_fixture("client-capabilities-v1.json")),
        expected_revision=project.revision,
    )


def test_full_director_cli_flow_is_machine_readable_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeDirectorClient()
    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", lambda _url: client
    )
    source = _init_project(tmp_path)
    started = _start(tmp_path, source)
    assert started["director"]["server_url"] == "https://director.example.test/v1"
    assert started["decision_card_set"] == client.card
    assert started["workflow"]["next_action"]["id"] == "director.answer"

    handoff = runner.invoke(
        app, ["director", "handoff", str(tmp_path), "--json"]
    )
    assert handoff.exit_code == 0, handoff.output
    handoff_payload = json.loads(handoff.stdout)
    assert handoff_payload["resume_argv"][-1] == "--json"
    assert handoff_payload["project_path"] == str(tmp_path.resolve())
    assert handoff_payload["director_resume_argv_by_adapter"]["openclaw"] == [
        "lecturecast",
        "director",
        "resume",
        str(tmp_path.resolve()),
        "--adapter",
        "openclaw",
        "--host-contract",
        "1.0.0",
        "--json",
    ]
    assert "api_key" not in handoff.stdout.lower()

    resumed = runner.invoke(
        app, ["director", "next", str(tmp_path), "--json"]
    )
    assert resumed.exit_code == 0, resumed.output
    payload = json.loads(resumed.stdout)
    option_ids = [
        option["option_id"]
        for question in payload["decision_card_set"]["questions"]
        for option in question["options"]
    ]
    assert option_ids
    assert all(isinstance(option_id, str) for option_id in option_ids)

    _confirm(tmp_path, client)
    _save_fixture_capabilities(tmp_path)
    generated = runner.invoke(
        app,
        [
            "director",
            "generate",
            str(tmp_path),
            "--generation-id",
            "generation_demo_001",
            "--json",
        ],
    )
    assert generated.exit_code == 0, generated.output
    assert json.loads(generated.stdout)["generation"]["status"] == "queued"
    assert json.loads(generated.stdout)["workflow"]["next_action"]["id"] == (
        "director.status"
    )

    status = runner.invoke(
        app, ["director", "status", str(tmp_path), "--json"]
    )
    assert status.exit_code == 0, status.output
    ready = json.loads(status.stdout)
    assert ready["generation"]["status"] == "ready"
    assert ready["project"]["status"] == "manifest_ready"
    assert ready["workflow"]["next_action"]["id"] == "manifest.review"
    assert (tmp_path / ".lecturecast" / "production-manifest.json").exists()

    deleted = runner.invoke(
        app, ["director", "delete", str(tmp_path), "--json"]
    )
    assert deleted.exit_code == 0, deleted.output
    assert json.loads(deleted.stdout)["director"]["session_status"] == "deleted"
    assert (tmp_path / ".lecturecast" / "production-manifest.json").exists()

    state_path = tmp_path / ".lecturecast" / "director-state.json"
    state_text = state_path.read_text(encoding="utf-8")
    assert "api_key" not in state_text.lower()
    assert "authorization" not in state_text.lower()
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_all_agent_adapters_receive_identical_stable_option_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    option_sets: list[list[str]] = []
    for adapter in ("codex", "claude-code", "openclaw"):
        path = tmp_path / adapter
        path.mkdir()
        source = _init_project(path, adapter=adapter)
        client = FakeDirectorClient()
        monkeypatch.setattr(
            "lecturecast.commands.director._make_client", lambda _url, c=client: c
        )
        payload = _start(path, source, adapter=adapter)
        option_sets.append(
            [
                option["option_id"]
                for question in payload["decision_card_set"]["questions"]
                for option in question["options"]
            ]
        )

    assert option_sets[0] == option_sets[1] == option_sets[2]


def test_same_project_rebinds_across_agents_and_refreshes_capabilities_before_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeDirectorClient()
    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", lambda _url: client
    )
    source = _init_project(tmp_path)
    started = _start(tmp_path, source, adapter="codex")
    assert started["director"]["adapter_kind"] == "codex"

    def unexpected_network(_url: str) -> FakeDirectorClient:
        raise AssertionError("director resume must stay offline")

    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", unexpected_network
    )
    resumed_by_claude = runner.invoke(
        app,
        [
            "director",
            "resume",
            str(tmp_path),
            "--adapter",
            "claude-code",
            "--adapter-version",
            "1.0.0",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert resumed_by_claude.exit_code == 0, resumed_by_claude.output
    claude_payload = json.loads(resumed_by_claude.stdout)
    assert claude_payload["director"]["adapter_kind"] == "claude-code"
    assert claude_payload["resume"] == {
        "adapter_changed": True,
        "network_requested": False,
        "director_network_requested": False,
        "commercial_access_verified": True,
        "credit_deducted": False,
        "capabilities_policy": "refresh_before_generate_on_adapter_mismatch",
    }

    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", lambda _url: client
    )
    _confirm(tmp_path, client)
    _save_fixture_capabilities(tmp_path)

    resumed_by_openclaw = runner.invoke(
        app,
        [
            "director",
            "resume",
            str(tmp_path),
            "--adapter",
            "openclaw",
            "--adapter-version",
            "1.0.0",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert resumed_by_openclaw.exit_code == 0, resumed_by_openclaw.output
    openclaw_state = json.loads(resumed_by_openclaw.stdout)["director"]
    resumed_again = runner.invoke(
        app,
        [
            "director",
            "resume",
            str(tmp_path),
            "--adapter",
            "openclaw",
            "--adapter-version",
            "1.0.0",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )
    assert resumed_again.exit_code == 0, resumed_again.output
    resumed_again_payload = json.loads(resumed_again.stdout)
    assert resumed_again_payload["resume"]["adapter_changed"] is False
    assert resumed_again_payload["director"]["state_revision"] == openclaw_state[
        "state_revision"
    ]

    captured: list[tuple[str, str]] = []

    def capture_current_adapter(**kwargs: Any) -> ClientCapabilities:
        adapter_kind = str(kwargs["adapter_kind"])
        adapter_version = str(kwargs["adapter_version"])
        captured.append((adapter_kind, adapter_version))
        payload = _fixture("client-capabilities-v1.json")
        payload["capabilities_id"] = "caps_openclaw_rebound"
        payload["adapter"] = {
            "kind": adapter_kind,
            "version": adapter_version,
        }
        return ClientCapabilities.model_validate(payload)

    monkeypatch.setattr(
        "lecturecast.commands.director.capture_capabilities", capture_current_adapter
    )
    generated = runner.invoke(
        app,
        [
            "director",
            "generate",
            str(tmp_path),
            "--generation-id",
            "generation_demo_001",
            "--json",
        ],
    )
    assert generated.exit_code == 0, generated.output
    assert captured == [("openclaw", "1.0.0")]
    assert client.generation_capabilities[-1]["adapter"] == {
        "kind": "openclaw",
        "version": "1.0.0",
    }


def test_director_resume_rejects_invalid_adapter_without_changing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeDirectorClient()
    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", lambda _url: client
    )
    source = _init_project(tmp_path)
    _start(tmp_path, source)
    state_path = tmp_path / ".lecturecast" / "director-state.json"
    before = state_path.read_bytes()

    invalid = runner.invoke(
        app,
        [
            "director",
            "resume",
            str(tmp_path),
            "--adapter",
            "openclaw",
            "--adapter-version",
            "latest",
            "--host-contract",
            "1.0.0",
            "--json",
        ],
    )

    assert invalid.exit_code == 1
    assert json.loads(invalid.stderr)["code"] == "manifest_incompatible"
    assert state_path.read_bytes() == before


def test_failed_generation_retry_reuses_reserved_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeDirectorClient(fail_first_generation=True)
    monkeypatch.setattr(
        "lecturecast.commands.director._make_client", lambda _url: client
    )
    source = _init_project(tmp_path)
    _start(tmp_path, source)
    _confirm(tmp_path, client)
    _save_fixture_capabilities(tmp_path)

    first = runner.invoke(
        app,
        [
            "director",
            "generate",
            str(tmp_path),
            "--generation-id",
            "generation_demo_001",
            "--json",
        ],
    )
    assert first.exit_code == 1
    error = json.loads(first.stderr)
    assert error["code"] == "core_unavailable"
    assert "generation_demo_001" in (
        tmp_path / ".lecturecast" / "director-state.json"
    ).read_text(encoding="utf-8")

    second = runner.invoke(
        app, ["director", "generate", str(tmp_path), "--json"]
    )
    assert second.exit_code == 0, second.output
    assert client.generation_ids == ["generation_demo_001", "generation_demo_001"]


class RecordingTransport:
    def __init__(self, document: dict[str, Any], status: int = 200) -> None:
        self.document = document
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        self.calls.append(kwargs)
        return self.status, self.document


def test_director_health_probe_sends_no_credential_or_media() -> None:
    transport = RecordingTransport({"status": "ok"})

    result = probe_director(
        "https://director.example.test",
        transport=transport,
    )

    assert result == {
        "reachable": True,
        "url": "https://director.example.test/v1",
        "status": "ok",
    }
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://director.example.test/v1/health"
    assert "Authorization" not in call["headers"]
    assert call["payload"] is None
    assert call["timeout"] == 15.0


def test_director_health_probe_rejects_error_status() -> None:
    transport = RecordingTransport({"status": "unavailable"}, status=503)

    with pytest.raises(LectureCastError) as captured:
        probe_director("https://director.example.test", transport=transport)

    assert captured.value.code == "director_unavailable"
    assert captured.value.retryable is True


def test_director_client_normalizes_url_validates_protocol_and_keeps_key_in_header() -> None:
    document = _session(
        status="collecting_decisions",
        card=_fixture("decision-card-set-v1.json"),
        brief=None,
        brief_version=0,
    )
    transport = RecordingTransport(document)
    secret = "lc_live_super_secret_value"
    client = DirectorClient(
        "https://director.example.test",
        api_key=secret,
        transport=transport,
    )

    assert client.get_session("dir_demo_001") == document
    call = transport.calls[0]
    assert call["url"] == "https://director.example.test/v1/director/sessions/dir_demo_001"
    assert call["headers"]["Authorization"] == f"Bearer {secret}"
    assert secret not in json.dumps(document)

    with pytest.raises(LectureCastError):
        normalize_server_url("http://remote.example.test/v1")
    assert normalize_server_url("http://localhost:8000") == "http://localhost:8000/v1"

    error_transport = RecordingTransport(
        {
            "detail": {
                "code": "insufficient_credits",
                "message": "credit 余额不足。",
                "next_action": "补充 credit 后重试。",
                "retryable": False,
            }
        },
        status=402,
    )
    failing = DirectorClient(
        "https://director.example.test/v1",
        api_key=secret,
        transport=error_transport,
    )
    with pytest.raises(LectureCastError) as captured:
        failing.get_session("dir_demo_001")
    assert captured.value.code == "insufficient_credits"
    assert secret not in str(captured.value.to_dict())


def test_source_summary_rejects_media_paths_and_extra_content(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                **_fixture("creative-brief-v1.json")["source"],
                "raw_media_path": "/private/video.mov",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LectureCastError):
        load_source_file(source)


def test_source_summary_rejects_title_only_material_before_network(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "source_type": "screen_recording",
                "title": "产品演示",
                "summary": "产品演示",
                "language": "zh-CN",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LectureCastError) as captured:
        load_source_file(source)

    assert "至少 20 字" in captured.value.next_action


def test_director_state_revision_detects_cross_agent_overwrite(tmp_path: Path) -> None:
    _init_project(tmp_path)
    store = DirectorStateStore(tmp_path)
    state = store.create(
        server_url="https://director.example.test/v1",
        session=_session(
            status="collecting_decisions",
            card=_fixture("decision-card-set-v1.json"),
            brief=None,
            brief_version=0,
        ),
        adapter_kind="codex",
        adapter_version="1.0.0",
    )
    stale = store.load()
    store.update(state, session_status="confirmed")

    with pytest.raises(LectureCastError) as captured:
        store.update(stale, session_status="deleted")
    assert captured.value.code == "project_revision_conflict"

    rebound = store.load()
    stale_rebind = store.load()
    store.bind_adapter(
        rebound,
        adapter_kind="claude-code",
        adapter_version="1.0.0",
    )
    with pytest.raises(LectureCastError) as rebind_conflict:
        store.bind_adapter(
            stale_rebind,
            adapter_kind="openclaw",
            adapter_version="1.0.0",
        )
    assert rebind_conflict.value.code == "project_revision_conflict"
