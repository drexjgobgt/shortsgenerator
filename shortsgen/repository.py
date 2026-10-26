"""SQLite job repository.

Fitur:
- Job + state machine stage (pending/running/done/failed/blocked/skipped)
- Resume: `next_pending_stage()` mengembalikan stage pertama yang belum selesai
- Pencegahan duplikasi pekerjaan: `dedupe_key` unik untuk job aktif
- Jejak audit: events + usage per job
- WAL + busy timeout agar aman untuk beberapa proses
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from .errors import DedupeConflict, NotFoundError
from .models import (
    STAGE_ORDER,
    EventRecord,
    Job,
    JobStatus,
    StageRecord,
    StageStatus,
    UsageRecord,
    utcnow_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    dedupe_key      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    config_snapshot TEXT NOT NULL DEFAULT '{}',
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key, status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS job_stages (
    job_id      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    artifacts   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (job_id, stage),
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    units         TEXT NOT NULL DEFAULT '{}',
    cost_estimate REAL NOT NULL DEFAULT 0,
    ts            TEXT NOT NULL,
    meta          TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage(job_id);

CREATE TABLE IF NOT EXISTS uploads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    video_id       TEXT NOT NULL,
    privacy_status TEXT NOT NULL,
    uploaded_at    TEXT NOT NULL,
    meta           TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_fingerprint ON uploads(fingerprint);
CREATE INDEX IF NOT EXISTS idx_uploads_job ON uploads(job_id);
"""


def normalize_topic(topic: str) -> str:
    """Normalisasi topik untuk fingerprint duplikasi."""
    return re.sub(r"\s+", " ", (topic or "").strip().lower())


def compute_dedupe_key(topic: str, mode: str) -> str:
    payload = f"{normalize_topic(topic)}|{mode}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class JobRepository:
    """Akses data job berbasis SQLite."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.initialize()

    # -- lifecycle ----------------------------------------------------

    def initialize(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobRepository":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- jobs ---------------------------------------------------------

    def create_job(
        self,
        topic: str,
        mode: str,
        config_snapshot: Optional[dict[str, Any]] = None,
        *,
        force: bool = False,
    ) -> Job:
        """Buat job baru; tolak duplikat topik kecuali `force=True`."""
        dedupe_key = compute_dedupe_key(topic, mode)
        if not force:
            existing = self._find_active_by_dedupe(dedupe_key)
            if existing is not None:
                raise DedupeConflict(existing["id"], topic)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = utcnow_iso()
        with self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, topic, mode, status, dedupe_key, created_at, updated_at, config_snapshot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    topic.strip(),
                    mode,
                    JobStatus.CREATED,
                    dedupe_key,
                    now,
                    now,
                    json.dumps(config_snapshot or {}, ensure_ascii=False),
                ),
            )
        self.add_event(job_id, "info", f"Job dibuat untuk topik: {topic.strip()}")
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        """Hapus job + seluruh data turunannya (stage, event, usage, upload).

        Media di disk tidak dihapus di sini; pemanggil (CLI/skrip) yang
        bertanggung jawab menghapus direktori job agar tidak ada data yatim.
        """
        self.get_job(job_id)  # NotFoundError bila tidak ada
        with self._conn:
            cursor = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    def _find_active_by_dedupe(self, dedupe_key: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ? AND status != ? ORDER BY created_at DESC LIMIT 1",
            (dedupe_key, JobStatus.FAILED),
        ).fetchone()

    def get_job(self, job_id: str) -> Job:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Job tidak ditemukan: {job_id}")
        return self._row_to_job(row)

    def get_job_optional(self, job_id: str) -> Optional[Job]:
        try:
            return self.get_job(job_id)
        except NotFoundError:
            return None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def set_job_status(self, job_id: str, status: str, *, error: Optional[str] = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                (status, utcnow_iso(), error, job_id),
            )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            topic=row["topic"],
            mode=row["mode"],
            status=row["status"],
            dedupe_key=row["dedupe_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            config_snapshot=json.loads(row["config_snapshot"] or "{}"),
            last_error=row["last_error"],
        )

    # -- stages -------------------------------------------------------

    def start_stage(self, job_id: str, stage: str) -> StageRecord:
        """Tandai stage berjalan; naikkan attempt bila stage dijalankan ulang."""
        if stage not in STAGE_ORDER:
            raise NotFoundError(f"Stage tidak dikenal: {stage}")
        now = utcnow_iso()
        current = self.get_stage(job_id, stage)
        attempt = (current.attempt + 1) if current else 1
        with self._conn:
            self._conn.execute(
                "INSERT INTO job_stages (job_id, stage, status, attempt, started_at, finished_at, error, artifacts)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL, '{}')"
                " ON CONFLICT(job_id, stage) DO UPDATE SET"
                "   status = excluded.status, attempt = excluded.attempt,"
                "   started_at = excluded.started_at, finished_at = NULL, error = NULL",
                (job_id, stage, StageStatus.RUNNING, attempt, now),
            )
        self.add_event(job_id, "info", f"Stage dimulai: {stage} (attempt {attempt})")
        return self.get_stage(job_id, stage)

    def finish_stage(
        self,
        job_id: str,
        stage: str,
        status: str,
        *,
        artifacts: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> StageRecord:
        if status not in (
            StageStatus.DONE,
            StageStatus.FAILED,
            StageStatus.BLOCKED,
            StageStatus.SKIPPED,
        ):
            raise ValueError(f"Status stage tidak valid untuk finish_stage: {status}")
        now = utcnow_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE job_stages SET status = ?, finished_at = ?, error = ?, artifacts = ?"
                " WHERE job_id = ? AND stage = ?",
                (
                    status,
                    now,
                    error,
                    json.dumps(artifacts or {}, ensure_ascii=False),
                    job_id,
                    stage,
                ),
            )
        level = "info" if status in (StageStatus.DONE, StageStatus.SKIPPED) else "error"
        message = f"Stage {status}: {stage}" + (f" - {error}" if error else "")
        self.add_event(job_id, level, message, {"artifacts": sorted((artifacts or {}).keys())})
        return self.get_stage(job_id, stage)

    def get_stage(self, job_id: str, stage: str) -> Optional[StageRecord]:
        row = self._conn.execute(
            "SELECT * FROM job_stages WHERE job_id = ? AND stage = ?", (job_id, stage)
        ).fetchone()
        return self._row_to_stage(row) if row else None

    def get_stages(self, job_id: str) -> dict[str, StageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM job_stages WHERE job_id = ?", (job_id,)
        ).fetchall()
        return {row["stage"]: self._row_to_stage(row) for row in rows}

    def next_pending_stage(self, job_id: str) -> Optional[str]:
        """Stage pertama (urut STAGE_ORDER) yang belum done/skipped, untuk resume."""
        stages = self.get_stages(job_id)
        for stage in STAGE_ORDER:
            record = stages.get(stage)
            if record is None or record.status in (StageStatus.PENDING, StageStatus.FAILED):
                return stage
        return None

    def reset_stages(self, job_id: str, from_stage: str) -> list[str]:
        """Hapus catatan stage mulai `from_stage` agar bisa dijalankan ulang.

        Aset di disk TIDAK dihapus; stage aset dapat memakai ulang berkas yang ada.
        """
        order = list(STAGE_ORDER)
        if from_stage not in order:
            raise NotFoundError(f"Stage tidak dikenal: {from_stage}")
        stages = order[order.index(from_stage) :]
        placeholders = ",".join("?" for _ in stages)
        with self._conn:
            self._conn.execute(
                f"DELETE FROM job_stages WHERE job_id = ? AND stage IN ({placeholders})",
                (job_id, *stages),
            )
        self.set_job_status(job_id, JobStatus.RUNNING)
        self.add_event(job_id, "info", f"Stage direset dari '{from_stage}': {', '.join(stages)}")
        return stages

    @staticmethod
    def _row_to_stage(row: sqlite3.Row) -> StageRecord:
        return StageRecord(
            job_id=row["job_id"],
            stage=row["stage"],
            status=row["status"],
            attempt=row["attempt"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
            artifacts=json.loads(row["artifacts"] or "{}"),
        )

    # -- events -------------------------------------------------------

    def add_event(
        self,
        job_id: Optional[str],
        level: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (job_id, ts, level, message, data) VALUES (?, ?, ?, ?, ?)",
                (job_id, utcnow_iso(), level, message, json.dumps(data or {}, ensure_ascii=False)),
            )

    def get_events(self, job_id: str, limit: int = 200) -> list[EventRecord]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id ASC LIMIT ?", (job_id, limit)
        ).fetchall()
        return [
            EventRecord(
                id=r["id"],
                job_id=r["job_id"],
                ts=r["ts"],
                level=r["level"],
                message=r["message"],
                data=json.loads(r["data"] or "{}"),
            )
            for r in rows
        ]

    # -- usage --------------------------------------------------------

    def record_usage(
        self,
        job_id: str,
        provider: str,
        model: str,
        kind: str,
        *,
        units: Optional[dict[str, float]] = None,
        cost_estimate: float = 0.0,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO usage (job_id, provider, model, kind, units, cost_estimate, ts, meta)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    provider,
                    model,
                    kind,
                    json.dumps(units or {}, ensure_ascii=False),
                    float(cost_estimate),
                    utcnow_iso(),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )

    def get_usage(self, job_id: str) -> list[UsageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM usage WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [
            UsageRecord(
                id=r["id"],
                job_id=r["job_id"],
                provider=r["provider"],
                model=r["model"],
                kind=r["kind"],
                units=json.loads(r["units"] or "{}"),
                cost_estimate=r["cost_estimate"],
                ts=r["ts"],
                meta=json.loads(r["meta"] or "{}"),
            )
            for r in rows
        ]

    def usage_summary(self, job_id: str) -> dict[str, Any]:
        """Ringkasan pemakaian per provider+kind untuk laporan job."""
        summary: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        for rec in self.get_usage(job_id):
            key = f"{rec.provider}:{rec.kind}"
            bucket = summary.setdefault(
                key, {"calls": 0, "units": {}, "cost_estimate": 0.0, "models": set()}
            )
            bucket["calls"] += 1
            bucket["cost_estimate"] += rec.cost_estimate
            bucket["models"].add(rec.model)
            for unit, value in rec.units.items():
                bucket["units"][unit] = bucket["units"].get(unit, 0) + value
            total_cost += rec.cost_estimate
        for bucket in summary.values():
            bucket["models"] = sorted(bucket["models"])
            bucket["cost_estimate"] = round(bucket["cost_estimate"], 6)
        return {"by_provider": summary, "total_cost_estimate": round(total_cost, 6)}

    # -- uploads ------------------------------------------------------

    def find_upload(self, fingerprint: str) -> Optional[dict[str, Any]]:
        """Cari upload sukses berdasarkan fingerprint video (anti-duplikat)."""
        row = self._conn.execute(
            "SELECT * FROM uploads WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "fingerprint": row["fingerprint"],
            "video_id": row["video_id"],
            "privacy_status": row["privacy_status"],
            "uploaded_at": row["uploaded_at"],
            "meta": json.loads(row["meta"] or "{}"),
        }

    def record_upload(
        self,
        job_id: str,
        fingerprint: str,
        video_id: str,
        privacy_status: str,
        *,
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO uploads (job_id, fingerprint, video_id, privacy_status, uploaded_at, meta)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    fingerprint,
                    video_id,
                    privacy_status,
                    utcnow_iso(),
                    json.dumps(meta or {}, ensure_ascii=False),
                ),
            )

    def get_uploads(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM uploads WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "fingerprint": r["fingerprint"],
                "video_id": r["video_id"],
                "privacy_status": r["privacy_status"],
                "uploaded_at": r["uploaded_at"],
                "meta": json.loads(r["meta"] or "{}"),
            }
            for r in rows
        ]

    # -- utilities ----------------------------------------------------

    def event_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def job_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def jobs_by_status(self, statuses: Iterable[str]) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at ASC",
            tuple(statuses),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]
