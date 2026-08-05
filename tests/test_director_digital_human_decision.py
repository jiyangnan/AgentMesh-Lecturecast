"""§5.5e5d-d D13 — interactive digital-human downgrade card.

Tests the lazy client-local card fired by ``director generate`` when the Brief
asks for a digital human (``presenter.avatar == "photo"``) but the local stack
cannot serve HeyGen (``third_party_processors`` absent). D13 is client-local:
the server is never informed of the capability gap (§0 Principle 6), and
``create_generation``'s payload always omits ``third_party_processors`` — no
false configured=true, whether the card fires or the user consents to option B.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lecturecast.cli import app
from lecturecast.commands.director import (
    _d13_brief_avatar,
    _d13_decision_action,
    _d13_heygen_configured,
)
from lecturecast.director import DirectorClient, DirectorStateStore
from lecturecast.project import ProjectStore, atomic_write_json
from lecturecast.protocol import parse_client_capabilities

runner = CliRunner()
NOW = "2026-08-03T12:00:00Z"
FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _bypass_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass commercial + host-workflow gates (mirrors _setup_resume_project in
    test_generation_v1_1.py) so tests can construct a v1.1 confirmed state
    directly without driving the full session flow."""
    monkeypatch.setattr(
        "lecturecast.commands.project.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.director.require_commercial_access", lambda: None
    )
    monkeypatch.setattr(
        "lecturecast.commands.director.require_project_host_workflow",
        lambda *a, **kw: None,
    )
    monkeypatch.setenv("LECTURECAST_API_KEY", "test_key")


# ===========================================================================
# Unit tests: _d13_brief_avatar (intent signal, fail-closed)
# ===========================================================================

def _write_brief(tmp_path: Path, brief: dict[str, Any]) -> ProjectStore:
    store = ProjectStore(tmp_path)
    store.brief_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(store.brief_path, brief)
    return store


def test_d13_brief_avatar_photo(tmp_path: Path) -> None:
    store = _write_brief(tmp_path, {"presenter": {"avatar": "photo"}})
    assert _d13_brief_avatar(store) == "photo"


def test_d13_brief_avatar_none(tmp_path: Path) -> None:
    store = _write_brief(tmp_path, {"presenter": {"avatar": "none"}})
    assert _d13_brief_avatar(store) == "none"


def test_d13_brief_avatar_defaults_to_none_when_presenter_empty(tmp_path: Path) -> None:
    # The v1.1 fixture ships with presenter: {} (avatar defaults "none").
    store = _write_brief(tmp_path, {"presenter": {}})
    assert _d13_brief_avatar(store) is None  # absent avatar → None (≠ "photo")


def test_d13_brief_avatar_missing_file_returns_none(tmp_path: Path) -> None:
    # No brief written → fail-closed None (None != "photo" → card never fires).
    store = ProjectStore(tmp_path)
    assert _d13_brief_avatar(store) is None


def test_d13_brief_avatar_malformed_json_returns_none(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    store.brief_path.parent.mkdir(parents=True, exist_ok=True)
    store.brief_path.write_text("{ not valid json", encoding="utf-8")
    assert _d13_brief_avatar(store) is None


def test_d13_brief_avatar_non_dict_presenter_returns_none(tmp_path: Path) -> None:
    store = _write_brief(tmp_path, {"presenter": ["photo"]})  # list, not dict
    assert _d13_brief_avatar(store) is None


def test_d13_brief_avatar_non_str_avatar_returns_none(tmp_path: Path) -> None:
    # type-stability: a non-str avatar (e.g. bool True) must not match "photo".
    store = _write_brief(tmp_path, {"presenter": {"avatar": True}})
    assert _d13_brief_avatar(store) is None


# ===========================================================================
# Unit tests: _d13_heygen_configured (capability signal)
# ===========================================================================

def _caps(payload: dict[str, Any]) -> Any:
    return parse_client_capabilities(payload)


def test_d13_heygen_configured_true_when_present() -> None:
    caps = _caps(_fixture("client-capabilities-v1_1.json"))  # has heygen+configured
    assert _d13_heygen_configured(caps) is True


def test_d13_heygen_configured_false_when_omitted() -> None:
    payload = _fixture("client-capabilities-v1_1.json")
    payload = {k: v for k, v in payload.items() if k != "third_party_processors"}
    assert _d13_heygen_configured(_caps(payload)) is False


def test_d13_heygen_configured_false_when_not_configured() -> None:
    payload = _fixture("client-capabilities-v1_1.json")
    payload["third_party_processors"][0]["configured"] = False
    assert _d13_heygen_configured(_caps(payload)) is False


def test_d13_heygen_configured_false_for_other_provider() -> None:
    # The v1.1 schema pins provider to "heygen" (const), so a validated
    # ClientCapabilities can never hold another provider in production — but
    # the predicate's `provider == "heygen"` branch must still be correct.
    # Feed a stub whose model_dump() returns a non-heygen provider to exercise
    # that branch directly (defense against a future schema widening).
    class _StubCaps:
        def model_dump(self) -> dict[str, Any]:
            return {"third_party_processors": [{"provider": "f5", "configured": True}]}

    assert _d13_heygen_configured(_StubCaps()) is False


# ===========================================================================
# Unit tests: _d13_decision_action (card shape)
# ===========================================================================

def test_d13_decision_action_shape() -> None:
    action = _d13_decision_action("/tmp/proj")
    assert action["id"] == "director.digital_human.decide"
    assert action["kind"] == "host_choice"
    assert action["question_id"] == "digital_human_downgrade"
    assert action["mutates"] is True
    assert action["requires_user_approval"] is True
    options = action["options"]
    assert [o["id"] for o in options] == ["configure", "downgrade"]
    # argv_template routes to the client-local decide command with <option_id>.
    assert action["argv_template"] == [
        "lecturecast", "director", "digital-human", "decide",
        "/tmp/proj", "--choice", "<option_id>", "--json",
    ]


# ===========================================================================
# Integration tests: director generate (the D13 trigger composition)
# ===========================================================================

class _Capture:
    """Fake transport: records create_generation payload, returns a queued gen.

    Protocol-aware: v1.0 returns the minimal pre-schema generation shape
    (``_generation`` skips schema validation for v1.0, and omitting billing_state
    avoids the v1.1→v1.2 state upgrade that a v1.0-origin state can't hold);
    v1.1 returns the full schema-valid ``_queued_generation`` body."""

    def __init__(self, *, protocol_version: str = "1.1") -> None:
        self.protocol_version = protocol_version
        self.calls: list[dict[str, Any]] = []

    def request(self, *, method: str, url: str, headers: dict, payload: dict, timeout: float):
        self.calls.append({"method": method, "url": url, "payload": payload})
        gen_id = (payload or {}).get("generation_id", "gen_1")
        if self.protocol_version == "1.0":
            return 200, {"generation_id": gen_id, "status": "queued", "updated_at": NOW}
        return 200, _queued_generation(gen_id)


def _queued_generation(generation_id: str) -> dict[str, Any]:
    """A schema-valid v1.1 generation response in the freshly-created queued
    state (no milestones charged yet, no manifest). Mirrors the required-field
    set of ``manifest-generation-out.schema.json`` so ``_generation`` accepts it."""
    return {
        "generation_id": generation_id,
        "session_id": "sess_1",  # minLength 3 per schema (state session "s1" is fine)
        "brief_version": 1,
        "status": "queued",
        "model_policy_version": "flash_all_v1",
        "capability_digest": "sha256:" + "a" * 64,
        "manifest_digest": None,
        "manifest": None,
        "deducted_credits": None,
        "error_code": None,
        "credit_return_status": "not_required",
        "attempt_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
        "milestone_charges": [],
        "billing_state": "in_progress",
        "resume_available": False,
    }


def _setup_d13_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    avatar: str = "photo",
    configured: bool = False,
    protocol_version: str = "1.1",
) -> tuple[_Capture, DirectorStateStore]:
    """Construct a confirmed director project with a controlled brief.avatar +
    capabilities (configured or not). Returns the capture (for create_generation
    assertions) + the store."""
    store = DirectorStateStore(tmp_path)
    store.project.init(name="D13")
    store.create(
        server_url="https://api.test",
        session={
            "session_id": "s1",
            "status": "confirmed",
            "brief_version": 1,
            "catalog_version": "cv",
            "updated_at": NOW,
        },
        adapter_kind="codex",
        adapter_version="1.0.0",
        protocol_version=protocol_version,
    )
    # Write a v1.1 brief with the requested presenter.avatar.
    project_store = ProjectStore(tmp_path)
    project = project_store.load()
    brief = _fixture("creative-brief-v1_1.json")
    brief["presenter"] = {"avatar": avatar}
    project_store.save_brief(brief, expected_revision=project.revision)
    # Save capabilities (with or without HeyGen configured).
    caps_payload = _fixture("client-capabilities-v1_1.json")
    if not configured:
        caps_payload = {
            k: v for k, v in caps_payload.items() if k != "third_party_processors"
        }
    project = project_store.load()
    project_store.save_capabilities(
        parse_client_capabilities(caps_payload),
        expected_revision=project.revision,
    )
    # When configured, the stored snapshot's HeyGen claim must be "still live"
    # for generate to keep it. The probe mechanics are independently tested
    # (test_capabilities_v1_1.py); here we stub the liveness check so the D13
    # trigger logic is isolated from probe I/O.
    if configured:
        monkeypatch.setattr(
            "lecturecast.commands.director._stored_heygen_still_live",
            lambda document, directory: True,
        )
    capture = _Capture(protocol_version=protocol_version)
    import lecturecast.commands.director as d
    d._make_client = lambda _url: DirectorClient(_url, transport=capture)
    return capture, store


def _invoke_generate(tmp_path: Path, *extra: str) -> Any:
    return runner.invoke(
        app, ["director", "generate", str(tmp_path), *extra, "--json"]
    )


def test_generate_fires_d13_card_when_photo_and_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="photo", configured=False)
    result = _invoke_generate(tmp_path)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    # Card fired: phase + host_choice next_action, create_generation NOT called.
    assert body["workflow"]["phase"] == "digital_human_decision_required"
    assert body["workflow"]["next_action"]["id"] == "director.digital_human.decide"
    assert body["workflow"]["next_action"]["kind"] == "host_choice"
    assert capture.calls == []  # paid call intercepted


def test_generate_skips_card_for_avatar_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="none", configured=False)
    result = _invoke_generate(tmp_path)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    # M1 path: no card, create_generation called once.
    assert body["workflow"]["phase"] != "digital_human_decision_required"
    assert len(capture.calls) == 1
    assert capture.calls[0]["url"].endswith("/director/sessions/s1/generations")


def test_generate_skips_card_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="photo", configured=True)
    result = _invoke_generate(tmp_path)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    # Configured: no card even though avatar=photo; create_generation called.
    assert body["workflow"]["phase"] != "digital_human_decision_required"
    assert len(capture.calls) == 1


def test_generate_flag_skips_card_and_payload_omits_heygen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="photo", configured=False)
    result = _invoke_generate(tmp_path, "--accept-digital-human-downgrade")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["workflow"]["phase"] != "digital_human_decision_required"
    # Option B proceeds to create_generation; payload STILL omits HeyGen
    # (no false configured=true — §0 Principle 6).
    assert len(capture.calls) == 1
    caps_sent = capture.calls[0]["payload"]["capabilities"]
    assert "third_party_processors" not in caps_sent


def test_generate_guard_refuses_present_but_not_configured_processor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13 payload-omission guard (§0.3, defense-in-depth): a v1.1 capabilities
    doc with a present-but-not-configured third_party_processors entry — a state
    capture never produces (heygen_processor returns None or configured=True)
    but the schema permits (configured: type boolean) and _stored_heygen_still_live
    reuses unchanged ("nothing to invalidate") — is refused at the upload
    boundary. create_generation is NOT called; fail-closed manifest_incompatible.
    This makes the payload-omission invariant locally enforceable rather than
    relying on capture-is-clean alone."""
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="photo", configured=False)
    # Overwrite with a schema-valid but inconsistent doc (configured=False entry).
    # save_capabilities re-binds the digest so the doc loads cleanly.
    project_store = ProjectStore(tmp_path)
    project = project_store.load()
    bad_caps = _fixture("client-capabilities-v1_1.json")
    bad_caps["third_party_processors"][0]["configured"] = False
    project_store.save_capabilities(
        parse_client_capabilities(bad_caps), expected_revision=project.revision,
    )
    # Option B (consented) → D13 card skipped → guard reached → refuse.
    result = _invoke_generate(tmp_path, "--accept-digital-human-downgrade")
    assert result.exit_code != 0
    body = json.loads(result.output)
    assert body["code"] == "manifest_incompatible"
    assert capture.calls == []  # create_generation NOT called — fail-closed


def test_generate_guard_refuses_empty_third_party_processors_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D13 guard catches the empty-list sub-clause Codex round-2 named.
    third_party_processors has maxItems:4 but NO minItems
    (client-capabilities.schema.json:318-325) — distinct from the
    [configured:false] entry case. capture never produces [] (capabilities.py
    sets [processor] only when non-None), but a crafted save_capabilities can
    store it (digest re-binds). The guard must reject it: key present +
    _d13_heygen_configured([]) = any(... for p in [] or []) = False -> fire.
    Locks this sub-clause against a future predicate refactor that
    special-cases [] vs [configured:false]."""
    capture, _ = _setup_d13_project(tmp_path, monkeypatch, avatar="photo", configured=False)
    project_store = ProjectStore(tmp_path)
    project = project_store.load()
    empty_caps = _fixture("client-capabilities-v1_1.json")
    empty_caps["third_party_processors"] = []  # schema-valid (no minItems)
    project_store.save_capabilities(
        parse_client_capabilities(empty_caps), expected_revision=project.revision,
    )
    result = _invoke_generate(tmp_path, "--accept-digital-human-downgrade")
    assert result.exit_code != 0
    body = json.loads(result.output)
    assert body["code"] == "manifest_incompatible"
    assert capture.calls == []  # create_generation NOT called — fail-closed


def test_generate_skips_card_for_v1_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture, _ = _setup_d13_project(
        tmp_path, monkeypatch, avatar="photo", configured=False, protocol_version="1.0"
    )
    result = _invoke_generate(tmp_path)
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    # v1.0 guard: no HeyGen concept, card never fires, create_generation called.
    assert body["workflow"]["phase"] != "digital_human_decision_required"
    assert len(capture.calls) == 1


# ===========================================================================
# Integration tests: director digital-human decide (routing)
# ===========================================================================

def _setup_decide_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> DirectorStateStore:
    """A confirmed v1.1 project for the decide routing command."""
    store = DirectorStateStore(tmp_path)
    store.project.init(name="D13")
    store.create(
        server_url="https://api.test",
        session={
            "session_id": "s1", "status": "confirmed", "brief_version": 1,
            "catalog_version": "cv", "updated_at": NOW,
        },
        adapter_kind="codex", adapter_version="1.0.0", protocol_version="1.1",
    )
    return store


def test_decide_configure_routes_to_doctor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_decide_project(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["director", "digital-human", "decide", str(tmp_path),
         "--choice", "configure", "--json"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["workflow"]["phase"] == "digital_human_configure_required"
    action = body["workflow"]["next_action"]
    assert action["id"] == "lecturecast.doctor"
    assert action["kind"] == "command"
    assert action["mutates"] is False  # doctor is read-only
    assert action["requires_user_approval"] is False
    assert action["argv"][:2] == ["lecturecast", "doctor"]


def test_decide_downgrade_routes_to_generate_with_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_decide_project(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["director", "digital-human", "decide", str(tmp_path),
         "--choice", "downgrade", "--json"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["workflow"]["phase"] == "credit_approval_required"
    action = body["workflow"]["next_action"]
    assert action["id"] == "director.generate"
    assert action["mutates"] is True  # paid generate
    assert action["requires_user_approval"] is True
    assert "--accept-digital-human-downgrade" in action["argv"]


def test_decide_bogus_choice_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_decide_project(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["director", "digital-human", "decide", str(tmp_path),
         "--choice", "bogus", "--json"],
    )
    # Non-whitelisted choice → LectureCastError (exit 1 via fail()), NOT a bare
    # crash. The choice guard is the type()+whitelist check in the command.
    # fail() emits to stderr → read via result.output (mix_stderr default).
    assert result.exit_code != 0
    body = json.loads(result.output)
    assert body["code"] == "invalid_choice"
