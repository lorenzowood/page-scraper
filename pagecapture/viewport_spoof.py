from __future__ import annotations

import re
from typing import Any

from playwright.async_api import BrowserContext, Page, Route

_UNIT = re.compile(r"(-?[\d.]+)(dvh|svh|lvh|vh|vmin|vmax)\b", re.I)

# Chromium screenshot/compositor limit on a side; stay under it when stretching the window.
CHROME_MAX_EDGE = 16_384


def rewrite_viewport_units(css: str, vw: int, vh: int) -> str:
    """Replace vh-like units with px so a later tall window does not inflate 100vh heroes."""
    vmin = min(vw, vh)
    vmax = max(vw, vh)

    def repl(match: re.Match[str]) -> str:
        n = float(match.group(1))
        unit = match.group(2).lower()
        if unit in {"vh", "dvh", "svh", "lvh"}:
            px = n / 100.0 * vh
        elif unit == "vmin":
            px = n / 100.0 * vmin
        else:
            px = n / 100.0 * vmax
        return f"{px:.4f}px"

    return _UNIT.sub(repl, css)


def window_spoof_script(width: int, height: int) -> str:
    return f"""(() => {{
  const W = {int(width)};
  const H = {int(height)};
  const spoof = (obj, prop, value) => {{
    if (!obj) return;
    try {{
      Object.defineProperty(obj, prop, {{
        get: () => value,
        configurable: true,
      }});
    }} catch (e) {{}}
  }};
  spoof(window, "innerWidth", W);
  spoof(window, "innerHeight", H);
  spoof(window, "outerWidth", W);
  spoof(window, "outerHeight", H);
  spoof(screen, "width", W);
  spoof(screen, "height", H);
  spoof(screen, "availWidth", W);
  spoof(screen, "availHeight", H);
  const docEl = document.documentElement;
  spoof(docEl, "clientWidth", W);
  spoof(docEl, "clientHeight", H);
  if (window.visualViewport) {{
    spoof(window.visualViewport, "width", W);
    spoof(window.visualViewport, "height", H);
  }}
  const vmin = Math.min(W, H);
  const vmax = Math.max(W, H);
  const rewrite = (text) => {{
    if (!text) return text;
    return String(text).replace(
      /(-?[\\d.]+)(dvh|svh|lvh|vh|vmin|vmax)\\b/gi,
      (_, n, unit) => {{
        const v = parseFloat(n);
        const u = unit.toLowerCase();
        let px = 0;
        if (u === "vmin") px = v / 100 * vmin;
        else if (u === "vmax") px = v / 100 * vmax;
        else px = v / 100 * H;
        return px.toFixed(4) + "px";
      }}
    );
  }};
  const origMatchMedia = window.matchMedia.bind(window);
  window.matchMedia = (query) => origMatchMedia(rewrite(query));
  const rewriteEl = (el) => {{
    if (!el || el.nodeType !== 1) return;
    if (el.tagName === "STYLE" && el.textContent) {{
      const next = rewrite(el.textContent);
      if (next !== el.textContent) el.textContent = next;
    }}
    const style = el.getAttribute && el.getAttribute("style");
    if (style) {{
      const next = rewrite(style);
      if (next !== style) el.setAttribute("style", next);
    }}
  }};
  const start = () => {{
    document.querySelectorAll("style, [style]").forEach(rewriteEl);
    const obs = new MutationObserver((muts) => {{
      for (const mut of muts) {{
        if (mut.type === "attributes" && mut.attributeName === "style") {{
          rewriteEl(mut.target);
        }}
        mut.addedNodes && mut.addedNodes.forEach((node) => {{
          if (node.nodeType !== 1) return;
          rewriteEl(node);
          node.querySelectorAll && node.querySelectorAll("style, [style]").forEach(rewriteEl);
        }});
      }}
    }});
    obs.observe(document.documentElement, {{
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["style"],
    }});
  }};
  if (document.documentElement) start();
  else document.addEventListener("DOMContentLoaded", start, {{ once: true }});
}})();"""


async def install_reported_window(context: BrowserContext, width: int, height: int) -> None:
    await context.add_init_script(window_spoof_script(width, height))


async def attach_css_vh_rewriter(context: BrowserContext, width: int, height: int) -> None:
    async def handle(route: Route) -> None:
        if route.request.resource_type not in {"stylesheet"}:
            await route.fallback()
            return
        try:
            response = await route.fetch(timeout=10_000)
            body = rewrite_viewport_units(await response.text(), width, height)
            await route.fulfill(response=response, body=body)
        except Exception:
            try:
                await route.fallback()
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass

    await context.route("**/*.css", handle)


async def rewrite_inline_viewport_units(page: Page, width: int, height: int) -> None:
    try:
        await page.evaluate(
            """({vw, vh}) => {
              const vmin = Math.min(vw, vh);
              const vmax = Math.max(vw, vh);
              const rewrite = (text) => {
                if (!text) return text;
                return String(text).replace(
                  /(-?[\\d.]+)(dvh|svh|lvh|vh|vmin|vmax)\\b/gi,
                  (_, n, unit) => {
                    const v = parseFloat(n);
                    const u = unit.toLowerCase();
                    let px = 0;
                    if (u === "vmin") px = v / 100 * vmin;
                    else if (u === "vmax") px = v / 100 * vmax;
                    else px = v / 100 * vh;
                    return px.toFixed(4) + "px";
                  }
                );
              };
              document.querySelectorAll("style").forEach((el) => {
                el.textContent = rewrite(el.textContent);
              });
              document.querySelectorAll("[style]").forEach((el) => {
                el.setAttribute("style", rewrite(el.getAttribute("style")));
              });
            }""",
            {"vw": int(width), "vh": int(height)},
        )
    except Exception:
        pass


async def expand_window_to_document(
    page: Page,
    *,
    width: int,
    window_height: int,
    max_height: int,
) -> tuple[int, bool]:
    """Stretch the real window to the document height. Reported innerHeight stays spoofed."""
    cap = min(int(max_height), CHROME_MAX_EDGE)
    last = -1
    target = int(window_height)
    for _ in range(12):
        try:
            doc_h = int(await page.evaluate(
                """() => Math.max(
                  document.documentElement ? document.documentElement.scrollHeight : 0,
                  document.body ? document.body.scrollHeight : 0,
                  0
                )"""
            ) or 0)
        except Exception:
            break
        target = _even(min(max(doc_h, int(window_height)), cap))
        current = (page.viewport_size or {}).get("height")
        if current != target:
            try:
                await page.set_viewport_size({"width": int(width), "height": target})
            except Exception:
                break
        elif abs(doc_h - last) < 4:
            last = doc_h
            break
        last = doc_h
        await page.wait_for_timeout(200)
    return target, last > cap


def _even(value: Any) -> int:
    n = int(value)
    return n if n % 2 == 0 else n + 1
