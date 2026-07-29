"""HeyGen operation repository — claim/lease/fence primitives (§5.5e3a).

A thin SQL/lease/fence layer over the journal. It holds NO product strategy and
no protocol-model knowledge: the submit consent guard (ConsentService) and the
adapter (e3b) are orchestrated around it by a coordinator, all inside one
BEGIN IMMEDIATE transaction so there is no guard→claim race window.

Fence rules (per Codex e3 plan):
- A new claim/reclaim bumps lease_fence by 1.
- A renewal keeps the fence, only extends lease_expires_at.
- An outcome transition is gated by (operation_id, lease_owner, lease_fence,
  expected_status); on success it clears owner/expires but RETAINS the fence.
- The fence never resets to 0; a stale worker's rowcount=0 UPDATE cannot
  overwrite a newer owner.

Critical safety invariant: an operation with attempt_started_at set (a submit
that may have reached HeyGen) is NEVER re-claimable for submit, even after the
lease expired — it must go through title reconciliation. This prevents a blind
double-submit after a crash.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lecturecast.consent import (
    ConsentService,
    ConsentStateError,
    OrchestrationPlanV1_1,
    PreparedOperation,
    PresenterPlanV1_1,
    ProductionManifest,
    SubmitConsentResult,
    CreativeBriefV1_1,
)
from lecturecast.heygen_journal import _chmod_secure, _utc_now, init_database

_RUNTIME_DB = Path(".lecturecast") / "runtime" / "heygen-operations.db"
_LEASE_OWNER_RE = __import__("re").compile(r"^[A-Za-z0-9_:.\-]{3,96}$")


class OperationError(RuntimeError):
    """Base for operation-layer errors."""


class OperationStateError(OperationError):
    """The requested transition is not allowed from the current state."""


def _require_lease_owner(owner: str) -> None:
    if not _LEASE_OWNER_RE.fullmatch(owner or ""):
        raise ValueError(f"invalid lease_owner: {owner!r}")


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return dt.astimezone(timezone.utc)


def _lease_expiry(now_iso: str, seconds: int) -> str:
    if seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    return (_parse_iso(now_iso) + timedelta(seconds=seconds)).isoformat()


# --- result types ------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    operation_id: str
    status: str  # "claimed" | "ambiguous" | "not_ready"
    fence: int
    submit_attempts: int
    lease_expires_at: str | None


@dataclass(frozen=True)
class RenewResult:
    operation_id: str
    status: str  # "renewed" | "not_held" | "expired"
    fence: int
    lease_expires_at: str | None


@dataclass(frozen=True)
class SubmitClaim:
    """The combined output of a coordinator claim: the consent authorization
    proof plus the leased claim handle the worker uses for the outcome write."""

    consent: SubmitConsentResult
    claim: ClaimResult


# --- repository --------------------------------------------------------


class OperationRepository:
    """SQL/lease/fence primitives. Every mutating method runs on a caller-provided
    connection that must already be in a BEGIN IMMEDIATE transaction (so the
    coordinator can keep guard + claim + outcome in one tx)."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._db_path = self._project_dir / _RUNTIME_DB

    @staticmethod
    def _require_tx(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise OperationStateError(
                "operation repository primitives require an active transaction"
            )

    def claim_submit_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> ClaimResult:
        """Claim a granted operation for a submit attempt. Bumps fence + attempts
        + attempt_started_at atomically. Refuses an operation whose
        attempt_started_at is set (a maybe-sent prior attempt) even if its lease
        expired — those must go through reconciliation, never a blind re-submit."""
        self._require_tx(conn)
        _require_lease_owner(lease_owner)
        _parse_iso(now_iso)  # validate
        expires = _lease_expiry(now_iso, lease_seconds)

        row = conn.execute(
            "SELECT status, consent_receipt_digest, attempt_started_at, "
            "lease_owner, lease_expires_at, lease_fence, submit_attempts "
            "FROM heygen_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return ClaimResult(operation_id, "not_ready", 0, 0, None)
        if row["status"] != "submit_pending" or row["consent_receipt_digest"] is None:
            return ClaimResult(operation_id, "not_ready", 0, 0, None)
        if row["attempt_started_at"] is not None:
            # An attempt is in flight (a claim always sets attempt_started_at
            # together with the lease, so a held/expired lease with an attempt
            # means a maybe-sent submit). Never re-claim for submit — reconcile.
            return ClaimResult(operation_id, "ambiguous", 0, 0, None)

        new_fence = row["lease_fence"] + 1
        cur = conn.execute(
            "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
            "lease_fence = ?, submit_attempts = ?, attempt_started_at = ?, "
            "updated_at = ? WHERE operation_id = ? AND status = 'submit_pending' "
            "AND attempt_started_at IS NULL AND lease_fence = ?",
            (lease_owner, expires, new_fence, row["submit_attempts"] + 1,
             now_iso, now_iso, operation_id, row["lease_fence"]),
        )
        if cur.rowcount == 0:
            # Lost a concurrent claim — another worker just claimed (and set an
            # attempt). Treat as ambiguous: do not retry submit blindly.
            return ClaimResult(operation_id, "ambiguous", 0, 0, None)
        return ClaimResult(operation_id, "claimed", new_fence,
                           row["submit_attempts"] + 1, expires)

    def renew_lease_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        lease_seconds: int,
    ) -> RenewResult:
        """Extend the lease of a claim the caller still holds. The fence is
        unchanged. Renewing an already-expired lease fails (the worker must have
        lost the fence to a reclaim, or the lease lapsed)."""
        self._require_tx(conn)
        _require_lease_owner(lease_owner)
        expires = _lease_expiry(now_iso, lease_seconds)

        row = conn.execute(
            "SELECT lease_owner, lease_fence, lease_expires_at FROM heygen_operations "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None or row["lease_owner"] != lease_owner or row["lease_fence"] != fence:
            return RenewResult(operation_id, "not_held", 0, None)
        if row["lease_expires_at"] is None or row["lease_expires_at"] < now_iso:
            return RenewResult(operation_id, "expired", fence, row["lease_expires_at"])
        conn.execute(
            "UPDATE heygen_operations SET lease_expires_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ?",
            (expires, now_iso, operation_id, lease_owner, fence),
        )
        return RenewResult(operation_id, "renewed", fence, expires)


# --- coordinator -------------------------------------------------------


class SubmitCoordinator:
    """Orchestrates the submit consent guard and the operation claim in ONE
    transaction, then hands control to the caller to invoke the adapter outside
    the transaction. Construct with the project directory."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)
        self._consent = ConsentService(self._project_dir)

    def claim_for_submit(
        self,
        *,
        prepared: PreparedOperation,
        brief: CreativeBriefV1_1,
        manifest: ProductionManifest,
        presenter_plan: PresenterPlanV1_1,
        orchestration_plan: OrchestrationPlanV1_1,
        request_descriptor,
        lease_owner: str,
        now_iso: str,
        lease_seconds: int,
    ) -> SubmitClaim:
        """Open one BEGIN IMMEDIATE transaction: run the full consent guard, then
        claim the operation. If the guard fails or the operation is not
        claimable, nothing is written. Returns the consent proof + claim handle
        the worker needs to record the adapter outcome."""
        conn = init_database(self._project_dir)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                consent = self._consent.validate_submit_consent_in_tx(
                    conn,
                    prepared=prepared, brief=brief, manifest=manifest,
                    presenter_plan=presenter_plan, orchestration_plan=orchestration_plan,
                    request_descriptor=request_descriptor,
                )
                claim = self._repository.claim_submit_in_tx(
                    conn, prepared.operation_id, lease_owner, now_iso, lease_seconds,
                )
                if claim.status != "claimed":
                    raise OperationStateError(
                        f"operation {prepared.operation_id} not claimable: {claim.status}"
                    )
                conn.execute("COMMIT")
            except Exception:
                _rollback(conn)
                raise
        finally:
            _chmod_secure(self._repository._db_path)
            conn.close()
        return SubmitClaim(consent=consent, claim=claim)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
