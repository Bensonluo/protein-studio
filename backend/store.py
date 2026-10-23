"""Transactional SQLite task storage.

SQLite is the authority for task ownership and cancellation. A queue claim and a
terminal transition use transactions so a cancellation cannot be lost between
reading a task and publishing its result. One server process should own a data
directory; server.py enforces that with a process lock.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

TERMINAL = frozenset({"completed", "failed", "cancelled"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ConflictError(ValueError):
    """The requested transition is invalid for the task's current state."""


class JobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            try:
                conn.execute("SELECT json_remove('{}', '$.result.pae')").fetchone()
            except sqlite3.OperationalError as exc:
                raise RuntimeError("Protein Studio requires SQLite JSON functions (JSON1) for bounded-memory task lists.") from exc
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    priority INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, priority, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_job ON events(job_id, sequence);
            """)

    @contextmanager
    def _connection(self, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _read(conn, job_id):
        row = conn.execute("SELECT document FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row["document"]) if row else None

    @staticmethod
    def _save(conn, job):
        conn.execute("UPDATE jobs SET status=?, updated_at=?, document=? WHERE id=?", (
            job["status"], job["updatedAt"], json.dumps(job, ensure_ascii=False, allow_nan=False), job["id"]))

    @staticmethod
    def _event(conn, job_id, stage, message, progress=None, **extra):
        event = {"time": utcnow(), "stage": str(stage), "message": str(message)}
        if progress is not None:
            event["progress"] = progress
        event.update({k: v for k, v in extra.items() if k not in event})
        conn.execute("INSERT INTO events(job_id,document) VALUES (?,?)", (
            job_id, json.dumps(event, ensure_ascii=False, allow_nan=False)))

    def _insert(self, conn, submission, parent_id=None, initial_status="queued"):
        if initial_status not in {"queued", "running"}:
            raise ValueError("Invalid initial status")
        now = utcnow()
        job = copy.deepcopy(submission)
        job.update({"id": uuid.uuid4().hex, "name": submission.get("name") or "Untitled prediction",
                    "engine": submission.get("engine", "reference"), "status": initial_status,
                    "stage": "queued" if initial_status == "queued" else "importing", "progress": 0,
                    "createdAt": now, "updatedAt": now, "cancelRequested": False, "result": None,
                    "error": None, "_submission": copy.deepcopy(submission)})
        if parent_id:
            job["retryOf"] = parent_id
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", (
            job["id"], initial_status, 0 if submission.get("priority") == "high" else 1,
            now, now, json.dumps(job, ensure_ascii=False, allow_nan=False)))
        self._event(conn, job["id"], job["stage"],
                    "Task accepted and queued." if initial_status == "queued" else "Import accepted.", 0)
        return job

    def create(self, submission: dict, parent_id: str | None = None, initial_status="queued"):
        with self._connection(write=True) as conn:
            job = self._insert(conn, submission, parent_id, initial_status)
        return self.get(job["id"])

    def create_many(self, submissions):
        """Persist an already validated batch atomically, preserving response order."""
        with self._connection(write=True) as conn:
            jobs = [self._insert(conn, submission) for submission in submissions]
        return jobs

    def get(self, job_id):
        with self._connection() as conn:
            job = self._read(conn, job_id)
            if job is not None:
                job["events"] = [json.loads(r["document"]) for r in conn.execute(
                    "SELECT document FROM events WHERE job_id=? ORDER BY sequence", (job_id,))]
            return job

    def list(self, limit=200):
        with self._connection() as conn:
            # Remove large scientific arrays before materializing any documents
            # in Python. Detail reads intentionally retain the full result.
            rows = conn.execute("""
                SELECT json_remove(document,
                    '$.result.pae', '$.result.residues', '$.result.plddt',
                    '$.result.rankedModels') AS document
                FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?
            """, (limit,))
            return [json.loads(r["document"]) for r in rows]

    def claim(self, allowed_engines=None):
        with self._connection(write=True) as conn:
            where, parameters = "status='queued'", ()
            if allowed_engines is not None:
                if not allowed_engines:
                    return None
                where += " AND json_extract(document,'$.engine') IN (" + ','.join('?' for _ in allowed_engines) + ')'
                parameters = tuple(allowed_engines)
            row = conn.execute('SELECT id FROM jobs WHERE ' + where + ' ORDER BY priority, created_at, id LIMIT 1', parameters).fetchone()
            if row is None:
                return None
            job = self._read(conn, row["id"])
            job.update(status="running", stage="starting", progress=1, updatedAt=utcnow(), startedAt=utcnow())
            self._save(conn, job)
            self._event(conn, job["id"], "starting", "Worker claimed task.", 1)
            return job

    def emit(self, job_id, stage, message, progress=None, **extra):
        if progress is not None:
            progress = max(0.0, min(99.0, float(progress)))
        with self._connection(write=True) as conn:
            job = self._read(conn, job_id)
            if job is None or job["status"] != "running":
                return
            job.update(stage=str(stage), updatedAt=utcnow())
            if progress is not None:
                job["progress"] = max(job.get("progress", 0), progress)
            self._save(conn, job)
            self._event(conn, job_id, stage, message, progress, **extra)

    def cancel(self, job_id):
        with self._connection(write=True) as conn:
            job = self._read(conn, job_id)
            if job is None:
                return None
            if job["status"] in TERMINAL:
                return job
            if job.get("cancelRequested"):
                return job
            job.update(cancelRequested=True, updatedAt=utcnow())
            if job["status"] == "queued":
                job.update(status="cancelled", stage="cancelled", finishedAt=utcnow())
                message = "Queued task cancelled."
            else:
                message = "Cancellation requested; waiting for the engine to stop safely."
            self._save(conn, job)
            self._event(conn, job_id, job["stage"], message, job["progress"])
        return self.get(job_id)

    def cancelled(self, job_id):
        with self._connection() as conn:
            job = self._read(conn, job_id)
            return job is None or job.get("cancelRequested", False) or job["status"] == "cancelled"

    def finish(self, job_id, status, result=None, error=None, artifacts=None):
        if status not in TERMINAL:
            raise ValueError("Invalid terminal status")
        with self._connection(write=True) as conn:
            job = self._read(conn, job_id)
            if job is None or job["status"] != "running":
                return False
            # Cancellation and result publication are serialized in this transaction.
            if job.get("cancelRequested"):
                status, result, artifacts, error = "cancelled", None, None, None
            job.update(status=status, stage=status, updatedAt=utcnow(), finishedAt=utcnow(),
                       result=result, error=error, _artifacts=artifacts or {})
            if status == "completed":
                job["progress"] = 100
                if not job.get("chains") and isinstance(result, dict) and result.get("chains"):
                    job["chains"] = copy.deepcopy(result["chains"])
            self._save(conn, job)
            message = {"completed": "Task completed and artifacts committed.",
                       "cancelled": "Task cancelled.", "failed": str(error or "Engine failed.")}[status]
            self._event(conn, job_id, status, message, job["progress"])
            return True

    def recover_interrupted(self):
        with self._connection(write=True) as conn:
            rows = conn.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                job = self._read(conn, row["id"])
                cancelled = job.get("cancelRequested", False)
                status = "cancelled" if cancelled else "failed"
                error = None if cancelled else "Server stopped while this task was running. Retry starts a new attempt."
                job.update(status=status, stage=status, error=error, updatedAt=utcnow(), finishedAt=utcnow())
                self._save(conn, job)
                self._event(conn, job["id"], status, error or "Cancelled task recovered after restart.", job["progress"])
            return len(rows)

    def delete(self, job_id):
        with self._connection(write=True) as conn:
            job = self._read(conn, job_id)
            if job is None:
                return False
            if job["status"] == "running":
                raise ConflictError("Cancel this task and wait for the worker to stop before deleting it.")
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return True
