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
- Viewport **H.264** sampled at up to **5 fps** for **30s** (`VIDEO_SECONDS`, x264 `qp 0`, full document height). Playback uses the wall-clock span of those frames, so it runs in real time even when a tall page screenshot is slower than 5 fps. Frames are padded to a constant size so ffmpeg does not abort when the page grows mid-clip. The window stays 1440×900; each frame is a full-page JPEG (`full_page=True`). Use VLC or mpv; Finder/QuickTime often show a black frame on this encode. If the page goes quiet first, the clip ends there.
- YouTube iframes cannot play in headless Chromium; they are replaced with the public poster image before capture.
- Before the still PNG, motion is frozen (CSS animations off, videos paused, oversized `position:fixed` SEO layers hidden) so a full-page stitch does not capture Duda/Elementor slide-ins the way a first-pass browser plugin does.
- Height cap **50,000px** (Chromium may refuse huge bitmaps; the worker falls back to 16384 / 8192)

Failure policy:

- Page does not load (navigation error, HTTP 4xx/5xx, empty document): **no files**, item marked `failed` / `load`
- Page loads but never goes quiet (ticking clock, live widgets, carousel): **keep PNG + DOM + video**, item marked `complete` with reason `unstable`
- PNG is missing (Chromium OOM, screenshot timeout, encoder killed): **do not link a 404**; item marked `failed` / `oom` or `screenshot`, with `error` set. `Retry errors` will pick these up.

Layout matches `examples/`:

```
{output}/{host}/: /screenshot 2026-08-22 00-10-00.png
{output}/{host}/: /dom 2026-08-22 00-10-00.html
{output}/{host}/: /video 2026-08-22 00-10-00.mp4
{output}/{host}/: /meta.json
```

`:` stands in for `/` so the homepage does not spill into the host directory. Extra presets land in a subfolder (`iphone/`, `no-css/`, `no-js/`).

## Install (Debian + Docker, same host as qBittorrent)

You do not need a separate VM. This is a second Compose stack on the box that already runs qBittorrent. It listens on **8081** so it does not clash with qBittorrent’s usual **8080**. Chromium is capped at **2.5 GiB** RAM and **1 CPU** in `docker-compose.yml`; leave `CONCURRENCY=1`. Make sure the host still has that RAM spare after qBittorrent (a 4 GiB VM is the practical minimum beside qBittorrent).

Same storage pattern as qBittorrent: keep the SQLite job list and the capture files on a NAS share (or at least put captures on the NAS). Do not point `OUTPUT_DIR` at qBittorrent’s download folder if you want the same security split (locked-down inbox vs a share the scraper cannot write to after a move).

### 1. Compose plugin and git

Docker Engine is not enough on its own. You need Compose v2 (`docker compose`, with a space) and git:

```bash
sudo apt-get update
sudo apt-get install -y git docker-compose-plugin
docker compose version
```

Your user should be in the `docker` group (`sudo usermod -aG docker "$USER"`, then log out and back in). Otherwise prefix the compose commands with `sudo`.

### 2. Clone the repo

```bash
sudo mkdir -p /opt/page-scraper
sudo chown "$USER:$USER" /opt/page-scraper
git clone https://github.com/lorenzowood/page-scraper.git /opt/page-scraper
cd /opt/page-scraper
```

### 3. NAS folders and `.env`

Create an inbox (and optional state dir) on the same kind of mount you already use for qBittorrent, then copy the example env:

```bash
sudo mkdir -p /mnt/nas/page-scraper/inbox /mnt/nas/page-scraper/state
cp .env.example .env
```

Edit `.env`:

```bash
TZ=Europe/London
DATA_DIR=/mnt/nas/page-scraper/state
OUTPUT_DIR=/mnt/nas/page-scraper/inbox
CONCURRENCY=1
```

`DATA_DIR` is the SQLite job list. `OUTPUT_DIR` is where PNG / HTML / MP4 land. Paths are **host** paths; Compose bind-mounts them into the container as `/data` and `/output`. Leave `API_TOKEN` empty until you want the UI/CLI to send a bearer token. Set it to a long random string if the host is reachable off-LAN.

The first image build pulls Playwright’s Chromium image plus ffmpeg. That is a few gigabytes; the boot disk only needs room for images, not for captures.

### 4. Build and start

```bash
cd /opt/page-scraper
docker compose up -d --build
docker compose ps
curl -sS http://127.0.0.1:8081/api/health
```

You should see `{"ok":true,"version":"0.1.0"}` (plus `cgroup_oom_kills` if the container has already killed a process). Open `http://<server>:8081` from a browser. If you use `ufw`, allow the port (`sudo ufw allow 8081/tcp`).

Queue a URL from the UI (**Add URLs**) or from the host. A host with no scheme is treated as `https://`.

```bash
docker compose exec page-scraper page-scraper add --wait https://example.com/
```

Files appear under `OUTPUT_DIR/{host}/:/`. `restart: unless-stopped` brings the container back after a reboot.

### 5. Updates

The Python package is **copied into the image**, not bind-mounted. After `git pull`, rebuild:

```bash
cd /opt/page-scraper
git pull
docker compose up -d --build --force-recreate
```

If you are not in `/opt/page-scraper`, the running container knows where Compose was started from:

```bash
docker inspect page-scraper --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

### Optional completion hook

```bash
# in .env, path is inside the container (OUTPUT_DIR is mounted at /output)
ON_COMPLETE_HOOK=/output/hooks/on-complete.sh
```

Copy `hooks/on-complete.example.sh` into that inbox as `hooks/on-complete.sh`. The process receives `JOB_ID`, `JOB_STATUS`, `JOB_OUTPUT_DIR`, `JOB_FAILED`, `JOB_TOTAL`. If you want the qBittorrent-style move to a share this container cannot write, run the move on the NAS (or another host) by watching `GET /api/jobs` until `status=completed`, rather than inside this container.

If a capture hangs Chromium, the worker recycles the browser. If that recycle hangs, the process exits, Docker recreates the container, and in-flight items go back to `queued`. Jobs live in SQLite under `DATA_DIR` (`page-scraper.sqlite3`; an older `pagecapture.sqlite3` is still used if that is what is already there). The UI is a live read of that database.

| | On this host |
| --- | --- |
| Extra CPU | **1** (`cpus: "1.0"`) |
| Extra RAM | **2.5 GiB** (`mem_limit: 2500m`) for Chromium + ffmpeg. A busy full-page capture (YouTube embeds, looping hero video) peaked around **1.5–1.7 GiB** here. Beside qBittorrent, give the **VM 4 GiB**. A 2 GiB box will OOM those pages even if quieter sites succeed. |
| Boot disk | Playwright image, on the order of **8–12 GiB** the first time. Captures go on the NAS. |
| `/dev/shm` | compose sets `shm_size: 256mb` (needed by Chromium) |

## Tests

No browser. The suite covers paths, cookie-label matching, job clone/retry, OOM/missing-PNG reporting, and the HTTP API with the worker stubbed out. Needs Python 3.12+ (same floor as the service).

```bash
python3 -m pip install -e '.[dev]'
pytest
```

## CLI

From a machine that can see the service (`pip install .` in this repo, or use the binary inside the container):

```bash
export PAGESCRAPER_URL=http://<server>:8081

page-scraper add https://orcarenewables.co.uk/
page-scraper add -f urls.txt -p desktop -p iphone -o client-a --wait
page-scraper jobs
page-scraper status JOB_ID
page-scraper wait JOB_ID
page-scraper get JOB_ID -o ./captures
page-scraper cancel JOB_ID
page-scraper rm JOB_ID [--files]
page-scraper rerun JOB_ID
page-scraper retry JOB_ID
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

`GET /api/health` — `{ "ok": true, "version": "…" }`. Adds `cgroup_oom_kills` when the container has killed a process.  
`GET /api/jobs` — list + counts + `eta_seconds`  
`GET /api/jobs/{id}` — items, including `screenshot_url` / `dom_url` / `video_url` / `meta_url` **only when that file exists on disk**. Ghost paths are omitted; `missing` lists the kinds (`png`, `html`, `video`) and `error` is set (for example `missing on disk: png`). Failed items use `reason` `load`, `oom`, `screenshot`, `timeout`, `crash`, plus an `error` string. The UI shows `reason` and `error` together.  
`GET /api/jobs/{id}/items/{item_id}/{screenshot|dom|video|meta}` — the file itself (paths must sit under the output root); **404** if it is gone  
`POST /api/jobs/{id}/cancel`  
`DELETE /api/jobs/{id}?delete_files=false` — drop from the list; `delete_files=true` also removes that job's PNG/DOM/video, `meta.json`, and empty folders  
`POST /api/jobs/delete` — `{ "ids": ["…"], "delete_files": false }`  
`POST /api/jobs/{id}/rerun` / `POST /api/jobs/rerun` — `{ "ids": ["…"] }` queues a **new** job with the same URLs, presets, and options. The original job and its files stay put.  
`POST /api/jobs/{id}/retry` / `POST /api/jobs/retry` — `{ "ids": ["…"] }` re-queues items in that job that never wrote a PNG (`oom`, `screenshot`, `crash`, `load`, cancelled leftovers). Successful captures are left alone.

If the page navigates after load (cookie reload, JS redirect), the worker waits for the new document and continues instead of marking the item `crash`.

Presets: `desktop`, `iphone`, `no-css`, `no-js`. Job-level cookies / UA / viewport / `--no-js` overlay every preset.

## Why not ArchiveBox / Browserless / shot-scraper

Those cover pieces (PNG, DOM dump, or a browser pool). None of them is a small queued “PNG + stable DOM + variants + ETA + NAS inbox” service. This repo is that thin layer on Playwright’s official Python image, which is the shortcut that actually stays up for thousands of unrelated homepages.
