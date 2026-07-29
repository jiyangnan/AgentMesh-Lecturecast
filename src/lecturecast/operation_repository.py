"""HeyGen operation repository — claim/lease/fence primitives (§5.5e3a).

A thin SQL/lease/fence layer over the journal. It holds NO product strategy and
no protocol-model knowledge: the submit consent guard (ConsentService) and the
adapter (e3b) are orchestrated around it by a coordinator, all inside one
BEGIN IMMEDIATE transaction (use repository.begin_immediate()) so there is no
guard→claim race window.

Fence rules (per Codex e3 plan):
- A new claim/reclaim bumps lease_fence by 1.
- A renewal keeps the fence, only extends lease_expires_at.
- An outcome transition is gated by (operation_id, lease_owner, lease_fence,
  expected_status); on success it clears owner/expires but RETAINS the fence.
- The fence never resets to 0; a stale worker's rowcount=0 UPDATE cannot
  overwrite a newer owner.

Critical safety invariant: an operation with attempt_started_at set (a submit
that may have reached HeyGen) is NEVER re-claimable for submit. If the lease is
still active that is reported as 'busy' (another worker is on it — wait); if the
lease expired it is 'ambiguous' (a maybe-sent attempt — route to reconciliation,
never a blind re-submit).

All timestamps are parsed to timezone-aware datetimes, compared as datetimes
(not lexically), and written back in a canonical UTC isoformat.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lecturecast.consent import (
    ConsentService,
    CreativeBriefV1_1,
    OrchestrationPlanV1_1,
    PreparedOperation,
    PresenterPlanV1_1,
    ProductionManifest,
    SubmitConsentResult,
)
from lecturecast.heygen_adapter import HeyGenAdapterError, SubmitAccepted, SubmitOutcome
from lecturecast.heygen_journal import _chmod_secure, init_database

_RUNTIME_DB = Path(".lecturecast") / "runtime" / "heygen-operations.db"
_LEASE_OWNER_RE = re.compile(r"^[A-Za-z0-9_:.\-]{3,96}$")
LEASE_MIN_SECONDS = 30
LEASE_MAX_SECONDS = 3600
NEXT_RETRY_BACKOFF_SECONDS = 60  # linear submit retry backoff (anti-hotloop is e3c)


class OperationError(RuntimeError):
    """Base for operation-layer errors."""


class OperationStateError(OperationError):
    """The requested transition is not allowed from the current state, or the
    journal is in an anomalous topology the repository refuses to overwrite."""


class OperationIntegrityError(OperationError):
    """A stored row is internally inconsistent (e.g. consent pointer ≠ receipt
    digest). Fail-closed rather than trusting the row."""


def _require_lease_owner(owner: str) -> None:
    if not _LEASE_OWNER_RE.fullmatch(owner or ""):
        raise ValueError(f"invalid lease_owner: {owner!r}")


def _check_lease_seconds(seconds: int) -> None:
    if not isinstance(seconds, int) or not (LEASE_MIN_SECONDS <= seconds <= LEASE_MAX_SECONDS):
        raise ValueError(
            f"lease_seconds must be an int in [{LEASE_MIN_SECONDS}, {LEASE_MAX_SECONDS}]"
        )


def _parse_utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not ISO-8601: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return dt.astimezone(timezone.utc)


def _canonical(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# --- result types ------------------------------------------------------


@dataclass(frozen=True)
class ClaimResult:
    operation_id: str
    status: str  # "claimed" | "busy" | "ambiguous" | "not_ready"
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
    """SQL/lease/fence primitives. Use begin_immediate() to open the transaction
    the in_tx primitives require; it guarantees the connection is bound to THIS
    project's journal."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._db_path = self._project_dir / _RUNTIME_DB

    @contextmanager
    def begin_immediate(self):
        conn = init_database(self._project_dir)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            _rollback(conn)
            raise
        finally:
            _chmod_secure(self._db_path)
            conn.close()

    def _require_tx(self, conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise OperationStateError(
                "operation repository primitives require an active transaction"
            )
        # Reject a connection from a different project's journal.
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_file = next((r[2] for r in rows if r[1] == "main"), None)
        if main_file is None:
            raise OperationStateError("connection has no main database")
        try:
            same = Path(main_file).resolve() == self._db_path.resolve()
        except (OSError, ValueError):
            same = False
        if not same:
            raise OperationStateError(
                "connection is not bound to this project's journal"
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
        + attempt_started_at atomically. Classification:
          - active lease + attempt started → 'busy' (wait for the lease)
          - expired/absent lease + attempt started → 'ambiguous' (reconcile)
          - lease present without an attempt → fail-closed (anomalous state)
          - no consent / not submit_pending → 'not_ready'
        A lost concurrent CAS re-reads and classifies the new state rather than
        guessing."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)
        expires_iso = _canonical(now + timedelta(seconds=lease_seconds))

        def _fetch():
            row = conn.execute(
                "SELECT status, consent_receipt_digest, attempt_started_at, lease_owner, "
                "lease_expires_at, lease_fence, submit_attempts FROM heygen_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT status, receipt_digest FROM heygen_consent_receipts "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return row, receipt

        def _classify(row, receipt):
            if row is None or row["status"] != "submit_pending":
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            ptr = row["consent_receipt_digest"]
            if ptr is None or receipt is None:
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            if receipt["status"] != "granted":
                return ClaimResult(operation_id, "not_ready", 0, 0, None)
            if receipt["receipt_digest"] != ptr:
                raise OperationIntegrityError(
                    "consent pointer does not match the receipt digest"
                )
            if row["attempt_started_at"] is not None:
                owner = row["lease_owner"]
                exp = row["lease_expires_at"]
                # A half lease (one of owner/expires set, the other NULL) is a
                # corrupt topology — fail-closed, never silently classify.
                if (owner is None) != (exp is None):
                    raise OperationIntegrityError(
                        f"operation {operation_id} has a half lease state "
                        f"(owner={owner!r}, expires={exp!r})"
                    )
                if owner is None and exp is None:
                    # Attempt with no lease at all — a maybe-sent attempt with
                    # no active holder. Ambiguous; route to reconciliation.
                    return ClaimResult(operation_id, "ambiguous",
                                       row["lease_fence"], row["submit_attempts"], None)
                # Both set — busy if still valid, ambiguous if expired.
                active = _parse_utc(exp) > now
                status = "busy" if active else "ambiguous"
                return ClaimResult(operation_id, status,
                                   row["lease_fence"], row["submit_attempts"], exp)
            # No attempt — a lease without an attempt is an anomalous state we
            # refuse to silently overwrite.
            if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
                raise OperationStateError(
                    f"operation {operation_id} has a lease without an attempt"
                )
            return None  # eligible

        row, receipt = _fetch()
        verdict = _classify(row, receipt)
        if verdict is not None:
            return verdict

        new_fence = row["lease_fence"] + 1
        cur = conn.execute(
            "UPDATE heygen_operations SET lease_owner = ?, lease_expires_at = ?, "
            "lease_fence = ?, submit_attempts = ?, attempt_started_at = ?, "
            "updated_at = ? WHERE operation_id = ? AND status = 'submit_pending' "
            "AND attempt_started_at IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND lease_fence = ?",
            (lease_owner, expires_iso, new_fence, row["submit_attempts"] + 1,
             _canonical(now), _canonical(now), operation_id, row["lease_fence"]),
        )
        if cur.rowcount == 0:
            # Lost a concurrent claim — re-read and classify the new state.
            row2, receipt2 = _fetch()
            v2 = _classify(row2, receipt2)
            if v2 is None:
                # Eligible again after a lost CAS means a concurrent tx rolled
                # back its claim — a state we cannot explain; refuse to guess.
                raise OperationStateError(
                    f"operation {operation_id} became eligible again after a lost CAS"
                )
            return v2
        return ClaimResult(operation_id, "claimed", new_fence,
                           row["submit_attempts"] + 1, expires_iso)

    def renew_lease_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        lease_seconds: int,
    ) -> RenewResult:
        """Extend the lease a submit worker still holds (fence unchanged). The
        lease must belong to the caller, the operation must still be
        submit_pending with an in-flight attempt and a live consent pointer, and
        the lease must not yet have expired (expires > now)."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        _check_lease_seconds(lease_seconds)

        row = conn.execute(
            "SELECT status, lease_owner, lease_fence, lease_expires_at, "
            "attempt_started_at, consent_receipt_digest FROM heygen_operations "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if (
            row is None
            or row["lease_owner"] != lease_owner
            or row["lease_fence"] != fence
        ):
            return RenewResult(operation_id, "not_held", 0, None)
        # A submit renewal only makes sense for an in-flight submit attempt.
        if (
            row["status"] != "submit_pending"
            or row["attempt_started_at"] is None
            or row["consent_receipt_digest"] is None
        ):
            return RenewResult(operation_id, "not_held", fence, row["lease_expires_at"])
        if row["lease_expires_at"] is None or _parse_utc(row["lease_expires_at"]) <= now:
            return RenewResult(operation_id, "expired", fence, row["lease_expires_at"])
        new_expires = _canonical(now + timedelta(seconds=lease_seconds))
        conn.execute(
            "UPDATE heygen_operations SET lease_expires_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ?",
            (new_expires, _canonical(now), operation_id, lease_owner, fence),
        )
        return RenewResult(operation_id, "renewed", fence, new_expires)

    # -- submit outcome (§5.5e3b) ---------------------------------------

    def apply_submit_outcome_in_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome: SubmitAccepted | HeyGenAdapterError,
    ) -> SubmitOutcome:
        """Apply a submit outcome behind a fenced CAS
        (operation_id + lease_owner + lease_fence + status='submit_pending' +
        attempt_started_at set). Clears the lease on every terminal outcome;
        the fence is retained.

        Outcome → status mapping (per Codex e3 plan):
          SubmitAccepted(remote_id)            → submitted (+ atomic video resource/ref)
          SubmitAccepted(empty remote_id)      → reconciliation_required (ambiguous)
          HeyGenAdapterError maybe_sent        → reconciliation_required
          HeyGenAdapterError not_sent retryable→ submit_pending (reset attempt + next_retry_at)
          HeyGenAdapterError not_sent permanent→ failed
        A fence mismatch writes nothing and returns status='fence_conflict'."""
        self._require_tx(conn)
        conn.row_factory = sqlite3.Row
        _require_lease_owner(lease_owner)
        now = _parse_utc(now_iso)
        now_c = _canonical(now)

        target, remote_id, provider_status, last_error, next_retry = self._plan_submit_outcome(
            outcome, now
        )

        # Common: clear lease, set status + updated_at. Extras per target.
        sets = ["status = ?", "lease_owner = NULL", "lease_expires_at = NULL", "updated_at = ?"]
        params: list = [target, now_c]
        if target == "submitted":
            sets += ["submitted_at = ?", "provider_status = ?"]
            params += [now_c, provider_status or ""]
        elif target == "submit_pending":
            sets += ["attempt_started_at = NULL", "next_retry_at = ?", "last_error_code = ?"]
            params += [next_retry, last_error]
        elif target == "failed":
            sets += ["last_error_code = ?", "completed_at = ?"]
            params += [last_error, now_c]
        elif target == "reconciliation_required":
            sets += ["last_error_code = ?"]
            params += [last_error]
        sql = (
            "UPDATE heygen_operations SET " + ", ".join(sets) +
            " WHERE operation_id = ? AND lease_owner = ? AND lease_fence = ? "
            "AND status = 'submit_pending' AND attempt_started_at IS NOT NULL"
        )
        where_params = [operation_id, lease_owner, fence]
        cur = conn.execute(sql, params + where_params)
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT lease_fence FROM heygen_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            cur_fence = row["lease_fence"] if row is not None else 0
            return SubmitOutcome("fence_conflict", cur_fence, None, None, None)

        remote_resource_id = None
        if target == "submitted" and remote_id:
            remote_resource_id = self._write_video_resource(
                conn, operation_id, remote_id, now_c
            )
        return SubmitOutcome(target, fence, remote_resource_id, last_error, next_retry)

    @staticmethod
    def _plan_submit_outcome(outcome, now):
        """Map a typed outcome to (target_status, remote_id, provider_status,
        last_error_code, next_retry_iso)."""
        if isinstance(outcome, SubmitAccepted):
            rid = (outcome.remote_id or "").strip()
            if rid:
                return ("submitted", rid, outcome.provider_status, None, None)
            # Accepted but no remote id — ambiguous; reconcile to learn the truth.
            return ("reconciliation_required", None, "", "unknown", None)
        if isinstance(outcome, HeyGenAdapterError):
            if outcome.submission_certainty == "maybe_sent":
                return ("reconciliation_required", None, "", outcome.code, None)
            # not_sent
            if outcome.retryable:
                retry = _canonical(now + timedelta(seconds=NEXT_RETRY_BACKOFF_SECONDS))
                return ("submit_pending", None, "", outcome.code, retry)
            return ("failed", None, "", outcome.code, None)
        raise TypeError(f"unsupported submit outcome type: {type(outcome)!r}")

    @staticmethod
    def _write_video_resource(conn, operation_id: str, remote_id: str, now_c: str) -> int:
        """Atomically record the remote video resource + its operation ref. The
        UNIQUE(credential_profile_id, resource_kind, remote_id) makes it
        idempotent if the same remote id surfaces again (e.g. after recovery)."""
        conn.execute(
            "INSERT OR IGNORE INTO heygen_remote_resources "
            "(credential_profile_id, resource_kind, remote_id, retention_mode, "
            "created_by_operation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("heygen_env_default", "video", remote_id, "ephemeral", operation_id, now_c, now_c),
        )
        row = conn.execute(
            "SELECT resource_id FROM heygen_remote_resources "
            "WHERE credential_profile_id = ? AND resource_kind = ? AND remote_id = ?",
            ("heygen_env_default", "video", remote_id),
        ).fetchone()
        resource_id = row["resource_id"]
        conn.execute(
            "INSERT OR IGNORE INTO heygen_resource_operation_refs "
            "(resource_id, operation_id, created_at) VALUES (?, ?, ?)",
            (resource_id, operation_id, now_c),
        )
        return resource_id


# --- coordinator -------------------------------------------------------


class SubmitProcessor:
    """Records the outcome of a submit attempt. The adapter call happens
    OUTSIDE any transaction (the worker holds the claim lease across it); this
    method opens its own fenced transaction to apply the outcome."""

    def __init__(self, project_dir: str | Path) -> None:
        self._project_dir = Path(project_dir)
        self._repository = OperationRepository(self._project_dir)

    def record_submit_outcome(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        fence: int,
        now_iso: str,
        outcome: SubmitAccepted | HeyGenAdapterError,
    ) -> SubmitOutcome:
        with self._repository.begin_immediate() as conn:
            return self._repository.apply_submit_outcome_in_tx(
                conn, operation_id, lease_owner, fence, now_iso, outcome,
            )


class SubmitCoordinator:
    """Orchestrates the submit consent guard and the operation claim in ONE
    transaction (repository.begin_immediate), then hands control to the caller
    to invoke the adapter outside the transaction."""

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
        """One transaction: run the full consent guard, then claim. If the guard
        fails or the operation is not claimable, nothing is written. Returns the
        consent proof + claim handle the worker needs to record the outcome."""
        with self._repository.begin_immediate() as conn:
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
        return SubmitClaim(consent=consent, claim=claim)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass
