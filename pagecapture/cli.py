from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="page-scraper",
        description="Capture full-page PNG screenshots and serialized DOM via the local service.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("PAGESCRAPER_URL")
        or os.environ.get("PAGECAPTURE_URL", "http://127.0.0.1:8081"),
        help="Service base URL (or PAGESCRAPER_URL)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PAGESCRAPER_TOKEN") or os.environ.get("PAGECAPTURE_TOKEN", ""),
        help="API token if the service requires one",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="Queue URLs for capture")
    add.add_argument("urls", nargs="*", help="One or more URLs (https:// if you omit the scheme)")
    add.add_argument("-f", "--file", type=Path, help="File of URLs, one per line")
    add.add_argument("-n", "--name", help="Job name")
    add.add_argument("-o", "--output", help="Output directory inside the NAS root")
    add.add_argument(
        "-p",
        "--preset",
        action="append",
        dest="presets",
        default=[],
        help="Repeatable: desktop, iphone, no-css, no-js",
    )
    add.add_argument("--width", type=int)
    add.add_argument("--height", type=int)
    add.add_argument("--user-agent")
    add.add_argument("--no-js", action="store_true")
    add.add_argument("--no-css", action="store_true")
    add.add_argument("--cookie", action="append", default=[], help="name=value")
    add.add_argument("--stable-ms", type=int)
    add.add_argument("--timeout-ms", type=int)
    add.add_argument("--video-seconds", type=int)
    add.add_argument("--wait", action="store_true", help="Block until the job finishes")
    add.set_defaults(func=cmd_add)

    jobs = sub.add_parser("jobs", help="List jobs")
    jobs.set_defaults(func=cmd_jobs)

    status = sub.add_parser("status", help="Show one job")
    status.add_argument("job_id")
    status.set_defaults(func=cmd_status)

    cancel = sub.add_parser("cancel", help="Cancel remaining items in a job")
    cancel.add_argument("job_id")
    cancel.set_defaults(func=cmd_cancel)

    rm = sub.add_parser("rm", help="Remove job(s) from the list")
    rm.add_argument("job_ids", nargs="+")
    rm.add_argument(
        "--files",
        action="store_true",
        help="Also delete PNG, DOM, and video files",
    )
    rm.set_defaults(func=cmd_rm)

    rerun = sub.add_parser("rerun", help="Queue a new job with the same URLs and options")
    rerun.add_argument("job_ids", nargs="+")
    rerun.set_defaults(func=cmd_rerun)

    retry = sub.add_parser("retry", help="Re-queue items in a job that did not write files")
    retry.add_argument("job_ids", nargs="+")
    retry.set_defaults(func=cmd_retry)

    wait = sub.add_parser("wait", help="Wait for a job to finish")
    wait.add_argument("job_id")
    wait.set_defaults(func=cmd_wait)

    get = sub.add_parser("get", help="Download PNG, DOM, and video for a job")
    get.add_argument("job_id")
    get.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("."),
        help="Directory to write files into",
    )
    get.set_defaults(func=cmd_get)

    serve = sub.add_parser("serve", help="Run the service (development)")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


def _client(args: argparse.Namespace) -> httpx.Client:
    headers = {}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    return httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=30)


def _raise(resp: httpx.Response) -> None:
    if resp.is_error:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise SystemExit(f"{resp.status_code}: {detail}")


def cmd_add(args: argparse.Namespace) -> None:
    urls = list(args.urls)
    if args.file:
        urls.extend(
            line.strip()
            for line in args.file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    cookies = []
    for item in args.cookie:
        if "=" not in item:
            raise SystemExit(f"cookie must be name=value: {item}")
        name, value = item.split("=", 1)
        cookies.append({"name": name, "value": value})
    payload = {
        "urls": urls,
        "name": args.name,
        "output_dir": args.output,
        "presets": args.presets or ["desktop"],
        "viewport_width": args.width,
        "viewport_height": args.height,
        "user_agent": args.user_agent,
        "javascript": False if args.no_js else None,
        "css": False if args.no_css else None,
        "cookies": cookies,
        "stable_ms": args.stable_ms,
        "timeout_ms": args.timeout_ms,
        "video_seconds": args.video_seconds,
    }
    payload = {key: value for key, value in payload.items() if value not in (None, [])}
    with _client(args) as client:
        resp = client.post("/api/jobs", json=payload)
        _raise(resp)
        job = resp.json()
    print(job["id"])
    _print_job(job)
    if args.wait:
        _wait(args, job["id"])


def cmd_jobs(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.get("/api/jobs")
        _raise(resp)
        data = resp.json()
    for job in data["jobs"]:
        _print_job(job, compact=True)


def cmd_status(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.get(f"/api/jobs/{args.job_id}")
        _raise(resp)
        job = resp.json()
    _print_job(job)
    for item in job.get("items") or []:
        flag = "saved" if item.get("saved") else "no-files"
        video = " video" if item.get("video_path") else ""
        print(
            f"  {item['status']:9} {flag:8}{video:6} {item['preset']:8} {item['url']}"
            + (f"  {item['reason']}" if item.get("reason") else "")
        )


def cmd_cancel(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.post(f"/api/jobs/{args.job_id}/cancel")
        _raise(resp)
        _print_job(resp.json())


def cmd_rm(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.post(
            "/api/jobs/delete",
            json={"ids": args.job_ids, "delete_files": args.files},
        )
        _raise(resp)
        data = resp.json()
    print(f"removed {data.get('deleted', 0)}" + (f", files {data.get('files_removed', 0)}" if args.files else ""))


def cmd_retry(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.post("/api/jobs/retry", json={"ids": args.job_ids})
        _raise(resp)
        data = resp.json()
    for row in data.get("retried") or []:
        print(f"retried {row['items']} item(s) in {row['id']}")
    for row in data.get("skipped") or []:
        print(f"  skipped {row['id']}: {row['reason']}")


def cmd_rerun(args: argparse.Namespace) -> None:
    with _client(args) as client:
        resp = client.post("/api/jobs/rerun", json={"ids": args.job_ids})
        _raise(resp)
        data = resp.json()
    created = data.get("created") or []
    print(f"queued {len(created)} new job(s)")
    for job_id in created:
        print(job_id)
    for row in data.get("skipped") or []:
        print(f"  skipped {row['id']}: {row['reason']}")


def cmd_wait(args: argparse.Namespace) -> None:
    _wait(args, args.job_id)


def cmd_get(args: argparse.Namespace) -> None:
    from urllib.parse import urlparse

    dest = args.output
    dest.mkdir(parents=True, exist_ok=True)
    with _client(args) as client:
        resp = client.get(f"/api/jobs/{args.job_id}")
        _raise(resp)
        job = resp.json()
        saved = 0
        for item in job.get("items") or []:
            host = urlparse(item.get("url") or "").netloc or "page"
            preset = item.get("preset") or "desktop"
            for kind, url_key in (
                ("screenshot", "screenshot_url"),
                ("dom", "dom_url"),
                ("video", "video_url"),
                ("meta", "meta_url"),
            ):
                url = item.get(url_key)
                if not url:
                    continue
                file_resp = client.get(url, timeout=300)
                _raise(file_resp)
                suffix = {
                    "screenshot": ".png",
                    "dom": ".html",
                    "video": ".mp4",
                    "meta": ".json",
                }[kind]
                name = f"{host} {preset} {kind}{suffix}"
                path = dest / name
                path.write_bytes(file_resp.content)
                print(path)
                saved += 1
    if not saved:
        raise SystemExit("no files saved for that job")


def cmd_serve(_: argparse.Namespace) -> None:
    import uvicorn

    from .config import settings

    uvicorn.run(
        "pagecapture.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


def _wait(args: argparse.Namespace, job_id: str) -> None:
    with _client(args) as client:
        while True:
            resp = client.get(f"/api/jobs/{job_id}")
            _raise(resp)
            job = resp.json()
            _print_job(job, compact=True)
            if job["status"] in {"completed", "cancelled"}:
                if job.get("failed"):
                    raise SystemExit(1)
                return
            time.sleep(1)


def _print_job(job: dict, compact: bool = False) -> None:
    done = job.get("done") or 0
    total = job.get("total") or 0
    eta = job.get("eta_seconds")
    eta_s = _fmt_eta(eta)
    line = (
        f"{job['id'][:8]}  {job.get('status', '?'):10}  {done}/{total}"
        f"  fail={job.get('failed') or 0}  eta={eta_s}  {job.get('name') or ''}"
    )
    print(line)
    if not compact and job.get("output_dir"):
        print(f"  output: {job['output_dir']}")


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02}m"
    if minutes:
        return f"{minutes}m{secs:02}s"
    return f"{secs}s"


if __name__ == "__main__":
    main(sys.argv[1:])
