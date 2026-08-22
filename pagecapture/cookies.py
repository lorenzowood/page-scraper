from __future__ import annotations

import re

from playwright.async_api import Page

ACCEPT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#accept-recommended-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#didomi-notice-agree-button",
    ".qc-cmp2-summary-buttons button[mode='primary']",
    ".osano-cm-accept-all",
    ".cky-btn-accept",
    "button[data-testid='uc-accept-all-button']",
    ".iubenda-cs-accept-btn",
    "#truste-consent-button",
    ".truste-consent-button",
    "[data-cookiefirst-action='accept']",
    ".cmplz-accept",
    ".cc-accept",
    ".cc-allow",
    "#cookie-notice-optin-btn",
    "button#accept-cookies",
    "#cookie-accept-submit",
    "#allow-cookies",
    "button[id*='accept-all' i]",
    "button[class*='accept-all' i]",
]

# Visible label (inner text). Tight phrases so we do not click "Cookie policy".
VISIBLE_LABEL = re.compile(
    r"(allow all|accept all|reject all|"
    r"accept additional cookies|reject additional cookies|"
    r"allow required|allow necessary|allow cookies|accept cookies|reject cookies|"
    r"only necessary|only required|necessary cookies only)",
    re.I,
)

# Accessible name / aria-label, e.g. YouTube "Accept the use of cookies and other data…"
ROLE_NAME = re.compile(
    r"(accept|allow|reject|decline).{0,60}(all|cookies|required|necessary|additional)",
    re.I,
)

SKIP_LABEL = re.compile(
    r"(view cookies|manage cookies|cookie (policy|settings|preferences)|"
    r"customise|customize|learn more|more options|see details)",
    re.I,
)

PREFER_ALLOW = re.compile(r"accept|allow", re.I)

BUTTON_SEL = "button, [role='button'], input[type='button'], input[type='submit']"

OVERLAY_SELECTORS = [
    "#onetrust-banner-sdk",
    "#CybotCookiebotDialog",
    "#didomi-notice",
    "#qc-cmp2-container",
    ".osano-cm-window",
    ".cky-consent-container",
    "#global-cookie-message",
    # WDS-style bars: injected after window.load, "Allow all" reloads the page.
    ".cookie-policy",
]


async def dismiss_cookie_banners(page: Page) -> bool:
    dismissed = False
    # Hide first so we do not click handlers that navigate (WDS Allow all → reload).
    if await _hide_known_overlays(page):
        dismissed = True
    for _ in range(2):
        if await _click_visible_label(page) or await _click_by_role_name(page) or await _click_known(page):
            dismissed = True
            await page.wait_for_timeout(350)
            continue
        break
    if await _hide_known_overlays(page):
        dismissed = True
    return dismissed


async def _click_visible_label(page: Page) -> bool:
    """Match the text the user sees, not aria-label (YouTube, GOV.UK, custom bars)."""
    allow: list = []
    reject: list = []
    for frame in page.frames:
        locator = frame.locator(BUTTON_SEL).filter(has_text=VISIBLE_LABEL)
        try:
            count = await locator.count()
        except Exception:
            continue
        for i in range(min(count, 12)):
            el = locator.nth(i)
            try:
                if not await el.is_visible(timeout=150):
                    continue
                text = (await el.inner_text(timeout=150) or "").strip()
                if not text or SKIP_LABEL.search(text):
                    continue
                (allow if PREFER_ALLOW.search(text) else reject).append(el)
            except Exception:
                continue
    for el in allow or reject:
        try:
            await el.click(timeout=800, force=True, no_wait_after=True)
            return True
        except Exception:
            continue
    return False


async def _click_by_role_name(page: Page) -> bool:
    allow: list = []
    reject: list = []
    for frame in page.frames:
        for role in ("button", "link"):
            try:
                loc = frame.get_by_role(role, name=ROLE_NAME)
                count = await loc.count()
            except Exception:
                continue
            for i in range(min(count, 8)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible(timeout=150):
                        continue
                    name = (await el.get_attribute("aria-label") or await el.inner_text() or "")
                    if SKIP_LABEL.search(name):
                        continue
                    (allow if PREFER_ALLOW.search(name) else reject).append(el)
                except Exception:
                    continue
    for el in allow or reject:
        try:
            await el.click(timeout=800, force=True, no_wait_after=True)
            return True
        except Exception:
            continue
    return False


async def _click_known(page: Page) -> bool:
    for frame in page.frames:
        for selector in ACCEPT_SELECTORS:
            locator = frame.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                if await locator.is_visible(timeout=200):
                    await locator.click(timeout=800, force=True, no_wait_after=True)
                    return True
            except Exception:
                continue
    return False


async def _hide_known_overlays(page: Page) -> bool:
    hidden = await page.evaluate(
        """(selectors) => {
            const hide = (el) => {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('visibility', 'hidden', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
            };
            let n = 0;
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach((el) => {
                    hide(el);
                    n += 1;
                });
            }
            const blockers = document.querySelectorAll(
                '[class*="cookie"][class*="overlay"], [id*="cookie"][id*="overlay"], [class*="cookie"][class*="banner"]'
            );
            blockers.forEach((el) => {
                hide(el);
                n += 1;
            });
            const action = /(allow all|accept all|accept cookies|allow cookies|allow required|reject all)/i;
            document.querySelectorAll('div, aside, section, form').forEach((el) => {
                const s = getComputedStyle(el);
                if (s.position !== 'fixed' && s.position !== 'sticky') return;
                if (s.display === 'none' || s.visibility === 'hidden') return;
                const t = (el.innerText || '').trim();
                if (t.length < 12 || t.length > 1500) return;
                if (!/cookie/i.test(t) || !action.test(t)) return;
                hide(el);
                n += 1;
            });
            return n > 0;
        }""",
        OVERLAY_SELECTORS,
    )
    return bool(hidden)
