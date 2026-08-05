"""Title reconciliation candidate discovery + claim (§5.5e3d1)."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from lecturecast.consent import (
    CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    CANONICAL_PROVIDER_COST_DISCLOSURE,
    ConsentService,
    DisclosedAsset,
    HeyGenOperationIdentity,
    ThirdPartyTransferDisclosure,
    prepare_operation,
)
from lecturecast.heygen_adapter import (
    TitleCandidate, TitleQueryAdapterError, TitleQueryResult,
)
from lecturecast.operation_repository import (
    OperationIntegrityError,
    OperationRepository,
)

D = "sha256:" + "a" * 64
NOW = "2026-07-29T00:00:00Z"
OWNER = "maintenance-reconcile-w1"
DB_REL = Path(".lecturecast") / "runtime" / "heygen-operations.db"


def Z(seed) -> str:
    return "sha256:" + hashlib.sha256(str(seed).encode()).hexdigest()


def _disclosure() -> ThirdPartyTransferDisclosure:
    return ThirdPartyTransferDisclosure(
        provider="heygen", operation_kind="video",
        disclosure_version="heygen-transfer-2026-07-27",
        disclosed_assets=[DisclosedAsset("portrait_photo", "face.png", D)],
        data_categories=["portrait_image", "facial_biometric_template"],
        provider_cost_disclosure=CANONICAL_PROVIDER_COST_DISCLOSURE,
        agentmesh_non_processor_disclosure=CANONICAL_AGENTMESH_NON_PROCESSOR_DISCLOSURE,
    )


def _grant(svc, dig, gen="gen_1"):
    prepared = prepare_operation(HeyGenOperationIdentity(
        operation_kind="video", generation_id=gen, manifest_digest=dig["manifest_digest"],
        request_digest=dig["request_digest"], credential_profile_id="heygen_env_default",
        orchestration_plan_digest=dig["orch_digest"], endpoint="/v3/videos"))
    svc.record_decision(prepared=prepared, disclosure=_disclosure(), decision="granted",
                        creative_brief_digest=dig["brief_digest"], decision_at=NOW)
    return prepared


def _seed_ambiguous(tmp_path: Path, status="submit_pending", gen="gen_1"):
    """An operation whose submit attempt may have reached HeyGen: attempt set,
    lease expired, no video resource (unknown id)."""
    svc = ConsentService(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    prepared = _grant(svc, dig, gen)
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute(
        "UPDATE heygen_operations SET status = ?, attempt_started_at = ?, "
        "lease_owner = ?, lease_expires_at = ?, lease_fence = 1 WHERE operation_id = ?",
        (status, "2026-07-28T00:00:00+00:00", "maintenance-submit-dead",
         "2026-07-28T00:01:00+00:00", prepared.operation_id),
    )
    db.commit()
    db.close()
    return prepared


# ---- Title type hardening --------------------------------------------

def test_title_candidate_rejects_empty_and_bad_status():
    with pytest.raises(ValueError):
        TitleCandidate(remote_id="", title="t", created_at=NOW, provider_status="processing")
    with pytest.raises(ValueError):
        TitleCandidate(remote_id="r", title="t", created_at=NOW, provider_status="bogus")
    with pytest.raises(ValueError):
        TitleCandidate(remote_id="r", title="t", created_at="2026-07-29T00:00:00", provider_status="processing")  # naive


def test_title_query_result_requires_bool_and_unique_remote_ids():
    TitleCandidate(remote_id="r1", title="t", created_at=NOW, provider_status="processing")
    with pytest.raises(TypeError):
        TitleQueryResult(query_complete="yes")  # type: ignore[arg-type]
    c1 = TitleCandidate(remote_id="r1", title="t", created_at=NOW, provider_status="processing")
    c2 = TitleCandidate(remote_id="r1", title="t", created_at=NOW, provider_status="processing")
    with pytest.raises(ValueError):
        TitleQueryResult(query_complete=True, candidates=(c1, c2))  # duplicate remote_id


def test_title_query_adapter_error_rejects_string_retryable_and_unknown_code():
    with pytest.raises(TypeError):
        TitleQueryAdapterError(code="connection_error", retryable="true")
    with pytest.raises(ValueError):
        TitleQueryAdapterError(code="bogus", retryable=True)


# ---- candidate discovery ----------------------------------------------

def test_find_candidates_returns_ambiguous_unknown_id(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="submit_pending")
    repo = OperationRepository(tmp_path)
    cands = repo.find_reconciliation_candidates(NOW)
    ids = [c.operation_id for c in cands]
    assert prepared.operation_id in ids
    cand = next(c for c in cands if c.operation_id == prepared.operation_id)
    assert cand.heygen_title == f"lecturecast:{prepared.operation_id}"
    assert cand.attempt_started_at is not None


def test_find_candidates_excludes_known_id_and_fresh(tmp_path: Path):
    svc = ConsentService(tmp_path)
    repo = OperationRepository(tmp_path)
    dig = {"brief_digest": Z(1), "manifest_digest": Z(2), "orch_digest": Z(3), "request_digest": Z(4)}
    # fresh submit_pending (no attempt) — not a candidate
    fresh = _grant(svc, dig, gen="gen_fresh")
    # ambiguous but already has a video resource (known id) — not a candidate
    known = _seed_ambiguous(tmp_path, status="reconciliation_required", gen="gen_known")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, "
               "remote_id, retention_mode, created_by_operation_id, created_at, updated_at) "
               "VALUES (?,?,?,?,?,?,?)",
               ("heygen_env_default", "video", "hg_known", "ephemeral", known.operation_id, NOW, NOW))
    rid = db.execute("SELECT resource_id FROM heygen_remote_resources").fetchone()[0]
    db.execute("INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) VALUES (?,?,?)",
               (rid, known.operation_id, NOW))
    db.commit()
    db.close()
    cands = [c.operation_id for c in repo.find_reconciliation_candidates(NOW)]
    assert fresh.operation_id not in cands
    assert known.operation_id not in cands


# ---- reconcile claim --------------------------------------------------

def test_claim_reconcile_flips_ambiguous_submit_pending(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="submit_pending")
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "claimed"
    assert claim.fence == 2
    assert claim.heygen_title == f"lecturecast:{prepared.operation_id}"
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT status, lease_owner, lease_fence FROM heygen_operations WHERE operation_id = ?",
                     (prepared.operation_id,)).fetchone()
    db.close()
    assert row["status"] == "reconciliation_required"  # flipped
    assert row["lease_owner"] == OWNER


def test_claim_reconcile_busy_while_active_lease(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="reconciliation_required")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ? WHERE operation_id = ?",
               ("maintenance-reconcile-other", "2026-07-29T00:05:00+00:00", prepared.operation_id))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "busy"


def test_claim_reconcile_not_ready_for_known_id(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="reconciliation_required", gen="gen_k")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("INSERT INTO heygen_remote_resources (credential_profile_id, resource_kind, remote_id, "
               "retention_mode, created_by_operation_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
               ("heygen_env_default", "video", "hg_k", "ephemeral", prepared.operation_id, NOW, NOW))
    rid = db.execute("SELECT resource_id FROM heygen_remote_resources").fetchone()[0]
    db.execute("INSERT INTO heygen_resource_operation_refs (resource_id, operation_id, created_at) VALUES (?,?,?)",
               (rid, prepared.operation_id, NOW))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "not_ready"  # has video resource → use poll, not reconcile


def test_claim_reconcile_fail_closed_on_half_lease(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="reconciliation_required")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = NULL WHERE operation_id = ?",
               (OWNER, prepared.operation_id))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        with pytest.raises(OperationIntegrityError):
            repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)


def test_claim_reconcile_retry_wait_during_backoff(tmp_path: Path):
    prepared = _seed_ambiguous(tmp_path, status="reconciliation_required")
    db = sqlite3.connect(str(tmp_path / DB_REL))
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("UPDATE heygen_operations SET next_retry_at = ? WHERE operation_id = ?",
               ("2026-07-29T00:05:00+00:00", prepared.operation_id))
    db.commit()
    db.close()
    repo = OperationRepository(tmp_path)
    with repo.begin_immediate() as conn:
        claim = repo.claim_reconcile_in_tx(conn, prepared.operation_id, OWNER, NOW, 60)
    assert claim.status == "retry_wait"
