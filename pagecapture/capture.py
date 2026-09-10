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
from .presets import chrome_desktop_ua

log = logging.getLogger("page-scraper.capture")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--disable-blink-features=AutomationControlled",
]

HIDE_WEBDRIVER_JS = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (e) {}
})();
"""

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

# Headless Chromium does not run YouTube. Swap embeds for the public poster
# so the PNG/clip show a frame instead of a black box.
YOUTUBE_POSTERS_JS = """() => {
  const re = /(?:youtube(?:-nocookie)?\\.com\\/embed\\/|youtu\\.be\\/)([A-Za-z0-9_-]{6,})/;
  let n = 0;
  for (const iframe of Array.from(document.querySelectorAll('iframe'))) {
    const src = iframe.src || iframe.getAttribute('data-src') || '';
    const match = src.match(re);
    if (!match) continue;
    const img = document.createElement('img');
    img.src = 'https://i.ytimg.com/vi/' + match[1] + '/hqdefault.jpg';
    img.alt = iframe.title || 'YouTube';
    img.width = iframe.offsetWidth || 640;
    img.height = iframe.offsetHeight || 360;
    img.style.display = 'block';
    img.style.width = (iframe.offsetWidth || 640) + 'px';
    img.style.height = (iframe.offsetHeight || 360) + 'px';
    img.style.objectFit = 'cover';
    iframe.replaceWith(img);
    n += 1;
  }
  return n;
}"""

# Full-page screenshots scroll internally; Duda/Elementor then slide things in
# mid-stitch (same as a browser plugin's first pass). Freeze first.
FREEZE_STILL_JS = """() => {
  const style = document.createElement('style');
  style.setAttribute('data-page-scraper', 'freeze');
  style.textContent = `
    html.page-scraper-freeze, html.page-scraper-freeze * {
      animation: none !important;
      animation-delay: 0s !important;
      animation-play-state: paused !important;
      transition: none !important;
      scroll-behavior: auto !important;
    }
  `;
  document.documentElement.classList.add('page-scraper-freeze');
  document.head.appendChild(style);
  for (const v of document.querySelectorAll('video')) {
    try { v.pause(); } catch (e) {}
  }
  document.querySelectorAll('.animated, [data-aos], .elementor-invisible, .skrollable').forEach((el) => {
    el.classList.add('revealed', 'aos-animate');
    el.classList.remove('elementor-invisible');
    el.style.setProperty('opacity', '1', 'important');
    el.style.setProperty('transform', 'none', 'important');
    el.style.setProperty('visibility', 'visible', 'important');
  });
  const vh = window.innerHeight || 900;
  for (const el of document.querySelectorAll('*')) {
    let pos;
    try { pos = getComputedStyle(el).position; } catch (e) { continue; }
    if (pos !== 'fixed' && pos !== 'sticky') continue;
    const r = el.getBoundingClientRect();
    if (r.height < vh * 2.5) continue;
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'header' || tag === 'nav') continue;
    if (el.querySelector('header, nav, video')) continue;
    el.style.setProperty('display', 'none', 'important');
  }
  return true;
}"""

_NAV_LOSS = (
    "execution context was destroyed",
    "because of a navigation",
    "frame was detached",
    "target closed",
    "target page, context or browser has been closed",
)


def _is_nav_loss(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(needle in text for needle in _NAV_LOSS)


def should_continue_after_goto_error(exc: BaseException, page_url: str) -> bool:
    """True when navigation timed out but Chromium already has an http(s) document."""
    if "Timeout" not in str(exc):
        return False
    return page_url.startswith("http://") or page_url.startswith("https://")


def is_retryable_failure(
    reason: str | None, error: str | None, http_status: int | None
) -> bool:
    if reason in {"timeout", "oom", "screenshot"}:
        return True
    if http_status == 403:
        return True
    err = error or ""
    if "ERR_HTTP2_PROTOCOL_ERROR" in err:
        return True
    if "chrome-error://" in err:
        return True
    if "Timeout" in err and "exceeded" in err:
        return True
    return False


_OOM_MARKERS = (
    "out of memory",
    "enomem",
    "cannot allocate memory",
    "result_code_oom",
    "oom_kill",
    "oom-kill",
    "ran out of memory",
    "memory pressure",
)

_BROWSER_DEATH = (
    "target closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "page crashed",
    "renderer process",
    "connection closed",
)

_CGROUP_EVENTS = Path("/sys/fs/cgroup/memory.events")
_CGROUP_OOM_CONTROL = Path("/sys/fs/cgroup/memory/memory.oom_control")


def existing_output(path: Path | None) -> str | None:
    """Return the path string only when a non-empty file is on disk."""
    if path is None:
        return None
    try:
        if path.is_file() and path.stat().st_size > 0:
            return str(path)
    except OSError:
        return None
    return None


def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width, height from a JPEG SOF marker."""
    if len(data) < 10 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    end = len(data)
    while i + 8 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            return None
        i += 2 + seglen
    return None


def ffmpeg_scale_filter(frames: list[bytes]) -> str:
    """Constant-size pad so image2pipe does not abort when a frame grows."""
    max_w = max_h = 0
    for jpeg in frames:
        size = jpeg_size(jpeg)
        if not size:
            continue
        max_w = max(max_w, size[0])
        max_h = max(max_h, size[1])
    if max_w < 2 or max_h < 2:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    width = max_w + (max_w % 2)
    height = max_h + (max_h % 2)
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def is_oom_error(error: str | None) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in _OOM_MARKERS)


def read_cgroup_oom_kills() -> int:
    """Container OOM-kill count, or 0 when cgroup memory stats are unavailable."""
    try:
        if _CGROUP_EVENTS.is_file():
            for line in _CGROUP_EVENTS.read_text().splitlines():
                if line.startswith("oom_kill "):
                    return int(line.split()[1])
        if _CGROUP_OOM_CONTROL.is_file():
            for line in _CGROUP_OOM_CONTROL.read_text().splitlines():
                if line.startswith("oom_kills "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return 0
    return 0


def _looks_like_browser_death(error: str | None) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in _BROWSER_DEATH)


def apply_memory_failure(result: CaptureResult, oom_before: int) -> CaptureResult:
    """If the cgroup OOM-killed a process or the error is OOM, say so explicitly."""
    delta = max(0, read_cgroup_oom_kills() - oom_before)
    oom = delta > 0 or is_oom_error(result.error) or result.reason == "oom"
    if result.screenshot_path and result.status == "complete" and not is_oom_error(result.error):
        return result
    if not oom:
        if result.status == "failed" and _looks_like_browser_death(result.error):
            err = result.error or "browser process died"
            if "memory" not in err.lower():
                result.error = f"{err} (often out of memory)"
        return result
    result.status = "failed"
    result.reason = "oom"
    if not result.screenshot_path:
        result.saved = False
    detail = result.error or "PNG was not written"
    if "out of memory" in detail.lower():
        result.error = detail
    else:
        result.error = f"out of memory (container memory limit): {detail}"
    return result


async def _settle(page: Page, timeout_ms: float = 5000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        await asyncio.sleep(0.25)


async def eval_page(page: Page, expression: str, *args, retries: int = 4):
    last: Exception | None = None
    for _ in range(retries):
        try:
            return await page.evaluate(expression, *args)
        except Exception as exc:
            last = exc
            if not _is_nav_loss(exc):
                raise
            log.info("page navigated during evaluate; waiting for the new document")
            await _settle(page)
    assert last is not None
    raise last


async def screenshot_page(page: Page, **kwargs):
    last: Exception | None = None
    for _ in range(4):
        try:
            return await page.screenshot(**kwargs)
        except Exception as exc:
            last = exc
            if not _is_nav_loss(exc):
                raise
            log.info("page navigated during screenshot; waiting for the new document")
            await _settle(page)
    assert last is not None
    raise last


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
            ignore_default_args=["--enable-automation"],
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
        budget = (timeout_ms / 1000) * 2 + 40 + extra
        result = CaptureResult(status="failed", reason="crash")
        for attempt in range(2):
            oom_before = read_cgroup_oom_kills()
            try:
                result = await asyncio.wait_for(
                    self._capture_inner(url, output_root, options),
                    timeout=budget,
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
            result = apply_memory_failure(result, oom_before)
            if (
                result.status != "failed"
                or attempt == 1
                or not is_retryable_failure(result.reason, result.error, result.http_status)
            ):
                break
            log.warning("retrying %s after %s", url, result.error or result.reason)
            if result.reason in {"timeout", "oom"}:
                try:
                    await self.restart()
                except Exception:
                    log.exception("browser restart after watchdog failed")
                    break
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

        if not user_agent:
            user_agent = chrome_desktop_ua(self._require_browser().version)

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": window_w, "height": window_h},
            "java_script_enabled": javascript,
            "device_scale_factor": float(options.get("device_scale_factor", 1)),
            "is_mobile": bool(options.get("is_mobile", False)),
            "has_touch": bool(options.get("has_touch", False)),
            "ignore_https_errors": True,
            "locale": "en-GB",
            "timezone_id": "Europe/London",
            "extra_http_headers": {"Accept-Language": "en-GB,en;q=0.9"},
            "user_agent": user_agent,
        }

        context = await self._require_browser().new_context(**context_kwargs)
        if javascript:
            await context.add_init_script(HIDE_WEBDRIVER_JS)
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
                current = page.url or ""
                if should_continue_after_goto_error(exc, current):
                    log.warning(
                        "domcontentloaded timed out for %s; continuing with %s",
                        url,
                        current,
                    )
                    response = None
                else:
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

            try:
                sizes = await eval_page(page, EMPTY_JS)
            except Exception as exc:
                result = CaptureResult(
                    status="failed",
                    reason="load" if _is_nav_loss(exc) else "crash",
                    error=str(exc),
                    http_status=http_status,
                )
                return result
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
                try:
                    await eval_page(
                        page,
                        """() => {
                          document.querySelectorAll('style, link[rel="stylesheet"]').forEach((el) => el.remove());
                          document.querySelectorAll('[style]').forEach((el) => el.removeAttribute('style'));
                        }""",
                    )
                except Exception:
                    pass

            try:
                await eval_page(
                    page,
                    """() => {
                      document.querySelectorAll('img[loading="lazy"]').forEach((img) => {
                        img.loading = "eager";
                      });
                    }""",
                )
            except Exception:
                pass
            await try_cookies()
            await scroll_page(page)
            await try_cookies()
            try:
                await eval_page(page, YOUTUBE_POSTERS_JS)
            except Exception:
                log.exception("could not replace YouTube embeds for %s", url)

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
                    screenshot_page(
                        page,
                        type="jpeg",
                        quality=80,
                        full_page=True,
                        animations="allow",
                        caret="hide",
                        scale="css",
                    ),
                    timeout=20,
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
            video_note: str | None = None
            if record_video and jpeg_frames:
                playback_fps = _playback_fps(len(jpeg_frames), t_first, t_last, video_fps)
                try:
                    ffmpeg = await _start_ffmpeg_pipe(
                        video_path, playback_fps, vf=ffmpeg_scale_filter(jpeg_frames)
                    )
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
                        video_note = "video encode failed"
                except Exception as exc:
                    log.exception("could not encode video for %s", url)
                    if ffmpeg is not None:
                        await _finish_ffmpeg_pipe(ffmpeg, ignore_errors=True)
                        ffmpeg = None
                    video_path.unlink(missing_ok=True)
                    video_note = f"video encode failed: {_exc_text(exc)[-300:]}"

            # Banners can appear during encode (WDS waits on window.load + 500ms).
            await try_cookies()

            height = 0
            height_capped = False
            notes: list[str] = []
            if video_note:
                notes.append(video_note)
            try:
                height = int(await eval_page(page, HEIGHT_JS) or 0)
                height_capped = height > max_height
                try:
                    await eval_page(page, FREEZE_STILL_JS)
                except Exception:
                    log.exception("could not freeze page for still %s", url)
                try:
                    still_capped = await asyncio.wait_for(
                        _screenshot(page, screenshot_path, height, max_height),
                        timeout=40,
                    )
                except Exception as exc:
                    still_capped = True
                    notes.append(f"still screenshot timed out: {_exc_text(exc)}")
                height_capped = bool(height_capped or still_capped)
                try:
                    outer = await eval_page(
                        page,
                        "() => document.documentElement ? document.documentElement.outerHTML : ''",
                    )
                    dom_path.write_text(outer or "", encoding="utf-8")
                except Exception as exc:
                    notes.append(f"DOM not saved: {exc}")
            except Exception as exc:
                notes.append(str(exc))

            result_shot = existing_output(screenshot_path)
            result_dom = existing_output(dom_path)
            result_video = existing_output(Path(result_video)) if result_video else None

            viewport_now = None
            try:
                viewport_now = page.viewport_size
            except Exception:
                pass
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
                "capture_viewport": viewport_now,
                "video_fps": round(playback_fps, 4) if result_video else None,
                "video": Path(result_video).name if result_video else None,
            }
            try:
                (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            except OSError:
                notes.append("meta.json not saved")

            if not stable:
                notes.append("page did not stay stable; captured anyway")
            if record_video and not result_video and not video_note:
                notes.append("video encode failed")
            if not result_shot:
                notes.append("PNG was not written")
            problems = [
                note
                for note in notes
                if note != "page did not stay stable; captured anyway"
            ]
            if not result_shot:
                result = CaptureResult(
                    status="failed",
                    reason="screenshot",
                    saved=False,
                    error="; ".join(problems) or "PNG was not written",
                    screenshot_path=None,
                    dom_path=result_dom,
                    video_path=result_video,
                    output_path=str(directory),
                    http_status=http_status,
                    height_px=height,
                    cookie_dismissed=cookie_dismissed,
                    height_capped=height_capped,
                    warnings=notes,
                )
            else:
                result = CaptureResult(
                    status="complete",
                    reason=None if stable else "unstable",
                    saved=True,
                    error="; ".join(problems) if problems else None,
                    screenshot_path=result_shot,
                    dom_path=result_dom,
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
        step = int(await eval_page(page, "() => window.innerHeight") or 900)
    except Exception:
        step = 900
    step = max(step, 1)
    y = 0
    last_height = 0
    while loop.time() < deadline:
        try:
            height = int(await eval_page(page, HEIGHT_JS) or 0)
        except Exception:
            break
        if height <= 0:
            break
        if y >= height and height == last_height:
            break
        last_height = height
        y = min(y + step, height)
        try:
            await eval_page(page, "(top) => window.scrollTo(0, top)", y)
        except Exception:
            break
        await page.wait_for_timeout(80)
    try:
        await eval_page(page, "() => window.scrollTo(0, 0)")
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
            signature = await asyncio.wait_for(
                eval_page(page, SIGNATURE_JS), timeout=min(4.0, remaining)
            )
        except Exception as exc:
            if _is_nav_loss(exc):
                await _settle(page)
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
    await eval_page(
        page,
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
        "caret": "hide",
        "scale": "css",
    }
    capped = page_height > max_height
    # Freeze CSS already paused motion; `disabled` can hang on Duda/video pages.
    for animations in ("allow", "disabled"):
        try:
            await asyncio.wait_for(
                screenshot_page(page, **kwargs, animations=animations),
                timeout=12,
            )
            return capped
        except Exception:
            continue
    kwargs["animations"] = "allow"
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
            await asyncio.wait_for(screenshot_page(page, **kwargs), timeout=8)
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


async def _start_ffmpeg_pipe(dest: Path, fps: float, vf: str | None = None):
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
        vf or "scale=trunc(iw/2)*2:trunc(ih/2)*2",
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
