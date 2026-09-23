"""SQLite persistence for reconciliation runs and human review decisions.

A new sync run inserts new rows. Prior approvals/rejections are never
silently overwritten. The schema is intentionally small.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .proposals import STATUS_APPROVED, STATUS_VALUES, Proposal, ProposalBatch

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    scrape_complete INTEGER NOT NULL DEFAULT 0,
    parent_account_id TEXT,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    account_id TEXT,
    facility_url TEXT,
    current_values_json TEXT NOT NULL,
    proposed_values_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    requires_review INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_action ON proposals(action_type);
CREATE INDEX IF NOT EXISTS idx_proposals_run ON proposals(run_id);

CREATE TABLE IF NOT EXISTS application_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    created_account_id TEXT,
    error_text TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE INDEX IF NOT EXISTS idx_application_attempts_proposal
    ON application_attempts(proposal_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_application_attempts_one_execute_in_progress
    ON application_attempts(proposal_id)
    WHERE mode = 'execute' AND state = 'in_progress';

CREATE TABLE IF NOT EXISTS account_write_locks (
    account_id TEXT PRIMARY KEY,
    proposal_id INTEGER NOT NULL,
    attempt_id INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id),
    FOREIGN KEY (attempt_id) REFERENCES application_attempts(id)
);
"""

# Applied only after legacy create_identity_locks tables are migrated/dropped.
CREATE_IDENTITY_LOCKS_SQL = """
CREATE TABLE IF NOT EXISTS create_identity_locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    street_norm TEXT NOT NULL,
    state_norm TEXT NOT NULL,
    city_norm TEXT,
    zip_norm TEXT,
    proposal_id INTEGER NOT NULL,
    attempt_id INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id),
    FOREIGN KEY (attempt_id) REFERENCES application_attempts(id)
);

CREATE INDEX IF NOT EXISTS idx_create_identity_locks_core
    ON create_identity_locks(parent_id, name_norm, street_norm, state_norm);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads(raw: str | None) -> Any:
    if not raw:
        return {}
    return json.loads(raw)


MODE_DRY_RUN = "dry_run"
MODE_EXECUTE = "execute"
STATE_PLANNED = "planned"
STATE_IN_PROGRESS = "in_progress"
STATE_APPLIED = "applied"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
STATE_UNCERTAIN = "uncertain"

CLAIM_CLAIMED = "claimed"
CLAIM_ALREADY_APPLIED = "already_applied"
CLAIM_IN_PROGRESS = "in_progress"
CLAIM_NOT_APPROVED = "not_approved"

# Leave write reservations in place for uncertain outcomes; release only when safe.
# Blocked/failed release only reservations owned by *this* attempt_id so an older
# uncertain attempt's lock is not dropped by a later same-proposal retry.
_WRITE_LOCK_RELEASE_STATES = frozenset({STATE_BLOCKED, STATE_FAILED})

ATTEMPT_MODES = frozenset({MODE_DRY_RUN, MODE_EXECUTE})
ATTEMPT_STATES = frozenset(
    {
        STATE_PLANNED,
        STATE_IN_PROGRESS,
        STATE_APPLIED,
        STATE_BLOCKED,
        STATE_FAILED,
        STATE_UNCERTAIN,
    }
)


@dataclass(frozen=True)
class ClaimResult:
    status: str
    attempt: ApplicationAttempt | None = None


@dataclass(frozen=True)
class CreateIdentityParts:
    """Structured canonical create identity for overlap-aware reservations."""

    parent_id: str
    name_norm: str
    street_norm: str
    state_norm: str
    city_norm: str | None = None
    zip_norm: str | None = None

    def overlaps(self, other: CreateIdentityParts) -> bool:
        """True when two reservations must not both be held.

        Same core identity conflicts unless optional city/ZIP evidence on both
        sides positively proves the facilities are different.
        """
        if (
            self.parent_id != other.parent_id
            or self.name_norm != other.name_norm
            or self.street_norm != other.street_norm
            or self.state_norm != other.state_norm
        ):
            return False
        if self.city_norm and other.city_norm and self.city_norm != other.city_norm:
            return False
        if self.zip_norm and other.zip_norm and self.zip_norm != other.zip_norm:
            return False
        return True


@dataclass(frozen=True)
class ApplicationAttempt:
    id: int
    proposal_id: int
    run_id: int
    action_type: str
    mode: str
    state: str
    created_account_id: str | None
    error_text: str | None
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class StoredProposal:
    id: int
    run_id: int
    action_type: str
    account_id: str | None
    facility_url: str | None
    current_values: dict[str, Any]
    proposed_values: dict[str, Any]
    evidence: dict[str, Any]
    confidence: str
    requires_review: bool
    status: str
    created_at: str
    updated_at: str

    @property
    def reason(self) -> str:
        return str(self.evidence.get("reason") or "")


class ProposalStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            # Base schema first (safe against legacy DBs). Create-identity locks and
            # their structured-column index are applied only after migration.
            conn.executescript(SCHEMA_SQL)
            self._migrate_lock_tables(conn)
            conn.executescript(CREATE_IDENTITY_LOCKS_SQL)
            conn.commit()

    def _migrate_lock_tables(self, conn: sqlite3.Connection) -> None:
        """Drop a legacy opaque-key create_identity_locks table before structured DDL."""
        cols = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(create_identity_locks)").fetchall()
        }
        if not cols:
            return
        if "identity_key" in cols or "name_norm" not in cols:
            conn.execute("DROP TABLE IF EXISTS create_identity_locks")

    def save_run(self, batch: ProposalBatch, *, started_at: str | None = None) -> int:
        """Persist a new reconciliation run and its proposals. Never mutates old rows."""
        started = started_at or batch.generated_at or _now()
        finished = _now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO reconciliation_runs (
                    started_at, finished_at, scrape_complete, parent_account_id,
                    blockers_json, summary_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    started,
                    finished,
                    1 if batch.scrape_complete else 0,
                    batch.parent_account_id,
                    _dumps(batch.blockers),
                    _dumps(batch.summary),
                ),
            )
            run_id = int(cur.lastrowid)
            for proposal in batch.proposals:
                self._insert_proposal(conn, run_id, proposal)
            conn.commit()
        return run_id

    def _insert_proposal(self, conn: sqlite3.Connection, run_id: int, proposal: Proposal) -> None:
        record = proposal.to_record()
        stamp = _now()
        conn.execute(
            """
            INSERT INTO proposals (
                run_id, action_type, account_id, facility_url,
                current_values_json, proposed_values_json, evidence_json,
                confidence, requires_review, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                record["action_type"],
                record["account_id"],
                record["facility_url"],
                _dumps(record["current_values"]),
                _dumps(record["proposed_values"]),
                _dumps(record["evidence"]),
                record["confidence"],
                1 if record["requires_review"] else 0,
                record["status"],
                stamp,
                stamp,
            ),
        )

    def list_proposals(
        self,
        *,
        status: str | None = None,
        action_type: str | None = None,
        run_id: int | None = None,
    ) -> list[StoredProposal]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if action_type:
            clauses.append("action_type = ?")
            params.append(action_type)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM proposals {where} ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def get_proposal(self, proposal_id: int) -> StoredProposal | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def set_status(self, proposal_id: int, status: str) -> StoredProposal:
        if status not in STATUS_VALUES:
            raise ValueError(f"invalid status {status!r}; expected one of {sorted(STATUS_VALUES)}")
        stamp = _now()
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?",
                (status, stamp, proposal_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"proposal {proposal_id} not found")
            conn.commit()
        proposal = self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    def latest_run_id(self) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM reconciliation_runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, finished_at, scrape_complete, parent_account_id
                FROM reconciliation_runs
                ORDER BY id DESC
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "scrape_complete": bool(row["scrape_complete"]),
                "parent_account_id": row["parent_account_id"],
            }
            for row in rows
        ]

    def record_attempt(
        self,
        *,
        proposal_id: int,
        run_id: int,
        action_type: str,
        mode: str,
        state: str,
        created_account_id: str | None = None,
        error_text: str | None = None,
    ) -> ApplicationAttempt:
        if mode not in ATTEMPT_MODES:
            raise ValueError(f"invalid application mode {mode!r}")
        if state not in ATTEMPT_STATES:
            raise ValueError(f"invalid application state {state!r}")
        started = _now()
        finished = started if state != STATE_IN_PROGRESS else None
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO application_attempts (
                    proposal_id, run_id, action_type, mode, state,
                    created_account_id, error_text, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    run_id,
                    action_type,
                    mode,
                    state,
                    created_account_id,
                    error_text,
                    started,
                    finished,
                ),
            )
            attempt_id = int(cur.lastrowid)
            conn.commit()
        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def claim_execute_attempt(
        self,
        *,
        proposal_id: int,
        run_id: int,
        action_type: str,
    ) -> ClaimResult:
        """Atomically claim a proposal for execute mode, or report why it cannot run.

        Uses ``BEGIN IMMEDIATE`` so two processes cannot both insert ``in_progress``.
        An existing ``in_progress`` row is never overwritten: it may already have
        written to the CRM.
        """
        started = _now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            proposal_row = conn.execute(
                "SELECT status FROM proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if proposal_row is None or str(proposal_row["status"]) != STATUS_APPROVED:
                conn.commit()
                return ClaimResult(CLAIM_NOT_APPROVED, None)

            applied = conn.execute(
                """
                SELECT * FROM application_attempts
                WHERE proposal_id = ? AND mode = ? AND state = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (proposal_id, MODE_EXECUTE, STATE_APPLIED),
            ).fetchone()
            if applied is not None:
                conn.commit()
                return ClaimResult(CLAIM_ALREADY_APPLIED, self._row_to_attempt(applied))

            busy = conn.execute(
                """
                SELECT * FROM application_attempts
                WHERE proposal_id = ? AND mode = ? AND state = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (proposal_id, MODE_EXECUTE, STATE_IN_PROGRESS),
            ).fetchone()
            if busy is not None:
                conn.commit()
                return ClaimResult(CLAIM_IN_PROGRESS, self._row_to_attempt(busy))

            cur = conn.execute(
                """
                INSERT INTO application_attempts (
                    proposal_id, run_id, action_type, mode, state,
                    created_account_id, error_text, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    run_id,
                    action_type,
                    MODE_EXECUTE,
                    STATE_IN_PROGRESS,
                    None,
                    None,
                    started,
                    None,
                ),
            )
            attempt_id = int(cur.lastrowid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return ClaimResult(CLAIM_CLAIMED, attempt)

    def claim_create_identity(
        self,
        *,
        identity: CreateIdentityParts,
        proposal_id: int,
        attempt_id: int,
    ) -> bool:
        """Reserve a create identity across proposals using structured overlap rules.

        Returns True if this proposal may proceed. The same proposal may continue
        while an older uncertain attempt still owns the reservation; ownership is
        not transferred onto the retry attempt. A different proposal cannot take
        an overlapping lock while it is held.
        """
        if not (
            identity.parent_id
            and identity.name_norm
            and identity.street_norm
            and identity.state_norm
        ):
            raise ValueError("create identity core fields are required")
        claimed_at = _now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, parent_id, name_norm, street_norm, state_norm,
                       city_norm, zip_norm, proposal_id, attempt_id
                FROM create_identity_locks
                WHERE parent_id = ? AND name_norm = ? AND street_norm = ? AND state_norm = ?
                """,
                (
                    identity.parent_id,
                    identity.name_norm,
                    identity.street_norm,
                    identity.state_norm,
                ),
            ).fetchall()
            for row in rows:
                held = CreateIdentityParts(
                    parent_id=str(row["parent_id"]),
                    name_norm=str(row["name_norm"]),
                    street_norm=str(row["street_norm"]),
                    state_norm=str(row["state_norm"]),
                    city_norm=str(row["city_norm"]) if row["city_norm"] else None,
                    zip_norm=str(row["zip_norm"]) if row["zip_norm"] else None,
                )
                if not identity.overlaps(held):
                    continue
                holder_proposal = int(row["proposal_id"])
                holder_attempt = int(row["attempt_id"])
                if holder_attempt == attempt_id or holder_proposal == proposal_id:
                    # Same proposal (or same attempt) may proceed; keep the original
                    # uncertain attempt as lock owner so a blocked retry cannot drop it.
                    conn.commit()
                    return True
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO create_identity_locks (
                    parent_id, name_norm, street_norm, state_norm, city_norm, zip_norm,
                    proposal_id, attempt_id, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.parent_id,
                    identity.name_norm,
                    identity.street_norm,
                    identity.state_norm,
                    identity.city_norm,
                    identity.zip_norm,
                    proposal_id,
                    attempt_id,
                    claimed_at,
                ),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_account_write(
        self,
        *,
        account_id: str,
        proposal_id: int,
        attempt_id: int,
    ) -> bool:
        """Reserve exclusive mutation of an existing CRM account across proposals.

        Same-proposal retries may continue without transferring ownership away from
        an older uncertain attempt. Different proposals fail closed.
        """
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("account_id is required for account write reservation")
        claimed_at = _now()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT proposal_id, attempt_id FROM account_write_locks WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is not None:
                holder_proposal = int(row["proposal_id"])
                holder_attempt = int(row["attempt_id"])
                if holder_attempt == attempt_id or holder_proposal == proposal_id:
                    conn.commit()
                    return True
                conn.commit()
                return False
            conn.execute(
                """
                INSERT INTO account_write_locks (
                    account_id, proposal_id, attempt_id, claimed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (account_id, proposal_id, attempt_id, claimed_at),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_create_identity_for_attempt(self, attempt_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM create_identity_locks WHERE attempt_id = ?",
                (attempt_id,),
            )
            conn.commit()

    def release_create_identity_for_proposal(self, proposal_id: int) -> None:
        """Drop any create-identity reservation held by attempts for this proposal."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM create_identity_locks WHERE proposal_id = ?",
                (proposal_id,),
            )
            conn.commit()

    def release_account_write_for_attempt(self, attempt_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM account_write_locks WHERE attempt_id = ?",
                (attempt_id,),
            )
            conn.commit()

    def release_account_write_for_proposal(self, proposal_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM account_write_locks WHERE proposal_id = ?",
                (proposal_id,),
            )
            conn.commit()

    def has_create_identity_lock_for_proposal(self, proposal_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM create_identity_locks WHERE proposal_id = ? LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return row is not None

    def has_account_write_lock_for_proposal(self, proposal_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM account_write_locks WHERE proposal_id = ? LIMIT 1",
                (proposal_id,),
            ).fetchone()
        return row is not None

    def create_identity_lock_attempt_id(self, proposal_id: int) -> int | None:
        """Attempt that currently owns the create-identity reservation, if any."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_id FROM create_identity_locks
                WHERE proposal_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return int(row["attempt_id"]) if row is not None else None

    def account_write_lock_attempt_id(self, account_id: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempt_id FROM account_write_locks WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return int(row["attempt_id"]) if row is not None else None

    def update_attempt(
        self,
        attempt_id: int,
        *,
        state: str,
        created_account_id: str | None = None,
        error_text: str | None = None,
    ) -> ApplicationAttempt:
        if state not in ATTEMPT_STATES:
            raise ValueError(f"invalid application state {state!r}")
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE application_attempts
                SET state = ?,
                    created_account_id = COALESCE(?, created_account_id),
                    error_text = COALESCE(?, error_text),
                    finished_at = ?
                WHERE id = ?
                """,
                (state, created_account_id, error_text, _now(), attempt_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"application attempt {attempt_id} not found")
            if state == STATE_APPLIED:
                owned = conn.execute(
                    "SELECT proposal_id FROM application_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
                if owned is not None:
                    proposal_id = int(owned["proposal_id"])
                    conn.execute(
                        "DELETE FROM create_identity_locks WHERE proposal_id = ?",
                        (proposal_id,),
                    )
                    conn.execute(
                        "DELETE FROM account_write_locks WHERE proposal_id = ?",
                        (proposal_id,),
                    )
            elif state in _WRITE_LOCK_RELEASE_STATES:
                conn.execute(
                    "DELETE FROM create_identity_locks WHERE attempt_id = ?",
                    (attempt_id,),
                )
                conn.execute(
                    "DELETE FROM account_write_locks WHERE attempt_id = ?",
                    (attempt_id,),
                )
            conn.commit()
        attempt = self.get_attempt(attempt_id)
        assert attempt is not None
        return attempt

    def get_attempt(self, attempt_id: int) -> ApplicationAttempt | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM application_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        return self._row_to_attempt(row) if row else None

    def list_attempts(self, proposal_id: int) -> list[ApplicationAttempt]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM application_attempts
                WHERE proposal_id = ?
                ORDER BY id
                """,
                (proposal_id,),
            ).fetchall()
        return [self._row_to_attempt(row) for row in rows]

    def successful_attempt(self, proposal_id: int) -> ApplicationAttempt | None:
        """An execute-mode attempt that finished applied. Dry-run plans do not count."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM application_attempts
                WHERE proposal_id = ? AND mode = ? AND state = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (proposal_id, MODE_EXECUTE, STATE_APPLIED),
            ).fetchone()
        return self._row_to_attempt(row) if row else None

    def created_account_id_for_proposal(self, proposal_id: int) -> str | None:
        """Newest execute-mode account id, including a failed attempt after a successful POST."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_account_id FROM application_attempts
                WHERE proposal_id = ?
                  AND mode = ?
                  AND created_account_id IS NOT NULL
                  AND created_account_id != ''
                ORDER BY id DESC
                LIMIT 1
                """,
                (proposal_id, MODE_EXECUTE),
            ).fetchone()
        if row is None:
            return None
        return str(row["created_account_id"])

    def has_uncertain_attempt(self, proposal_id: int) -> bool:
        """True when an execute-mode attempt recorded an uncertain write outcome."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM application_attempts
                WHERE proposal_id = ? AND mode = ? AND state = ?
                LIMIT 1
                """,
                (proposal_id, MODE_EXECUTE, STATE_UNCERTAIN),
            ).fetchone()
        return row is not None

    def action_types(self, *, run_id: int | None = None) -> list[str]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT action_type FROM proposals {where} ORDER BY action_type",
                params,
            ).fetchall()
        return [str(row["action_type"]) for row in rows]

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> ApplicationAttempt:
        created = row["created_account_id"]
        error = row["error_text"]
        finished = row["finished_at"]
        return ApplicationAttempt(
            id=int(row["id"]),
            proposal_id=int(row["proposal_id"]),
            run_id=int(row["run_id"]),
            action_type=str(row["action_type"]),
            mode=str(row["mode"]),
            state=str(row["state"]),
            created_account_id=str(created) if created else None,
            error_text=str(error) if error else None,
            started_at=str(row["started_at"]),
            finished_at=str(finished) if finished else None,
        )

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> StoredProposal:
        return StoredProposal(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            action_type=str(row["action_type"]),
            account_id=row["account_id"],
            facility_url=row["facility_url"],
            current_values=_loads(row["current_values_json"]),
            proposed_values=_loads(row["proposed_values_json"]),
            evidence=_loads(row["evidence_json"]),
            confidence=str(row["confidence"]),
            requires_review=bool(row["requires_review"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def default_db_path(data_dir: Path) -> Path:
    return Path(data_dir) / "bellhaven_sync.db"
