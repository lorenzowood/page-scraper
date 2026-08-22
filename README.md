# Page Scraper

Local service that takes a URL (or a batch) and writes a **full-page PNG**, the **live serialized DOM**, and an optional **viewport video** once the page has gone quiet. The CLI talks to a REST API. The Web UI is a qBittorrent-style job list: queued, running, finished, progress, ETA.

It is a **page** scraper, not a site crawler. Chromium (via Playwright) is the engine. Brave is not used; it is a common source of hung headless sessions.

The Python package import is still `pagecapture`; the CLI and Compose service are `page-scraper`.

## What you get

Defaults:

- Desktop Chromium, **1440×900 window**, PNG of the **full scrollable page** (`scale=css`, so 1× pixels). The window height stays 1440×900 so `100vh` heroes layout as they do on a desktop; the screenshot still includes everything below the fold. Do not stretch Chromium’s real viewport to the document height — the compositor paints black.
- DOM as `document.documentElement.outerHTML` after JS has run
- Wait until the DOM signature is unchanged for **5s**, give up after **30s**
- Cookie banners: hide known overlays first (so “Allow all” handlers that reload the page do not interrupt the capture), then click visible Accept/Allow/Reject-style buttons (and known CMP selectors). Retry after scroll, every **5s** while waiting, and again immediately before the still PNG. Leftover bars with no clickable Accept (GOV.UK `#global-cookie-message`, WDS `.cookie-policy`) are hidden.
- Viewport **H.264** sampled at up to **5 fps** for **30s** (`VIDEO_SECONDS`, x264 `qp 0`, full document height). Playback uses the wall-clock span of those frames, so it runs in real time even when a tall page screenshot is slower than 5 fps. The window stays 1440×900; each frame is a full-page JPEG (`full_page=True`). Use VLC or mpv; Finder/QuickTime often show a black frame on this encode. If the page goes quiet first, the clip ends there.
- Height cap **50,000px** (Chromium may refuse huge bitmaps; the worker falls back to 16384 / 8192)

Failure policy:

- Page does not load (navigation error, HTTP 4xx/5xx, empty document): **no files**, item marked `failed` / `load`
- Page loads but never goes quiet (ticking clock, live widgets, carousel): **keep PNG + DOM + video**, item marked `complete` with reason `unstable`

Layout matches `examples/`:

```
{output}/{host}/: /screenshot 2026-08-22 00-10-00.png
{output}/{host}/: /dom 2026-08-22 00-10-00.html
{output}/{host}/: /video 2026-08-22 00-10-00.mp4
{output}/{host}/: /meta.json
```

`:` stands in for `/` so the homepage does not spill into the host directory. Extra presets land in a subfolder (`iphone/`, `no-css/`, `no-js/`).

## VM size

Chromium is the cost. A qBittorrent VM at 2 CPU / 2 GiB is more than this needs if you keep concurrency at 1.

| | Recommendation |
| --- | --- |
| CPU | **1 vCPU** |
| RAM | **1.5 GiB** (`mem_limit` in compose). 1 GiB is tight once a tall page is screenshot. |
| Disk | **8–12 GiB** boot (Playwright image). Output goes on the NAS, not the boot disk. |
| `/dev/shm` | compose sets `shm_size: 256mb` |

Do not run two browser workers on that RAM. Leave `CONCURRENCY=1`.

## Deploy (Proxmox + NAS)

Same pattern as a qBittorrent setup: the container writes to a locked-down NAS inbox; a completion hook on the NAS can move finished jobs somewhere the service cannot reach.

```bash
cp .env.example .env
# OUTPUT_DIR=/mnt/nas/page-scraper/inbox
# DATA_DIR=/mnt/nas/page-scraper/state
docker compose up -d --build
```

The Python package is copied into the image. After code changes, rebuild (`docker compose up -d --build --force-recreate`). Compose does not bind-mount the package.

Open `http://<vm>:8080`. Optional `API_TOKEN` in `.env`; the CLI uses `PAGESCRAPER_TOKEN` or `--token`. The UI can store a token in `localStorage` as `page-scraper-token`.

Completion hook (optional):

```bash
ON_COMPLETE_HOOK=/output/hooks/on-complete.sh
```

The process receives `JOB_ID`, `JOB_STATUS`, `JOB_OUTPUT_DIR`, `JOB_FAILED`, `JOB_TOTAL`. See `hooks/on-complete.example.sh`. If you want the qBittorrent security split, run the *move* script on the NAS via the API (`GET /api/jobs` until `status=completed`) rather than inside the container.

If a capture hangs the browser, the worker restarts Chromium. If that restart itself hangs, the process exits and Docker recreates it; in-flight items go back to `queued`.

Jobs live in SQLite under `DATA_DIR` (`page-scraper.sqlite3`; an older `pagecapture.sqlite3` is still used if that is what is already there). The UI is a live read of that database. A restart resets `running` items to `queued`.

## CLI

From this repo, or `pip install .` on a machine that can see the VM:

```bash
export PAGESCRAPER_URL=http://page-scraper.lan:8080

page-scraper add https://orcarenewables.co.uk/
page-scraper add -f urls.txt -p desktop -p iphone -o client-a --wait
page-scraper jobs
page-scraper status JOB_ID
page-scraper wait JOB_ID
page-scraper get JOB_ID -o ./captures
page-scraper cancel JOB_ID
page-scraper rm JOB_ID [--files]
page-scraper rerun JOB_ID
```

Inside the container:

```bash
docker compose exec page-scraper page-scraper add --wait https://example.com/
```

`--output` is a folder under the NAS root, not an arbitrary host path.

`get` downloads each item’s PNG, HTML, MP4, and `meta.json` through the API. `rm --files` deletes that job’s capture files (not the whole NAS root), then `meta.json` and empty host folders.

## API

`POST /api/jobs`

```json
{
  "urls": ["https://example.com/", "https://example.org/"],
  "presets": ["desktop", "iphone"],
  "output_dir": "client-a",
  "cookies": [{"name": "session", "value": "…"}],
  "user_agent": null,
  "stable_ms": 5000,
  "timeout_ms": 30000,
  "video_seconds": 30
}
```

`GET /api/jobs` — list + counts + `eta_seconds`  
`GET /api/jobs/{id}` — items, including `screenshot_url` / `dom_url` / `video_url` / `meta_url`  
`GET /api/jobs/{id}/items/{item_id}/{screenshot|dom|video|meta}` — the file itself (paths must sit under the output root)  
`POST /api/jobs/{id}/cancel`  
`DELETE /api/jobs/{id}?delete_files=false` — drop from the list; `delete_files=true` also removes that job's PNG/DOM/video, `meta.json`, and empty folders  
`POST /api/jobs/delete` — `{ "ids": ["…"], "delete_files": false }`  
`POST /api/jobs/{id}/rerun` / `POST /api/jobs/rerun` — `{ "ids": ["…"] }` re-queues items (skips jobs that are still running)

Presets: `desktop`, `iphone`, `no-css`, `no-js`. Job-level cookies / UA / viewport / `--no-js` overlay every preset.

## Why not ArchiveBox / Browserless / shot-scraper

Those cover pieces (PNG, DOM dump, or a browser pool). None of them is a small queued “PNG + stable DOM + variants + ETA + NAS inbox” service. This repo is that thin layer on Playwright’s official Python image, which is the shortcut that actually stays up for thousands of unrelated homepages.
