from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import Settings
from .cookies import dismiss_cookie_banners
from .paths import timestamp_label, unique_path, url_output_dir

log = logging.getLogger("page-scraper.capture")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
]

# Below-the-fold carousels often pause until they intersect the window.
# Force them to start so full-page frames actually show motion.
FORCE_IN_VIEW_JS = """
(() => {
  const Native = window.IntersectionObserver;
  if (!Native) return;
  window.IntersectionObserver = class {
    constructor(callback, options) {
      this._callback = callback;
      this._native = new Native((entries, observer) => {
        callback(entries.map((entry) => {
          try {
            return new Proxy(entry, {
              get(target, prop) {
                if (prop === "isIntersecting") return true;
                if (prop === "intersectionRatio") return Math.max(target.intersectionRatio, 1);
                const value = target[prop];
                return typeof value === "function" ? value.bind(target) : value;
              }
            });
          } catch (e) {
            return entry;
          }
        }), observer);
      }, options);
    }
    observe(target) {
      this._native.observe(target);
      try {
        const rect = target.getBoundingClientRect();
        this._callback([{
          isIntersecting: true,
          intersectionRatio: 1,
          target,
          time: performance.now(),
          boundingClientRect: rect,
          intersectionRect: rect,
          rootBounds: null,
        }], this);
      } catch (e) {}
    }
    unobserve(target) { this._native.unobserve(target); }
    disconnect() { this._native.disconnect(); }
    takeRecords() { return this._native.takeRecords ? this._native.takeRecords() : []; }
  };
})();
"""

SIGNATURE_JS = """() => {
  const root = document.documentElement;
  const body = document.body;
  const images = Array.from(document.images || []);
  const loaded = images.filter((img) => img.complete).length;
  const height = Math.max(
    root ? root.scrollHeight : 0,
    body ? body.scrollHeight : 0,
    0
  );
  return [
    height,
    root ? root.childElementCount : 0,
    (root && root.innerHTML ? root.innerHTML.length : 0),
    loaded,
    images.length
  ].join(":");
}"""

HEIGHT_JS = """() => Math.max(
  document.documentElement ? document.documentElement.scrollHeight : 0,
  document.body ? document.body.scrollHeight : 0,
  0
)"""

EMPTY_JS = """() => {
  const html = document.documentElement ? document.documentElement.outerHTML : "";
  const text = document.body ? document.body.innerText : "";
  return { html: html.length, text: (text || "").trim().length };
}"""


@dataclass
class CaptureResult:
    status: str
    reason: str | None = None
    saved: bool = False
    error: str | None = None
    screenshot_path: str | None = None
    dom_path: str | None = None
    video_path: str | None = None
    output_path: str | None = None
    http_status: int | None = None
    height_px: int | None = None
    cookie_dismissed: bool = False
    height_capped: bool = False
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def item_fields(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "saved": 1 if self.saved else 0,
            "error": self.error,
            "screenshot_path": self.screenshot_path,
            "dom_path": self.dom_path,
            "video_path": self.video_path,
            "output_path": self.output_path,
            "http_status": self.http_status,
            "height_px": self.height_px,
            "cookie_dismissed": 1 if self.cookie_dismissed else 0,
            "height_capped": 1 if self.height_capped else 0,
            "duration_ms": self.duration_ms,
        }


class BrowserEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser: Browser | None = None
        self.captures = 0

    async def start(self) -> None:
        await self.stop()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=LAUNCH_ARGS,
        )
        self.captures = 0

    async def stop(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=5)
            except Exception:
                pass
        if playwright is not None:
            try:
                await asyncio.wait_for(playwright.stop(), timeout=5)
            except Exception:
                pass

    async def restart(self) -> None:
        await self.start()

    def _require_browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("browser is not running")
        return self._browser

    async def capture(
        self,
        *,
        url: str,
        output_root: Path,
        options: dict[str, Any],
    ) -> CaptureResult:
        started = asyncio.get_event_loop().time()
        timeout_ms = int(options.get("timeout_ms", self.settings.timeout_ms))
        extra = 0
        if bool(options.get("video", self.settings.video)):
            extra = int(options.get("video_seconds", self.settings.video_seconds)) + 30
        try:
            result = await asyncio.wait_for(
                self._capture_inner(url, output_root, options),
                timeout=(timeout_ms / 1000) * 2 + 40 + extra,
            )
        except asyncio.TimeoutError:
            result = CaptureResult(
                status="failed",
                reason="timeout",
                error="capture watchdog fired",
            )
        except Exception as exc:
            result = CaptureResult(
                status="failed",
                reason="crash",
                error=str(exc),
            )
        result.duration_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        self.captures += 1
        return result

    async def _capture_inner(
        self, url: str, output_root: Path, options: dict[str, Any]
    ) -> CaptureResult:
        viewport = options.get("viewport") or {
            "width": self.settings.viewport_width,
            "height": self.settings.viewport_height,
        }
        javascript = bool(options.get("javascript", True))
        css = bool(options.get("css", True))
        timeout_ms = int(options.get("timeout_ms", self.settings.timeout_ms))
        stable_ms = int(options.get("stable_ms", self.settings.stable_ms))
        max_height = int(options.get("max_height_px", self.settings.max_height_px))
        user_agent = options.get("user_agent")
        cookies = options.get("cookies") or []
        dismiss = bool(options.get("dismiss_cookies", self.settings.dismiss_cookies))
        if not javascript:
            dismiss = False

        window_w = int(viewport["width"])
        window_h = int(viewport["height"])
        record_video = bool(options.get("video", self.settings.video))
        video_fps = int(options.get("video_fps", self.settings.video_fps))
        video_seconds = int(options.get("video_seconds", self.settings.video_seconds))

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": window_w, "height": window_h},
            "java_script_enabled": javascript,
            "device_scale_factor": float(options.get("device_scale_factor", 1)),
            "is_mobile": bool(options.get("is_mobile", False)),
            "has_touch": bool(options.get("has_touch", False)),
            "ignore_https_errors": True,
        }
        if user_agent:
            context_kwargs["user_agent"] = user_agent

        context = await self._require_browser().new_context(**context_kwargs)
        if javascript:
            await context.add_init_script(FORCE_IN_VIEW_JS)

        page: Page | None = None
        ffmpeg = None
        result = CaptureResult(status="failed", reason="crash")
        try:
            if cookies:
                await _apply_cookies(context, url, cookies)
            page = await context.new_page()
            page.set_default_timeout(min(timeout_ms, 15_000))
            if not css:
                await page.route("**/*.css", lambda route: route.abort())

            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=timeout_ms
                )
            except Exception as exc:
                result = CaptureResult(
                    status="failed",
                    reason="load",
                    error=str(exc),
                    http_status=None,
                )
                return result

            http_status = response.status if response is not None else None
            current = page.url or ""
            if current.startswith("chrome-error://") or current == "about:blank":
                result = CaptureResult(
                    status="failed",
                    reason="load",
                    error=f"browser error page ({current})",
                    http_status=http_status,
                )
                return result
            if http_status is not None and http_status >= 400:
                result = CaptureResult(
                    status="failed",
                    reason="load",
                    error=f"HTTP {http_status}",
                    http_status=http_status,
                )
                return result

            sizes = await page.evaluate(EMPTY_JS)
            if sizes["html"] < 180 and sizes["text"] < 20:
                result = CaptureResult(
                    status="failed",
                    reason="load",
                    error="page was empty",
                    http_status=http_status,
                )
                return result

            cookie_dismissed = False

            async def try_cookies() -> None:
                nonlocal cookie_dismissed
                if not dismiss:
                    return
                try:
                    if await asyncio.wait_for(dismiss_cookie_banners(page), timeout=4):
                        cookie_dismissed = True
                except Exception:
                    pass

            if not css:
                await page.evaluate(
                    """() => {
                      document.querySelectorAll('style, link[rel="stylesheet"]').forEach((el) => el.remove());
                      document.querySelectorAll('[style]').forEach((el) => el.removeAttribute('style'));
                    }"""
                )

            try:
                await page.evaluate(
                    """() => {
                      document.querySelectorAll('img[loading="lazy"]').forEach((img) => {
                        img.loading = "eager";
                      });
                    }"""
                )
            except Exception:
                pass
            await try_cookies()
            await scroll_page(page)
            await try_cookies()

            directory = url_output_dir(output_root, url)
            if options.get("preset") and options["preset"] != "desktop":
                directory = directory / str(options["preset"])
            stamp = timestamp_label(datetime.now())
            screenshot_path = unique_path(directory, f"screenshot {stamp}", ".png")
            dom_path = unique_path(directory, f"dom {stamp}", ".html")
            video_path = unique_path(directory, f"video {stamp}", ".mp4")
            clock = asyncio.get_event_loop()
            jpeg_frames: list[bytes] = []
            max_frames = max(video_fps, 1) * max(video_seconds, 1)
            t_first: float | None = None
            t_last: float | None = None

            async def grab_frame() -> None:
                nonlocal t_first, t_last
                if len(jpeg_frames) >= max_frames:
                    return
                jpeg = await asyncio.wait_for(
                    page.screenshot(
                        type="jpeg",
                        quality=80,
                        full_page=True,
                        animations="allow",
                        caret="hide",
                        scale="css",
                    ),
                    timeout=5,
                )
                now = clock.time()
                if t_first is None:
                    t_first = now
                t_last = now
                jpeg_frames.append(jpeg)

            stable = await wait_until_stable(
                page,
                stable_ms=stable_ms,
                timeout_ms=timeout_ms,
                on_tick=grab_frame if record_video else None,
                interval=1.0 / max(video_fps, 1),
                on_periodic=try_cookies if dismiss else None,
                periodic_s=5.0,
            )

            result_video = None
            playback_fps = float(video_fps)
            if record_video and jpeg_frames:
                playback_fps = _playback_fps(len(jpeg_frames), t_first, t_last, video_fps)
                try:
                    ffmpeg = await _start_ffmpeg_pipe(video_path, playback_fps)
                    assert ffmpeg.stdin is not None
                    for jpeg in jpeg_frames:
                        ffmpeg.stdin.write(jpeg)
                        await asyncio.wait_for(ffmpeg.stdin.drain(), timeout=5)
                    video_ok = await _finish_ffmpeg_pipe(ffmpeg)
                    ffmpeg = None
                    if video_ok:
                        result_video = str(video_path)
                    else:
                        video_path.unlink(missing_ok=True)
                except Exception:
                    log.exception("could not encode video for %s", url)
                    if ffmpeg is not None:
                        await _finish_ffmpeg_pipe(ffmpeg, ignore_errors=True)
                        ffmpeg = None
                    video_path.unlink(missing_ok=True)

            # Banners can appear during encode (WDS waits on window.load + 500ms).
            await try_cookies()

            height = int(await page.evaluate(HEIGHT_JS) or 0)
            height_capped = height > max_height
            notes: list[str] = []
            try:
                still_capped = await asyncio.wait_for(
                    _screenshot(page, screenshot_path, height, max_height),
                    timeout=20,
                )
            except Exception:
                still_capped = True
                notes.append("still screenshot timed out")
            height_capped = bool(height_capped or still_capped)
            outer = await page.evaluate(
                "() => document.documentElement ? document.documentElement.outerHTML : ''"
            )
            dom_path.write_text(outer or "", encoding="utf-8")

            meta = {
                "url": url,
                "preset": options.get("preset"),
                "http_status": http_status,
                "stable": stable,
                "height_px": height,
                "height_capped": height_capped,
                "cookie_dismissed": cookie_dismissed,
                "javascript": javascript,
                "css": css,
                "viewport": {"width": window_w, "height": window_h},
                "capture_viewport": page.viewport_size,
                "video_fps": round(playback_fps, 4) if result_video else None,
                "video": video_path.name if result_video else None,
            }
            (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

            if not stable:
                notes.append("page did not stay stable; captured anyway")
            if record_video and not result_video:
                notes.append("video encode failed")
            result = CaptureResult(
                status="complete",
                reason=None if stable else "unstable",
                saved=True,
                screenshot_path=str(screenshot_path),
                dom_path=str(dom_path),
                video_path=result_video,
                output_path=str(directory),
                http_status=http_status,
                height_px=height,
                cookie_dismissed=cookie_dismissed,
                height_capped=height_capped,
                warnings=notes,
            )
            return result
        finally:
            if ffmpeg is not None:
                await _finish_ffmpeg_pipe(ffmpeg, ignore_errors=True)
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=5)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(context.close(), timeout=5)
            except Exception:
                pass


async def _apply_cookies(context: BrowserContext, url: str, cookies: list[dict[str, Any]]) -> None:
    prepared = []
    for cookie in cookies:
        item = dict(cookie)
        if "url" not in item and "domain" not in item:
            item["url"] = url
        prepared.append(item)
    if prepared:
        await context.add_cookies(prepared)


async def scroll_page(page: Page, *, budget_s: float = 12) -> None:
    """Walk the page so lazy content loads, then return to the top. Not recorded."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + budget_s
    try:
        step = int(await page.evaluate("() => window.innerHeight") or 900)
    except Exception:
        step = 900
    step = max(step, 1)
    y = 0
    last_height = 0
    while loop.time() < deadline:
        try:
            height = int(await page.evaluate(HEIGHT_JS) or 0)
        except Exception:
            break
        if height <= 0:
            break
        if y >= height and height == last_height:
            break
        last_height = height
        y = min(y + step, height)
        try:
            await page.evaluate("(top) => window.scrollTo(0, top)", y)
        except Exception:
            break
        await page.wait_for_timeout(80)
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass
    await page.wait_for_timeout(150)


async def wait_until_stable(
    page: Page,
    *,
    stable_ms: int,
    timeout_ms: int,
    on_tick=None,
    interval: float = 0.2,
    on_periodic=None,
    periodic_s: float = 5.0,
) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (timeout_ms / 1000)
    last = None
    changed_at = loop.time()
    last_periodic = loop.time()
    while loop.time() < deadline:
        tick = loop.time()
        if on_periodic is not None and (tick - last_periodic) >= periodic_s:
            try:
                await on_periodic()
            except Exception:
                log.exception("periodic cookie dismiss failed")
            last_periodic = loop.time()
        if on_tick is not None:
            try:
                await on_tick()
            except Exception:
                log.exception("video frame capture failed")
                on_tick = None
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            signature = await asyncio.wait_for(page.evaluate(SIGNATURE_JS), timeout=min(2.0, remaining))
        except Exception:
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))
            continue
        now = loop.time()
        if signature != last:
            last = signature
            changed_at = now
        elif (now - changed_at) * 1000 >= stable_ms:
            return True
        leftover = interval - (loop.time() - tick)
        if leftover > 0:
            await asyncio.sleep(min(leftover, max(0.0, deadline - loop.time())))
    return False


async def _clamp_document_height(page: Page, height: int) -> None:
    await page.evaluate(
        """(h) => {
          const root = document.documentElement;
          const body = document.body;
          root.style.setProperty('max-height', h + 'px', 'important');
          root.style.setProperty('overflow', 'hidden', 'important');
          if (body) {
            body.style.setProperty('max-height', h + 'px', 'important');
            body.style.setProperty('overflow', 'hidden', 'important');
          }
        }""",
        height,
    )


async def _screenshot(page: Page, path: Path, page_height: int, max_height: int) -> bool:
    kwargs: dict[str, Any] = {
        "path": str(path),
        "type": "png",
        "full_page": True,
        "animations": "disabled",
        "caret": "hide",
        "scale": "css",
    }
    capped = page_height > max_height
    try:
        await page.screenshot(**kwargs)
        return capped
    except Exception:
        pass
    kwargs["full_page"] = True
    limits = [max_height] if capped else [None]
    limits.extend(cap for cap in (16_384, 8_192, 4_096) if cap < (page_height or max_height))
    seen: set[int | None] = set()
    for limit in limits:
        if limit in seen:
            continue
        seen.add(limit)
        try:
            if limit is not None:
                await _clamp_document_height(page, limit)
            await page.screenshot(**kwargs)
            return bool(capped or limit)
        except Exception:
            continue
    return True


def _playback_fps(
    n_frames: int, t_first: float | None, t_last: float | None, sample_fps: int
) -> float:
    """Constant fps so n frames occupy the wall-clock span they were taken over."""
    sample = float(max(sample_fps, 1))
    if n_frames <= 1 or t_first is None or t_last is None or t_last <= t_first:
        return sample
    duration = (t_last - t_first) * n_frames / (n_frames - 1)
    if duration <= 0:
        return sample
    return n_frames / duration


async def _start_ffmpeg_pipe(dest: Path, fps: float):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    dest.parent.mkdir(parents=True, exist_ok=True)
    rate = max(fps, 0.01)
    return await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-f",
        "image2pipe",
        "-framerate",
        f"{rate:.6f}",
        "-vcodec",
        "mjpeg",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-qp",
        "0",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-movflags",
        "+faststart",
        str(dest),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def _finish_ffmpeg_pipe(proc, ignore_errors: bool = False) -> bool:
    try:
        if proc.stdin:
            proc.stdin.close()
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            if ignore_errors:
                return False
            raise RuntimeError("ffmpeg timed out")
        if proc.returncode != 0:
            if ignore_errors:
                return False
            detail = (err or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(detail or f"ffmpeg exited {proc.returncode}")
        return True
    except Exception:
        if ignore_errors:
            return False
        raise
