"""ConsentService withdraw + validate_submit_consent guard tests (§5.5e2c)."""

from __future__ import annotations

import copy
import sqlite3
import threading
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentConflictError,
    ConsentIntegrityError,
    ConsentService,
    ConsentStateError,
    DisclosedAsset,
    HeyGenOperationIdentity,
    PreparedOperation,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.protocol.canonical import canonical_digest

D = "sha256:" + "a" * 64
GEN = "gen_1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


class _Doc:
    """Lightweight stand-in for a ProtocolDocument. The guard recomputes digests
    from model_dump() and reads .payload; it does NOT validate schema or
    signatures (that is e3's job before calling the guard)."""

    def __init__(self, payload: dict):
        self._payload = payload

    def model_dump(self) -> dict:
        return copy.deepcopy(self._payload)

    @property
    def payload(self) -> dict:
        return self._payload


def _chain(*, avatar="photo", consent_status="granted", consented_at="2026-07-29T00:00:00Z",
           gen=GEN):
    brief_p = {
        "schema_version": "1.1",
        "brief_id": "b1",
        "presenter": {
            "avatar": avatar,
            "voice_mode": "own_voice",
            "third_party_processing": {
                "provider": "heygen",
                "credential_mode": "byo_local",
                "consent_status": consent_status,
                "disclosure_version": "heygen-transfer-2026-07-27",
                "consented_at": consented_at,
            },
        },
    }
    brief = _Doc(brief_p)
    brief_digest = canonical_digest(brief)
    manifest_p = {"generation_id": gen, "brief_digest": brief_digest, "scenes": [], "outputs": []}
    manifest = _Doc(manifest_p)
    manifest_digest = canonical_digest(manifest)
    orch_p = {
        "generation_id": gen,
        "production_manifest_digest": manifest_digest,
        "presenter_plan_digest": "sha256:" + "p" * 64,
    }
    orch = _Doc(orch_p)
    orch_digest = canonical_digest(orch)
    request_descriptor = {"video_inputs": {"avatar": "photo"}, "title": "lecturecast:t"}
    request_digest = canonical_digest(request_descriptor)
    return brief, manifest, orch, request_descriptor, {
        "brief_digest": brief_digest, "manifest_digest": manifest_digest,
        "orch_digest": orch_digest, "request_digest": request_digest,
    }


def _identity(digests, gen=GEN) -> HeyGenOperationIdentity:
    return HeyGenOperationIdentity(
        operation_kind="video", generation_id=gen,
        manifest_digest=digests["manifest_digest"],
        request_digest=digests["request_digest"],
        credential_profile_id="heygen_env_default",
        orchestration_plan_digest=digests["orch_digest"], endpoint="/v3/videos",
    )


def _disclosure() -> ThirdPartyTransferDisclosure:
    return ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    )


def _grant(svc, prepared, brief_digest):
    return svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                               creative_brief_digest=brief_digest, decision_at="2026-07-29T00:00:00Z")


def _fk_off(project: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(project / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    return db


# ---- withdraw ----------------------------------------------------------

def test_withdraw_pristine_cancels_operation_and_clears_pointer(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    res = svc.withdraw(prepared.operation_id)
    assert res.cleanup_required is False
    assert res.idempotent is False
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.row_factory = sqlite3.Row
    op = db.execute("SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
                    (prepared.operation_id,)).fetchone()
    rc = db.execute("SELECT status, withdrawn_at FROM heygen_consent_receipts WHERE operation_id = ?",
                    (prepared.operation_id,)).fetchone()
    db.close()
    assert op["status"] == "cancelled"
    assert op["consent_receipt_digest"] is None
    assert rc["status"] == "withdrawn"
    assert rc["withdrawn_at"] is not None


@pytest.mark.parametrize("progressed", ["submitted", "failed", "submit_attempts", "remote_ref"])
def test_withdraw_non_pristine_requires_cleanup(tmp_path: Path, progressed: str):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    db = _fk_off(tmp_path)
    if progressed == "submit_attempts":
        db.execute("UPDATE heygen_operations SET submit_attempts = 1 WHERE operation_id = ?",
                   (prepared.operation_id,))
    elif progressed == "remote_ref":
        db.execute(
            "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, remote_id, "
            "created_by_operation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("heygen_env_default", "video", "rem_1", prepared.operation_id,
             "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
        )
    else:
        db.execute("UPDATE heygen_operations SET status = ? WHERE operation_id = ?",
                   (progressed, prepared.operation_id))
    db.commit()
    db.close()
    res = svc.withdraw(prepared.operation_id)
    assert res.cleanup_required is True
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.row_factory = sqlite3.Row
    op = db.execute("SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
                    (prepared.operation_id,)).fetchone()
    db.close()
    # operation status preserved (not silently cancelled), pointer cleared
    assert op["consent_receipt_digest"] is None
    if progressed not in ("submitted", "failed"):
        assert op["status"] != "cancelled"


def test_withdraw_replay_keeps_original_withdrawn_at(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    first = svc.withdraw(prepared.operation_id)
    second = svc.withdraw(prepared.operation_id)
    assert second.idempotent is True
    assert second.withdrawn_at == first.withdrawn_at
    assert second.cleanup_required is True  # already withdrawn ⇒ not pristine


def test_withdraw_declined_or_missing_rejected(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="declined",
                        creative_brief_digest=dig["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    with pytest.raises(ConsentStateError):
        svc.withdraw(prepared.operation_id)
    with pytest.raises(ConsentStateError):
        svc.withdraw("lc_hg_nonexistent")


def test_withdraw_fail_closed_on_tampered_receipt(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    db = _fk_off(tmp_path)
    db.execute("UPDATE heygen_consent_receipts SET disclosed_assets_json = '[]' WHERE operation_id = ?",
               (prepared.operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.withdraw(prepared.operation_id)
    # State unchanged: still granted, pointer intact.
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.row_factory = sqlite3.Row
    rc = db.execute("SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
                    (prepared.operation_id,)).fetchone()
    db.close()
    assert rc["status"] == "granted"


def test_concurrent_withdraw_transitions_once(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        results.append(svc.withdraw(prepared.operation_id))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    transitions = [r for r in results if not r.idempotent]
    replays = [r for r in results if r.idempotent]
    assert len(transitions) == 1
    assert len(replays) == 1
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.row_factory = sqlite3.Row
    rc = db.execute("SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
                    (prepared.operation_id,)).fetchone()
    db.close()
    assert rc["status"] == "withdrawn"


# ---- forged PreparedOperation -----------------------------------------

def test_record_decision_rejects_forged_prepared(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, _, _, _, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    forged = PreparedOperation(
        operation_id="lc_hg_forged",
        idempotency_key=prepared.idempotency_key,
        heygen_title=prepared.heygen_title,
        identity=prepared.identity,
    )
    with pytest.raises(ConsentConflictError):
        svc.record_decision(prepared=forged, disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=dig["brief_digest"], decision_at="2026-07-29T00:00:00Z")


# ---- validate_submit_consent guard ------------------------------------

def _guard_ok(svc, prepared, brief, manifest, orch, request_descriptor):
    return svc.validate_submit_consent(prepared=prepared, brief=brief, manifest=manifest,
                                       orchestration_plan=orch, request_descriptor=request_descriptor)


def test_guard_authorizes_a_properly_chained_submit(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, request_descriptor, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    result = _guard_ok(svc, prepared, brief, manifest, orch, request_descriptor)
    assert result.operation_id == prepared.operation_id
    assert result.manifest_digest == dig["manifest_digest"]
    assert result.orchestration_plan_digest == dig["orch_digest"]
    assert result.request_digest == dig["request_digest"]
    assert result.generation_id == GEN


def test_guard_rejects_brief_not_granted(tmp_path: Path):
    svc = ConsentService(tmp_path)
    for status in ("declined", "not_applicable"):
        brief, manifest, orch, req, dig = _chain(consent_status=status)
        prepared = prepare_operation(_identity(dig))
        _grant(svc, prepared, dig["brief_digest"])
        with pytest.raises(ConsentStateError):
            _guard_ok(svc, prepared, brief, manifest, orch, req)


def test_guard_rejects_brief_avatar_not_photo(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, req, dig = _chain(avatar="none", consent_status="not_applicable")
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    with pytest.raises(ConsentStateError):
        _guard_ok(svc, prepared, brief, manifest, orch, req)


@pytest.mark.parametrize("break_", ["brief_digest", "manifest_digest", "orch_digest",
                                    "request_digest", "manifest_generation", "orch_generation"])
def test_guard_rejects_broken_chain(tmp_path: Path, break_: str):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, req, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    # Mutate one artifact to break the chain after grant.
    if break_ == "brief_digest":
        manifest.payload["brief_digest"] = "sha256:" + "0" * 64
    elif break_ == "manifest_digest":
        # identity carries the old manifest_digest; mutate manifest content.
        manifest.payload["extra"] = "tampered"
    elif break_ == "orch_digest":
        orch.payload["extra"] = "tampered"
    elif break_ == "request_digest":
        req["extra"] = "tampered"
    elif break_ == "manifest_generation":
        manifest.payload["generation_id"] = "gen_other"
    elif break_ == "orch_generation":
        orch.payload["generation_id"] = "gen_other"
    with pytest.raises(ConsentConflictError):
        _guard_ok(svc, prepared, brief, manifest, orch, req)


def test_guard_rejects_forged_prepared(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, req, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    forged = PreparedOperation(
        operation_id="lc_hg_forged", idempotency_key=prepared.idempotency_key,
        heygen_title=prepared.heygen_title, identity=prepared.identity,
    )
    with pytest.raises(ConsentConflictError):
        _guard_ok(svc, forged, brief, manifest, orch, req)


def test_guard_rejects_after_withdraw(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, req, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    _grant(svc, prepared, dig["brief_digest"])
    svc.withdraw(prepared.operation_id)
    # Withdraw cleared the pointer + cancelled the (pristine) operation.
    with pytest.raises(ConsentStateError):
        _guard_ok(svc, prepared, brief, manifest, orch, req)


def test_guard_rejects_unauthorized_operation(tmp_path: Path):
    svc = ConsentService(tmp_path)
    brief, manifest, orch, req, dig = _chain()
    prepared = prepare_operation(_identity(dig))
    # Decline → no consent pointer, operation cancelled.
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="declined",
                        creative_brief_digest=dig["brief_digest"], decision_at="2026-07-29T00:00:00Z")
    with pytest.raises(ConsentStateError):
        _guard_ok(svc, prepared, brief, manifest, orch, req)
