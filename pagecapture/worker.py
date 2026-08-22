from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .capture import BrowserEngine
from .config import Settings
from .db import Database
from .presets import merge_preset

log = logging.getLogger("page-scraper.worker")


class Worker:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.engine = BrowserEngine(settings)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()

    async def start(self) -> None:
        await self.engine.start()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="page-scraper-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.engine.stop()

    async def _run(self) -> None:
        slots = asyncio.Semaphore(max(1, self.settings.concurrency))
        while not self._stop.is_set():
            await slots.acquire()
            try:
                item = await self.db.claim_queued_item()
            except Exception:
                slots.release()
                log.exception("failed to claim work")
                await asyncio.sleep(1)
                continue
            if item is None:
                slots.release()
                await asyncio.sleep(0.4)
                continue
            task = asyncio.create_task(self._run_item(item, slots))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _run_item(self, item: dict, slots: asyncio.Semaphore) -> None:
        try:
            await self._process(item)
        except Exception:
            log.exception("worker crashed on %s", item.get("url"))
            try:
                await self.db.finish_item(
                    item["id"],
                    item["job_id"],
                    {
                        "status": "failed",
                        "reason": "crash",
                        "saved": 0,
                        "error": "internal worker error",
                    },
                )
            except Exception:
                log.exception("could not record crash for %s", item.get("id"))
            await self._recover()
        finally:
            slots.release()

    async def _process(self, item: dict) -> None:
        job = await self.db.get_job(item["job_id"])
        if job is None:
            return
        options = dict(job.get("options") or {})
        try:
            options = merge_preset(item["preset"], options)
        except ValueError:
            options["preset"] = item["preset"]
        output_root = Path(job["output_dir"])
        result = await self.engine.capture(
            url=item["url"],
            output_root=output_root,
            options=options,
        )
        await self.db.finish_item(item["id"], item["job_id"], result.item_fields())
        updated = await self.db.get_job(item["job_id"])
        if updated and updated["status"] in {"completed", "cancelled"}:
            await self._run_hook(updated)
        if self.engine.captures >= self.settings.recycle_every:
            log.info("recycling browser after %s captures", self.engine.captures)
            await self.engine.restart()

    async def _recover(self) -> None:
        log.warning("restarting browser after failure")
        try:
            await asyncio.wait_for(self.engine.restart(), timeout=20)
        except Exception:
            log.exception("browser restart hung; exiting so Docker can respawn")
            os._exit(1)

    async def _run_hook(self, job: dict) -> None:
        command = self.settings.on_complete_hook.strip()
        if not command:
            return
        env = os.environ.copy()
        env.update(
            {
                "JOB_ID": job["id"],
                "JOB_STATUS": job["status"],
                "JOB_OUTPUT_DIR": job["output_dir"],
                "JOB_FAILED": str(job["failed"] or 0),
                "JOB_TOTAL": str(job["total"] or 0),
            }
        )
        try:
            proc = await asyncio.create_subprocess_shell(command, env=env)
            await asyncio.wait_for(proc.wait(), timeout=60)
        except Exception:
            log.exception("on-complete hook failed for job %s", job["id"])
