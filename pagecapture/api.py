from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .db import Database
from .paths import parse_http_url, resolve_output_dir, resolve_output_file, unlink_capture_files
from .presets import known_presets
from .worker import Worker

STATIC_DIR = Path(__file__).parent / "static"
db = Database(settings.db_path)
worker = Worker(db, settings)


class CookieIn(BaseModel):
    name: str
    value: str
    domain: str | None = None
    path: str = "/"
    url: str | None = None


class JobCreate(BaseModel):
    urls: list[str] = Field(default_factory=list)
    name: str | None = None
    output_dir: str | None = None
    presets: list[str] = Field(default_factory=lambda: ["desktop"])
    viewport_width: int | None = None
    viewport_height: int | None = None
    user_agent: str | None = None
    javascript: bool | None = None
    css: bool | None = None
    cookies: list[CookieIn] = Field(default_factory=list)
    stable_ms: int | None = None
    timeout_ms: int | None = None
    video_seconds: int | None = None
    max_height_px: int | None = None
    dismiss_cookies: bool | None = None


class JobBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1)
    delete_files: bool = False


class JobBulkIds(BaseModel):
    ids: list[str] = Field(min_length=1)


def _check_token(authorization: str | None, x_api_key: str | None) -> None:
    if not settings.api_token:
        return
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1].strip()
    provided = x_api_key or bearer
    if provided != settings.api_token:
        raise HTTPException(status_code=401, detail="unauthorized")


def _eta(job: dict[str, Any]) -> int | None:
    remaining = (job.get("total") or 0) - (job.get("done") or 0)
    avg = job.get("avg_ms")
    if remaining <= 0 or not avg:
        return 0 if remaining <= 0 else None
    return int((remaining * avg) / 1000)


def _with_eta(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    job["eta_seconds"] = _eta(job)
    failed = int(job.get("failed") or 0)
    partial = int(job.get("partial") or 0)
    job["unsaved"] = max(failed - partial, 0)
    return job


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.ensure_dirs()
    await db.connect()
    await worker.start()
    yield
    await worker.stop()
    await db.close()


app = FastAPI(title="Page Scraper", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__}


@app.get("/api/config")
async def get_config(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    return {
        "viewport_width": settings.viewport_width,
        "viewport_height": settings.viewport_height,
        "max_height_px": settings.max_height_px,
        "stable_ms": settings.stable_ms,
        "timeout_ms": settings.timeout_ms,
        "concurrency": settings.concurrency,
        "output_root": str(settings.output_root),
        "presets": known_presets(),
        "video": settings.video,
        "video_fps": settings.video_fps,
        "video_seconds": settings.video_seconds,
        "auth_required": bool(settings.api_token),
    }


@app.get("/api/jobs")
async def list_jobs(
    status: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    jobs = await db.list_jobs(status)
    counts = await db.job_counts()
    return {"jobs": [_with_eta(j) for j in jobs], "counts": counts}


@app.post("/api/jobs", status_code=201)
async def create_job(
    body: JobCreate,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    urls: list[str] = []
    try:
        for raw in body.urls:
            for piece in raw.replace(",", "\n").splitlines():
                piece = piece.strip()
                if not piece or piece.startswith("#"):
                    continue
                urls.append(parse_http_url(piece))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not urls:
        raise HTTPException(status_code=400, detail="no URLs provided")

    presets = body.presets or ["desktop"]
    unknown = [p for p in presets if p not in known_presets()]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown presets: {unknown}")

    try:
        output_dir = resolve_output_dir(settings.output_root, body.output_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    options: dict[str, Any] = {
        "cookies": [c.model_dump(exclude_none=True) for c in body.cookies],
        "stable_ms": body.stable_ms or settings.stable_ms,
        "timeout_ms": body.timeout_ms or settings.timeout_ms,
        "video_seconds": body.video_seconds or settings.video_seconds,
        "max_height_px": body.max_height_px or settings.max_height_px,
        "dismiss_cookies": settings.dismiss_cookies
        if body.dismiss_cookies is None
        else body.dismiss_cookies,
    }
    if body.viewport_width or body.viewport_height:
        options["viewport"] = {
            "width": body.viewport_width or settings.viewport_width,
            "height": body.viewport_height or settings.viewport_height,
        }
    if body.user_agent:
        options["user_agent"] = body.user_agent
    if body.javascript is not None:
        options["javascript"] = body.javascript
    if body.css is not None:
        options["css"] = body.css
    items = [(url, preset) for url in urls for preset in presets]
    name = body.name or _default_name(urls, presets)
    job = await db.create_job(
        name=name,
        output_dir=str(output_dir),
        options=options,
        items=items,
    )
    return _with_eta(job)


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    items = await db.list_items(job_id)
    payload = _with_eta(job)
    payload["items"] = [_with_file_urls(job_id, item) for item in items]
    return payload


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    job = await db.cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _with_eta(job)


@app.get("/api/jobs/{job_id}/items/{item_id}/{kind}")
async def get_item_file(
    job_id: str,
    item_id: str,
    kind: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    if kind not in {"screenshot", "dom", "video", "meta"}:
        raise HTTPException(status_code=404, detail="unknown file")
    item = await db.get_item(job_id, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if kind == "meta":
        directory = item.get("output_path")
        raw = str(Path(directory) / "meta.json") if directory else None
        media = "application/json"
    else:
        raw = item.get(f"{kind}_path")
        media = {
            "screenshot": "image/png",
            "dom": "text/html; charset=utf-8",
            "video": "video/mp4",
        }[kind]
    path = resolve_output_file(settings.output_root, raw)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, media_type=media, filename=path.name)


@app.post("/api/jobs/delete")
async def delete_jobs(
    body: JobBulkDelete,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    return await _delete_jobs(body.ids, body.delete_files)


@app.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str,
    delete_files: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    result = await _delete_jobs([job_id], delete_files)
    if result["deleted"] == 0:
        raise HTTPException(status_code=404, detail="job not found")
    return result


@app.post("/api/jobs/rerun")
async def rerun_jobs(
    body: JobBulkIds,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    result = await db.clone_jobs(body.ids)
    return {
        "created": [job["id"] for job in result["created"]],
        "jobs": [_with_eta(job) for job in result["created"]],
        "skipped": result["skipped"],
    }


@app.post("/api/jobs/{job_id}/rerun")
async def rerun_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    result = await db.clone_jobs([job_id])
    if not result["created"] and result["skipped"] and result["skipped"][0]["reason"] == "not found":
        raise HTTPException(status_code=404, detail="job not found")
    if not result["created"]:
        reason = result["skipped"][0]["reason"] if result["skipped"] else "skipped"
        raise HTTPException(status_code=409, detail=f"cannot rerun: {reason}")
    payload = _with_eta(result["created"][0])
    payload["source_id"] = job_id
    return payload


@app.post("/api/jobs/retry")
async def retry_jobs(
    body: JobBulkIds,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    return await db.retry_unsaved(body.ids)


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
):
    _check_token(authorization, x_api_key)
    result = await db.retry_unsaved([job_id])
    if not result["retried"] and result["skipped"] and result["skipped"][0]["reason"] == "not found":
        raise HTTPException(status_code=404, detail="job not found")
    if not result["retried"]:
        reason = result["skipped"][0]["reason"] if result["skipped"] else "skipped"
        raise HTTPException(status_code=409, detail=f"cannot retry: {reason}")
    job = await db.get_job(job_id)
    payload = _with_eta(job) if job else {"id": job_id}
    payload["retry"] = result
    return payload


async def _delete_jobs(job_ids: list[str], delete_files: bool) -> dict[str, Any]:
    deleted = await db.delete_jobs(job_ids)
    files_removed = 0
    if delete_files:
        items = [item for job in deleted for item in job.get("items") or []]
        files_removed = unlink_capture_files(settings.output_root, items)
    return {
        "ok": True,
        "deleted": len(deleted),
        "ids": [job["id"] for job in deleted],
        "files_removed": files_removed,
    }


def _with_file_urls(job_id: str, item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item)
    base = f"/api/jobs/{job_id}/items/{item['id']}"
    if item.get("screenshot_path"):
        item["screenshot_url"] = f"{base}/screenshot"
    if item.get("dom_path"):
        item["dom_url"] = f"{base}/dom"
    if item.get("video_path"):
        item["video_url"] = f"{base}/video"
    if item.get("output_path"):
        item["meta_url"] = f"{base}/meta"
    return item


def _default_name(urls: list[str], presets: list[str]) -> str:
    from urllib.parse import urlparse

    host = urlparse(urls[0]).netloc
    extra = f" +{len(urls) - 1}" if len(urls) > 1 else ""
    preset = "" if presets == ["desktop"] else f" [{', '.join(presets)}]"
    return f"{host}{extra}{preset}"
