"""State store — SQLite-backed persistence for Ghost Ops."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Generator

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id          TEXT PRIMARY KEY,
    schedule    TEXT NOT NULL,
    last_run    DATETIME,
    next_run    DATETIME,
    status      TEXT DEFAULT 'idle',
    run_count   INTEGER DEFAULT 0,
    fail_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  TEXT NOT NULL,
    started_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME,
    status      TEXT DEFAULT 'running',
    model_used  TEXT,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    results     TEXT,
    error       TEXT,
    mission     TEXT GENERATED ALWAYS AS (mission_id) VIRTUAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    severity     TEXT NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT,
    repo         TEXT,
    acknowledged BOOLEAN DEFAULT FALSE,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watched_repos (
    repo             TEXT PRIMARY KEY,
    scan_frequency   TEXT DEFAULT 'daily',
    last_scanned     DATETIME,
    security_score   REAL,
    compliance_score REAL,
    activity_score   REAL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS mutations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL,
    original_hash TEXT,
    mutated_hash  TEXT,
    mutation_type TEXT,
    validator_1   TEXT,
    validator_2   TEXT,
    validator_3   TEXT,
    consensus     TEXT,
    deployed      BOOLEAN DEFAULT FALSE,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS elo_cache (
    model          TEXT PRIMARY KEY,
    elo            REAL,
    wins           INTEGER,
    losses         INTEGER,
    best_task_types TEXT,
    updated_at     DATETIME
);
"""


class StateStore:
    """SQLite state store with async helpers via asyncio.to_thread."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path if db_path == ":memory:" else os.path.expanduser(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open (or create) the database and apply schema."""
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # Idempotent migration: add A/B testing columns to mutations table
        for _col_sql in [
            "ALTER TABLE mutations ADD COLUMN ab_score_original REAL",
            "ALTER TABLE mutations ADD COLUMN ab_score_mutated REAL",
            "ALTER TABLE mutations ADD COLUMN ab_task TEXT",
            "ALTER TABLE mutations ADD COLUMN ab_winner TEXT",
        ]:
            try:
                self._conn.execute(_col_sql)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()
        logger.debug("StateStore opened: %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("StateStore is not open")
        return self._conn

    # ------------------------------------------------------------------
    # Sync context manager for transactions
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Synchronous context manager that commits on success, rolls back on error."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Async wrappers (run SQLite in thread pool to avoid blocking the loop)
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        def _run() -> list[dict[str, Any]]:
            with self._lock:
                cur = self.conn.execute(sql, params)
                # Consume cursor BEFORE commit — required for RETURNING clauses
                rows = [dict(row) for row in cur.fetchall()] if cur.description else []
                self.conn.commit()
                return rows
        return await asyncio.to_thread(_run)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        def _run() -> None:
            with self._lock:
                self.conn.executemany(sql, params_seq)
                self.conn.commit()
        await asyncio.to_thread(_run)

    # ------------------------------------------------------------------
    # Mission helpers
    # ------------------------------------------------------------------

    async def upsert_mission(self, mission_id: str, schedule: str) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO missions (id, schedule) VALUES (?, ?)",
            (mission_id, schedule),
        )

    async def record_run_start(self, mission_id: str) -> int:
        rows = await self.execute(
            "INSERT INTO runs (mission_id, status) VALUES (?, 'running') RETURNING id",
            (mission_id,),
        )
        run_id: int = rows[0]["id"]
        await self.execute(
            "UPDATE missions SET status='running', last_run=CURRENT_TIMESTAMP WHERE id=?",
            (mission_id,),
        )
        return run_id

    async def record_run_finish(
        self,
        run_id: int,
        mission_id: str,
        status: str,
        *,
        model_used: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        results: Any = None,
        error: str | None = None,
    ) -> None:
        results_json = json.dumps(results) if results is not None else None
        await self.execute(
            """UPDATE runs
               SET finished_at=CURRENT_TIMESTAMP, status=?, model_used=?,
                   tokens_in=?, tokens_out=?, results=?, error=?
               WHERE id=?""",
            (status, model_used, tokens_in, tokens_out, results_json, error, run_id),
        )
        col = "fail_count" if status == "failed" else "run_count"
        await self.execute(
            f"UPDATE missions SET status=?, {col}={col}+1 WHERE id=?",  # noqa: S608
            ("idle", mission_id),
        )

    # ------------------------------------------------------------------
    # Alert helpers
    # ------------------------------------------------------------------

    async def write_alert(
        self,
        severity: str,
        source: str,
        title: str,
        detail: str | None = None,
        repo: str | None = None,
    ) -> int:
        rows = await self.execute(
            "INSERT INTO alerts (severity, source, title, detail, repo) VALUES (?,?,?,?,?) RETURNING id",
            (severity, source, title, detail, repo),
        )
        return rows[0]["id"]

    # ------------------------------------------------------------------
    # Watched repos helpers
    # ------------------------------------------------------------------

    async def get_watched_repos(self) -> list[dict[str, Any]]:
        return await self.execute("SELECT * FROM watched_repos")

    async def upsert_watched_repo(self, repo: str, **kwargs: Any) -> None:
        if kwargs:
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            updates = ", ".join(f"{k}=excluded.{k}" for k in kwargs)
            values = tuple(kwargs.values())
            await self.execute(
                f"INSERT INTO watched_repos (repo, {cols}) VALUES (?, {placeholders}) "  # noqa: S608
                f"ON CONFLICT(repo) DO UPDATE SET {updates}",
                (repo, *values),
            )
        else:
            await self.execute(
                "INSERT OR IGNORE INTO watched_repos (repo) VALUES (?)",
                (repo,),
            )

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    async def record_mutation(
        self,
        agent_id: str,
        original_hash: str,
        mutated_hash: str,
        mutation_type: str,
        validators: list[str],
        consensus: str,
        deployed: bool = False,
        ab_score_original: float | None = None,
        ab_score_mutated: float | None = None,
        ab_task: str | None = None,
        ab_winner: str | None = None,
    ) -> int:
        v1 = validators[0] if len(validators) > 0 else None
        v2 = validators[1] if len(validators) > 1 else None
        v3 = validators[2] if len(validators) > 2 else None
        rows = await self.execute(
            """INSERT INTO mutations
               (agent_id, original_hash, mutated_hash, mutation_type,
                validator_1, validator_2, validator_3, consensus, deployed,
                ab_score_original, ab_score_mutated, ab_task, ab_winner)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
            (agent_id, original_hash, mutated_hash, mutation_type, v1, v2, v3, consensus, deployed,
             ab_score_original, ab_score_mutated, ab_task, ab_winner),
        )
        return rows[0]["id"]

    # ------------------------------------------------------------------
    # ELO cache helpers
    # ------------------------------------------------------------------

    async def upsert_elo_cache(self, model: str, elo: float, wins: int, losses: int, best_task_types: str = "") -> None:
        await self.execute(
            """INSERT INTO elo_cache (model, elo, wins, losses, best_task_types, updated_at)
               VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model) DO UPDATE SET
                   elo=excluded.elo, wins=excluded.wins, losses=excluded.losses,
                   best_task_types=excluded.best_task_types, updated_at=excluded.updated_at""",
            (model, elo, wins, losses, best_task_types),
        )
