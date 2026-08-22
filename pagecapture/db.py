from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    options_json TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    partial INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    avg_ms INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    url TEXT NOT NULL,
    preset TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    saved INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    output_path TEXT,
            screenshot_path TEXT,
            dom_path TEXT,
            video_path TEXT,
    http_status INTEGER,
    height_px INTEGER,
    cookie_dismissed INTEGER NOT NULL DEFAULT 0,
    height_capped INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_items_job ON items(job_id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "options_json" in data and data["options_json"]:
        data["options"] = json.loads(data["options_json"])
        del data["options_json"]
    return data


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()
        await self.reset_running()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn

    async def _migrate(self) -> None:
        cur = await self.conn.execute("PRAGMA table_info(items)")
        cols = {row[1] for row in await cur.fetchall()}
        if "video_path" not in cols:
            await self.conn.execute("ALTER TABLE items ADD COLUMN video_path TEXT")
        await self.conn.execute(
            """
            UPDATE items
            SET status='complete'
            WHERE status='failed' AND reason='unstable' AND saved=1
            """
        )
        await self.conn.execute(
            """
            UPDATE jobs
            SET
              failed = (SELECT COUNT(*) FROM items WHERE items.job_id = jobs.id AND status='failed'),
              partial = (SELECT COUNT(*) FROM items WHERE items.job_id = jobs.id AND status='failed' AND saved=1),
              done = (SELECT COUNT(*) FROM items WHERE items.job_id = jobs.id AND status IN ('complete', 'failed'))
            """
        )

    async def reset_running(self) -> None:
        now = _now()
        await self.conn.execute(
            "UPDATE items SET status='queued', started_at=NULL WHERE status='running'"
        )
        await self.conn.execute(
            """
            UPDATE jobs
            SET status='queued', updated_at=?
            WHERE status='running'
            """,
            (now,),
        )
        await self.conn.commit()

    async def create_job(
        self,
        *,
        name: str | None,
        output_dir: str,
        options: dict[str, Any],
        items: list[tuple[str, str]],
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _now()
        await self.conn.execute(
            """
            INSERT INTO jobs (id, created_at, updated_at, name, status, output_dir, options_json, total)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, now, now, name, output_dir, json.dumps(options), len(items)),
        )
        await self.conn.executemany(
            """
            INSERT INTO items (id, job_id, url, preset, status)
            VALUES (?, ?, ?, ?, 'queued')
            """,
            [(str(uuid.uuid4()), job_id, url, preset) for url, preset in items],
        )
        await self.conn.commit()
        job = await self.get_job(job_id)
        assert job is not None
        return job

    async def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            cur = await self.conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            )
        return [_row(r) for r in await cur.fetchall()]  # type: ignore[misc]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _row(await cur.fetchone())

    async def get_item(self, job_id: str, item_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT * FROM items WHERE id=? AND job_id=?",
            (item_id, job_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_items(self, job_id: str) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM items WHERE job_id=? ORDER BY rowid",
            (job_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def claim_queued_item(self) -> dict[str, Any] | None:
        now = _now()
        cur = await self.conn.execute(
            """
            UPDATE items
            SET status='running', started_at=?
            WHERE id = (
                SELECT id FROM (
                    SELECT items.id AS id FROM items
                    JOIN jobs ON jobs.id = items.job_id
                    WHERE items.status='queued' AND jobs.status IN ('queued', 'running')
                    ORDER BY items.rowid
                    LIMIT 1
                )
            )
            RETURNING *
            """,
            (now,),
        )
        row = await cur.fetchone()
        if row is None:
            await self.conn.commit()
            return None
        item = dict(row)
        await self.conn.execute(
            """
            UPDATE jobs
            SET status='running', updated_at=?, started_at=COALESCE(started_at, ?)
            WHERE id=?
            """,
            (now, now, item["job_id"]),
        )
        await self.conn.commit()
        return item

    async def finish_item(self, item_id: str, job_id: str, fields: dict[str, Any]) -> None:
        now = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        cur = await self.conn.execute(
            f"UPDATE items SET {cols}, finished_at=? WHERE id=?",
            (*fields.values(), now, item_id),
        )
        if cur.rowcount == 0:
            await self.conn.commit()
            return
        cur = await self.conn.execute(
            """
            SELECT
                SUM(status IN ('complete', 'failed')) AS done,
                SUM(status='failed' AND saved=1) AS partial,
                SUM(status='failed') AS failed,
                AVG(CASE WHEN duration_ms IS NOT NULL THEN duration_ms END) AS avg_ms,
                SUM(status='queued') AS queued,
                SUM(status='running') AS running
            FROM items WHERE job_id=?
            """,
            (job_id,),
        )
        stats = dict(await cur.fetchone())
        remaining = (stats["queued"] or 0) + (stats["running"] or 0)
        job_status = "running" if remaining else "completed"
        finished_at = None if remaining else now
        await self.conn.execute(
            """
            UPDATE jobs
            SET done=?, failed=?, partial=?, avg_ms=?, status=?, updated_at=?, finished_at=COALESCE(?, finished_at)
            WHERE id=?
            """,
            (
                stats["done"] or 0,
                stats["failed"] or 0,
                stats["partial"] or 0,
                int(stats["avg_ms"] or 0) or None,
                job_status,
                now,
                finished_at,
                job_id,
            ),
        )
        await self.conn.commit()

    async def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        now = _now()
        await self.conn.execute(
            "UPDATE items SET status='failed', reason='cancelled', finished_at=? WHERE job_id=? AND status='queued'",
            (now, job_id),
        )
        job = await self.get_job(job_id)
        if not job:
            return None
        if job["status"] in {"queued", "running"}:
            cur = await self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE job_id=? AND status='running'",
                (job_id,),
            )
            running = (await cur.fetchone())[0]
            status = "running" if running else "cancelled"
            finished = None if running else now
            await self.conn.execute(
                "UPDATE jobs SET status=?, updated_at=?, finished_at=COALESCE(?, finished_at) WHERE id=?",
                (status, now, finished, job_id),
            )
            await self.conn.commit()
        return await self.get_job(job_id)

    async def delete_jobs(self, job_ids: list[str]) -> list[dict[str, Any]]:
        deleted: list[dict[str, Any]] = []
        for job_id in job_ids:
            job = await self.get_job(job_id)
            if not job:
                continue
            items = await self.list_items(job_id)
            await self.conn.execute("DELETE FROM items WHERE job_id=?", (job_id,))
            await self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            job["items"] = items
            deleted.append(job)
        await self.conn.commit()
        return deleted

    async def clone_jobs(self, job_ids: list[str]) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for job_id in job_ids:
            job = await self.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "not found"})
                continue
            items = await self.list_items(job_id)
            pairs = [(item["url"], item["preset"]) for item in items]
            if not pairs:
                skipped.append({"id": job_id, "reason": "no items"})
                continue
            clone = await self.create_job(
                name=job.get("name"),
                output_dir=job["output_dir"],
                options=job.get("options") or {},
                items=pairs,
            )
            created.append(clone)
        return {"created": created, "skipped": skipped}

    async def retry_unsaved(self, job_ids: list[str]) -> dict[str, Any]:
        now = _now()
        retried: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for job_id in job_ids:
            job = await self.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "not found"})
                continue
            cur = await self.conn.execute(
                """
                UPDATE items
                SET status='queued',
                    reason=NULL,
                    saved=0,
                    error=NULL,
                    screenshot_path=NULL,
                    dom_path=NULL,
                    video_path=NULL,
                    output_path=NULL,
                    http_status=NULL,
                    height_px=NULL,
                    cookie_dismissed=0,
                    height_capped=0,
                    started_at=NULL,
                    finished_at=NULL,
                    duration_ms=NULL
                WHERE job_id=?
                  AND IFNULL(saved, 0)=0
                  AND status NOT IN ('queued', 'running')
                """,
                (job_id,),
            )
            n = cur.rowcount or 0
            if n <= 0:
                skipped.append({"id": job_id, "reason": "nothing to retry"})
                continue
            cur = await self.conn.execute(
                """
                SELECT
                    SUM(status IN ('complete', 'failed')) AS done,
                    SUM(status='failed' AND saved=1) AS partial,
                    SUM(status='failed') AS failed,
                    SUM(status='queued') AS queued,
                    SUM(status='running') AS running
                FROM items WHERE job_id=?
                """,
                (job_id,),
            )
            stats = dict(await cur.fetchone())
            remaining = (stats["queued"] or 0) + (stats["running"] or 0)
            if (stats["running"] or 0) > 0:
                job_status = "running"
            elif remaining:
                job_status = "queued"
            else:
                job_status = "completed"
            await self.conn.execute(
                """
                UPDATE jobs
                SET done=?, failed=?, partial=?, status=?, updated_at=?,
                    finished_at=CASE WHEN ?='completed' THEN finished_at ELSE NULL END
                WHERE id=?
                """,
                (
                    stats["done"] or 0,
                    stats["failed"] or 0,
                    stats["partial"] or 0,
                    job_status,
                    now,
                    job_status,
                    job_id,
                ),
            )
            retried.append({"id": job_id, "items": n})
        await self.conn.commit()
        return {"retried": retried, "skipped": skipped}

    async def job_counts(self) -> dict[str, int]:
        cur = await self.conn.execute(
            """
            SELECT
                SUM(status='queued') AS queued,
                SUM(status='running') AS running,
                SUM(status='completed') AS completed,
                SUM(status='cancelled') AS cancelled
            FROM jobs
            """
        )
        row = dict(await cur.fetchone())
        return {k: int(v or 0) for k, v in row.items()}
