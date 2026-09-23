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

from .proposals import STATUS_VALUES, Proposal, ProposalBatch

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

ATTEMPT_MODES = frozenset({MODE_DRY_RUN, MODE_EXECUTE})
ATTEMPT_STATES = frozenset(
    {STATE_PLANNED, STATE_IN_PROGRESS, STATE_APPLIED, STATE_BLOCKED, STATE_FAILED}
)


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
            conn.executescript(SCHEMA_SQL)

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
