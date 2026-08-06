"""ConsentService.record_decision contract tests (§5.5e2b)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentConflictError,
    ConsentDisclosureDriftError,
    ConsentService,
    ConsentStateError,
    DisclosedAsset,
    HeyGenOperationIdentity,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)

D = "sha256:" + "a" * 64
BRIEF = "sha256:" + "b" * 64
REQ = "sha256:" + "c" * 64
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


def _db(project: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(project / DB_REL))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _prepared(**over) -> object:
    base = dict(
        operation_kind="video", generation_id="gen_1", manifest_digest=D,
        request_digest=REQ, credential_profile_id="heygen_env_default",
        orchestration_plan_digest=BRIEF, endpoint="/v3/videos",
    )
    base.update(over)
    return prepare_operation(HeyGenOperationIdentity(**base))


def _disclosure(**over) -> ThirdPartyTransferDisclosure:
    base = dict(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    )
    base.update(over)
    return ThirdPartyTransferDisclosure(**base)


def test_fresh_grant_records_operation_receipt_and_pointer(tmp_path: Path):
    svc = ConsentService(tmp_path)
    res = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    assert res.decision == "granted" and res.status == "granted"
    assert res.idempotent is False
    assert res.consented_at == "2026-07-29T00:00:00Z"
    assert res.receipt_digest.startswith("sha256:")

    db = _db(tmp_path)
    op = db.execute("SELECT * FROM heygen_operations WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert op["status"] == "submit_pending"
    assert op["consent_receipt_digest"] == res.receipt_digest
    rc = db.execute("SELECT * FROM heygen_consent_receipts WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert rc["status"] == "granted"
    assert rc["consented_at"] == "2026-07-29T00:00:00Z"
    assert rc["withdrawn_at"] is None
    db.close()


def test_fresh_decline_cancels_operation_and_leaves_pointer_null(tmp_path: Path):
    svc = ConsentService(tmp_path)
    res = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="declined",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    assert res.decision == "declined" and res.status == "declined"
    db = _db(tmp_path)
    op = db.execute("SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert op["status"] == "cancelled"
    assert op["consent_receipt_digest"] is None
    rc = db.execute("SELECT status FROM heygen_consent_receipts WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert rc["status"] == "declined"
    db.close()


def test_grant_replay_is_idempotent_and_keeps_original_timestamp(tmp_path: Path):
    svc = ConsentService(tmp_path)
    first = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    # Same decision + disclosure, different decision_at → replay.
    second = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:30Z",
    )
    assert second.idempotent is True
    assert second.receipt_digest == first.receipt_digest
    assert second.consented_at == "2026-07-29T00:00:00Z"  # original kept
    db = _db(tmp_path)
    n = db.execute("SELECT COUNT(*) FROM heygen_consent_receipts WHERE operation_id = ?",
                   (first.operation_id,)).fetchone()[0]
    assert n == 1
    db.close()


def test_decline_replay_is_idempotent(tmp_path: Path):
    svc = ConsentService(tmp_path)
    first = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="declined",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    second = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="declined",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:30Z",
    )
    assert second.idempotent is True
    assert second.receipt_digest == first.receipt_digest


def test_replay_with_changed_disclosure_is_drift_and_rolls_back(tmp_path: Path):
    svc = ConsentService(tmp_path)
    first = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    # A different asset digest → different disclosure content.
    drifted = _disclosure(disclosed_assets=[
        DisclosedAsset("portrait_photo", "face.png", "sha256:" + "e" * 64)])
    with pytest.raises(ConsentDisclosureDriftError):
        svc.record_decision(
            prepared=_prepared(), disclosure=drifted, decision="granted",
            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
        )
    # Original receipt untouched.
    db = _db(tmp_path)
    rc = db.execute("SELECT receipt_digest, consented_at FROM heygen_consent_receipts WHERE operation_id = ?",
                    (first.operation_id,)).fetchone()
    assert rc["receipt_digest"] == first.receipt_digest
    assert rc["consented_at"] == "2026-07-29T00:00:00Z"
    db.close()


def test_untrusted_disclosure_text_rejected(tmp_path: Path):
    svc = ConsentService(tmp_path)
    bad = _disclosure(provider_cost_disclosure="x")
    with pytest.raises(ConsentDisclosureDriftError):
        svc.record_decision(
            prepared=_prepared(), disclosure=bad, decision="granted",
            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
        )
    # Rejected before any DB write — no journal exists.
    assert not (tmp_path / DB_REL).exists()


def test_cross_object_kind_mismatch_rejected(tmp_path: Path):
    svc = ConsentService(tmp_path)
    pre = _prepared()           # identity operation_kind == "video"
    disc = _disclosure()        # disclosure operation_kind == "video"
    # Both kinds are "video" by construction (closed vocab). Force a divergence
    # to prove the cross-object guard fires before any DB write.
    object.__setattr__(disc, "operation_kind", "video_other")
    with pytest.raises(ConsentDisclosureDriftError):
        svc.record_decision(prepared=pre, disclosure=disc, decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # Rejected before any DB write.
    assert not (tmp_path / DB_REL).exists()


def test_declined_to_granted_when_pristine(tmp_path: Path):
    svc = ConsentService(tmp_path)
    svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="declined",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    res = svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:01:00Z",
    )
    assert res.decision == "granted" and res.idempotent is False
    db = _db(tmp_path)
    op = db.execute("SELECT status, consent_receipt_digest FROM heygen_operations WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert op["status"] == "submit_pending"
    assert op["consent_receipt_digest"] == res.receipt_digest
    rc = db.execute("SELECT status, receipt_digest FROM heygen_consent_receipts WHERE operation_id = ?",
                    (res.operation_id,)).fetchone()
    assert rc["status"] == "granted"
    assert rc["receipt_digest"] == res.receipt_digest
    db.close()


def test_declined_to_granted_blocked_when_not_pristine(tmp_path: Path):
    svc = ConsentService(tmp_path)
    svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="declined",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    db = _db(tmp_path)
    db.execute("UPDATE heygen_operations SET submit_attempts = 1 WHERE operation_id = ?",
               (_prepared().operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentStateError):
        svc.record_decision(
            prepared=_prepared(), disclosure=_disclosure(), decision="granted",
            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:01:00Z",
        )


def test_granted_to_declined_not_allowed(tmp_path: Path):
    svc = ConsentService(tmp_path)
    svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    with pytest.raises(ConsentStateError):
        svc.record_decision(
            prepared=_prepared(), disclosure=_disclosure(), decision="declined",
            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:01:00Z",
        )


def test_immutable_field_conflict_detected(tmp_path: Path):
    svc = ConsentService(tmp_path)
    other = _prepared(request_digest="sha256:" + "9" * 64)  # different identity
    real_pre = _prepared()
    svc.record_decision(prepared=other, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # Force the existing row to collide on operation_id with real_pre while its
    # request_digest differs — emulates a re-keyed operation. FK must be off for
    # the re-key (both rows reference each other).
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET operation_id = ? WHERE operation_id = ?",
               (real_pre.operation_id, other.operation_id))
    db.execute("UPDATE heygen_consent_receipts SET operation_id = ? WHERE operation_id = ?",
               (real_pre.operation_id, other.operation_id))
    db.commit()
    db.close()
    with pytest.raises(ConsentConflictError):
        svc.record_decision(prepared=real_pre, disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_write_tightens_db_permissions(tmp_path: Path):
    svc = ConsentService(tmp_path)
    svc.record_decision(
        prepared=_prepared(), disclosure=_disclosure(), decision="granted",
        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z",
    )
    import os
    import stat
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(tmp_path / DB_REL).st_mode)
        assert mode == 0o600


# ---- §5.5e2b round-2: heygen_title conflict, pristine-attach, integrity ---


def _seed_operation(db, prepared, *, status="submit_pending", consent_ptr=None):
    db.execute(
        "INSERT INTO heygen_operations (operation_id, kind, endpoint, segment_id, "
        "generation_id, manifest_digest, orchestration_plan_digest, request_digest, "
        "idempotency_key, heygen_title, credential_profile_id, consent_receipt_digest, "
        "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prepared.operation_id, prepared.identity.operation_kind, prepared.identity.endpoint,
         prepared.identity.segment_id, prepared.identity.generation_id,
         prepared.identity.manifest_digest, prepared.identity.orchestration_plan_digest,
         prepared.identity.request_digest, prepared.idempotency_key, prepared.heygen_title,
         prepared.identity.credential_profile_id, consent_ptr, status,
         "2026-07-29T00:00:00Z", "2026-07-29T00:00:00Z"),
    )
    db.commit()


def test_heygen_title_collision_raises_conflict(tmp_path: Path):
    svc = ConsentService(tmp_path)
    other = _prepared(request_digest="sha256:" + "9" * 64)
    svc.record_decision(prepared=other, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # Force the existing row to collide on heygen_title with the new prepared.
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET heygen_title = ? WHERE operation_id = ?",
               (_prepared().heygen_title, other.operation_id))
    db.commit()
    db.close()
    with pytest.raises(ConsentConflictError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_attach_receipt_to_progressed_operation_rejected(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError  # noqa: F401
    from lecturecast.heygen_journal import init_database
    init_database(tmp_path).close()
    db = _db(tmp_path)
    _seed_operation(db, _prepared(), status="submitted")  # progressed past consent gate
    db.close()
    svc = ConsentService(tmp_path)
    with pytest.raises(ConsentStateError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_tampered_receipt_digest_fail_closed(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError
    svc = ConsentService(tmp_path)
    res = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                              creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_consent_receipts SET receipt_digest = ? WHERE operation_id = ?",
               ("sha256:" + "0" * 64, res.operation_id))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_granted_receipt_with_cancelled_operation_fail_closed(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError
    svc = ConsentService(tmp_path)
    res = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                              creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET status = 'cancelled' WHERE operation_id = ?",
               (res.operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_declined_receipt_with_non_null_pointer_fail_closed(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError
    svc = ConsentService(tmp_path)
    res = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="declined",
                              creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET consent_receipt_digest = ? WHERE operation_id = ?",
               ("sha256:" + "f" * 64, res.operation_id))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="declined",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


# ---- §5.5e2b round-3: post-submit lifecycle, withdrawn digest, refs guard ---


T = "2026-07-29T00:00:00Z"


def _fk_off(db_path):
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA foreign_keys = OFF")
    return db


def test_granted_replay_allowed_across_post_submit_lifecycle(tmp_path: Path):
    svc = ConsentService(tmp_path)
    first = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                                creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # The operation has legitimately progressed past submit_pending; the grant
    # must still stand and replays must be idempotent.
    db = _fk_off(tmp_path / DB_REL)
    db.execute("UPDATE heygen_operations SET status = 'processing' WHERE operation_id = ?",
               (first.operation_id,))
    db.commit()
    db.close()
    second = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                                 creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:30Z")
    assert second.idempotent is True
    assert second.receipt_digest == first.receipt_digest


def test_withdrawn_receipt_validates_against_original_grant_digest(tmp_path: Path):
    svc = ConsentService(tmp_path)
    first = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                                creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # Simulate the e2c withdraw lifecycle: receipt withdrawn, pointer cleared.
    db = _fk_off(tmp_path / DB_REL)
    db.execute("UPDATE heygen_consent_receipts SET status = 'withdrawn', withdrawn_at = ? "
               "WHERE operation_id = ?", (T, first.operation_id))
    db.execute("UPDATE heygen_operations SET consent_receipt_digest = NULL WHERE operation_id = ?",
               (first.operation_id,))
    db.commit()
    db.close()
    # Validation must recompute against decision="granted" (withdrawal is a
    # lifecycle state, not a new decision) — no IntegrityError; the call fails
    # later with the "cannot record on withdrawn" state error.
    with pytest.raises(ConsentStateError, match="withdrawn"):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:30Z")


def test_declined_to_granted_blocked_by_lease_fence(tmp_path: Path):
    svc = ConsentService(tmp_path)
    svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="declined",
                        creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    db = _fk_off(tmp_path / DB_REL)
    db.execute("UPDATE heygen_operations SET lease_fence = 1 WHERE operation_id = ?",
               (_prepared().operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentStateError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:01:00Z")


def test_resource_operation_ref_blocks_attach(tmp_path: Path):
    from lecturecast.heygen_journal import init_database
    init_database(tmp_path).close()
    pre = _prepared()
    db = _db(tmp_path)
    _seed_operation(db, pre, status="cancelled")  # pristine otherwise
    db.execute(
        "INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
        "remote_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("heygen_env_default", "video", "rem_1", T, T),
    )
    rid = db.execute("SELECT resource_id FROM heygen_remote_resources").fetchone()[0]
    db.execute(
        "INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) "
        "VALUES (?, ?, ?)",
        (rid, pre.operation_id, T),
    )
    db.commit()
    db.close()
    svc = ConsentService(tmp_path)
    # The ref-only association path must block attaching a fresh receipt.
    with pytest.raises(ConsentStateError):
        svc.record_decision(prepared=pre, disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")


def test_granted_receipt_with_unknown_operation_status_fail_closed(tmp_path: Path):
    from lecturecast.consent import ConsentIntegrityError
    svc = ConsentService(tmp_path)
    first = svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                                creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:00Z")
    # Bypass the CHECK constraint to plant an operation status the validator
    # must reject on its own (not trust the DB constraint to have held).
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA ignore_check_constraints = ON")
    db.execute("UPDATE heygen_operations SET status = 'bogus' WHERE operation_id = ?",
               (first.operation_id,))
    db.commit()
    db.close()
    with pytest.raises(ConsentIntegrityError):
        svc.record_decision(prepared=_prepared(), disclosure=_disclosure(), decision="granted",
                            creative_brief_digest=BRIEF, decision_at="2026-07-29T00:00:30Z")
