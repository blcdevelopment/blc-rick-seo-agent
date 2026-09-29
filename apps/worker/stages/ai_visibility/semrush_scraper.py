"""Semrush AI Visibility login-bot (Playwright).

Signs into the operator's OWN Semrush account, opens the AI Visibility overview for a domain,
screenshots the dashboard, and hands the images to the vision extractor. Semrush publishes no API
for this toolkit, so this UI automation is the only programmatic route (see
``docs/OPERATIONS.md §5`` — the ToS/compliance decision is the
operator's, and this runs low-volume, on-demand, from a single server-side account).

Design for robustness + low detection surface:
- **Reuse a saved browser session** (``semrush_session_state_path``) rather than scripting the
  password every run. Establish it ONCE — ideally via ``scripts/check_semrush_ai_visibility.py
  --login`` in a headed browser so a human clears any CAPTCHA / 2FA — and the bot loads those
  cookies thereafter. An email+password login is attempted only when no session file exists.
- **Everything is wrapped** so any failure (login wall, CAPTCHA, UI change, timeout) returns
  ``None``: the collector then skips gracefully and the enrichment task restores its snapshot, so a
  failed scrape leaves the existing report byte-identical.

Selectors/URLs are best-effort defaults and may need tuning against the live site — they are
config-driven (``semrush_login_url`` / ``semrush_ai_visibility_url``) and the probe script is the
tool for tuning them interactively.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from apps.shared.config import Settings
from apps.worker.stages.ai_visibility.vision import extract_ai_visibility_from_images

JsonDict = dict[str, Any]

# A realistic desktop Chrome UA — the audit bot's UA would stand out on an authenticated session.
_REALISTIC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _session_path(settings: Settings) -> Path | None:
    raw = (settings.semrush_session_state_path or "").strip()
    return Path(raw) if raw else None


async def _launch(playwright: Any, settings: Settings) -> Any:
    headless = settings.ai_visibility_headless
    try:
        return await playwright.chromium.launch(headless=headless)
    except Exception:
        # Reuse the crawler's installed-Chromium fallback so a missing default shell still launches.
        from apps.worker.stages.crawler import _find_installed_chromium_executable

        fallback = _find_installed_chromium_executable()
        if fallback is None:
            raise
        return await playwright.chromium.launch(headless=headless, executable_path=str(fallback))


def _has_credentials(settings: Settings) -> bool:
    """True when both an email and a password are configured for a credential login."""
    password = settings.semrush_password.get_secret_value() if settings.semrush_password else ""
    return bool((settings.semrush_email or "").strip() and password)


async def _login(page: Any, settings: Settings) -> None:
    """Best-effort email+password login. Tolerant of Semrush's multi-step form; raises on failure.

    Prefer establishing the session manually (probe ``--login``) — automated login is the fallback
    for a fully headless refresh and is the most fragile part (CAPTCHA/2FA can block it).
    """
    email = (settings.semrush_email or "").strip()
    password = settings.semrush_password.get_secret_value() if settings.semrush_password else ""
    if not email or not password:
        raise ValueError("No saved session and no Semrush email/password to log in with.")

    await page.goto(settings.semrush_login_url, wait_until="domcontentloaded")

    # Email — try the common shapes, then an optional "continue" step, then password.
    for selector in ("input[type='email']", "input[name='email']", "#email"):
        field = page.locator(selector)
        if await field.count():
            await field.first.fill(email)
            break

    for selector in (
        "button[type='submit']",
        "button:has-text('Continue')",
        "button:has-text('Log in')",
    ):
        button = page.locator(selector)
        if await button.count():
            with contextlib.suppress(Exception):
                await button.first.click()
            break

    for selector in ("input[type='password']", "input[name='password']", "#password"):
        field = page.locator(selector)
        if await field.count():
            await field.first.fill(password)
            break

    for selector in (
        "button[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
    ):
        button = page.locator(selector)
        if await button.count():
            with contextlib.suppress(Exception):
                await button.first.click()
            break

    await page.wait_for_load_state("networkidle")


async def _open_dashboard(page: Any, domain: str, settings: Settings) -> None:
    """Open the AI Visibility report for ``domain`` directly via its ``?q=`` report URL.

    Navigating straight to the domain-parameterised URL (e.g. ``.../ai-seo/overview/?q=example.com``
    — the exact URL Semrush uses when you view a report) is far more reliable than typing into the
    intro page's domain box, which left the report un-run and screenshotted only the intro page.
    """
    from urllib.parse import quote

    base = settings.semrush_ai_visibility_url
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}q={quote(domain)}"
    await page.goto(url, wait_until="domcontentloaded")
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle")
    # The gauge/tables render asynchronously after load. Wait for the "/100" score marker up to the
    # configured ceiling: on success we proceed as soon as it paints (no wasted fixed sleep); if the
    # marker never appears we've still blocked the full ceiling (same floor as before) before the
    # screenshot, so a UI change can't under-wait. The vision step handles a still-empty page.
    ceiling_ms = int(settings.ai_visibility_render_wait_seconds * 1000)
    # Guard: Playwright treats timeout=0 as "wait forever", and the config permits render_wait=0.
    if ceiling_ms > 0:
        with contextlib.suppress(Exception):
            await page.wait_for_selector(r"text=/\/\s*100/", timeout=ceiling_ms)
    # The gauge (top of page) can paint before the lazily client-rendered lower tables (topics /
    # by-country); a brief settle after it appears lets them finish before the screenshot. Still
    # far faster than the old flat 10s wait, which always ran to completion.
    with contextlib.suppress(Exception):
        await page.wait_for_timeout(2000)


async def _detect_block(page: Any) -> str | None:
    """Best-effort detection of a CAPTCHA / logged-out wall standing between us and the dashboard.

    Returns ``"captcha"`` for a VISIBLE human-verification challenge, ``"no_session"`` when the page
    is Semrush's logged-out marketing/login wall (expired/invalid session), else ``None``.

    Deliberately CONSERVATIVE to avoid false positives (a false positive discards a good scrape and
    shows a bogus note; a false negative just lets the vision step return empty). So:
    - CAPTCHA is detected only by human-facing CHALLENGE TEXT — NOT by the mere presence of a
      reCAPTCHA/hCaptcha script or badge, since Semrush (like most SaaS) loads invisible reCAPTCHA
      v3 site-wide even on a perfectly-authenticated dashboard.
    - Logged-out is detected only when BOTH a "Log in" AND a "Sign Up" affordance are present (the
      top-nav pair a signed-in dashboard never shows) — not a lone "Sign up" upsell CTA, which valid
      dashboards do render — plus a hard login/auth URL fallback."""
    try:
        # Query visible challenge TEXT via locators (case-insensitive substring) instead of
        # serializing + lowercasing the whole multi-MB DOM; this also only matches rendered text,
        # not a phrase buried in a script/comment.
        for phrase in (
            "verify you are human",
            "confirm you are human",
            "please verify you're a human",
            "i'm not a robot",
            "unusual traffic",
        ):
            if await page.get_by_text(phrase).count():
                return "captcha"
        # Logged-OUT wall: require BOTH "Log in" and "Sign Up" affordances (only shown together on
        # the marketing/login page; a signed-in dashboard shows the account menu instead).
        has_login = False
        for sel in ("a:has-text('Log in')", "button:has-text('Log in')", "a:has-text('Sign in')"):
            if await page.locator(sel).count():
                has_login = True
                break
        has_signup = False
        for sel in ("a:has-text('Sign Up')", "button:has-text('Sign Up')", "a:has-text('Sign up')"):
            if await page.locator(sel).count():
                has_signup = True
                break
        if has_login and has_signup:
            return "no_session"
        url = (page.url or "").lower()
        # "multilogin" is Semrush's "account used in several places" interstitial — the cookie is
        # technically valid but parked before the dashboard, so a scrape reads nothing. It slipped
        # past the "/login" substring (no leading slash in "/multilogin/"); match it explicitly.
        if any(
            marker in url for marker in ("/login", "signin", "/auth", "challenge", "multilogin")
        ):
            return "no_session"
    except Exception:
        return None
    return None


async def _capture(page: Any, settings: Settings, domain: str) -> list[Path]:
    """Full-page screenshot of the rendered dashboard. Returns the saved image paths.

    The filename carries a per-run suffix so two concurrent audits of the SAME domain can't
    overwrite each other's screenshot between capture and the vision read (retention prunes it)."""
    safe = "".join(c if c.isalnum() else "_" for c in domain) or "domain"
    folder = settings.local_screenshot_storage_dir / "semrush_ai_visibility"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe}-{uuid4().hex[:8]}.png"
    await page.screenshot(path=str(path), full_page=True)
    return [path]


async def fetch_semrush_ai_visibility(domain: str, settings: Settings) -> JsonDict | None:
    """Log in (or reuse a session), open the AI Visibility dashboard for ``domain``, and extract it.

    Returns the extraction as a plain dict on success, ``{"__blocked__": <reason>}`` when a CAPTCHA
    / login wall stopped us (so the report can show an honest "could not retrieve"
    note instead of silently omitting the section), or ``None`` on any other failure.
    """
    from playwright.async_api import async_playwright

    session_path = _session_path(settings)
    has_session = bool(session_path and session_path.is_file())

    # SAFETY: with no saved session and auto-login OFF (the default), do NOT attempt a fresh
    # credential login. A repeated headless login is what trips the CAPTCHA and risks flagging the
    # account — so instead tell the report the session must be established once (--login /
    # `make semrush-connect`). We don't even launch a browser here.
    if not has_session and not settings.semrush_allow_headless_login:
        return {"__blocked__": "no_session"}

    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await _launch(playwright, settings)
            context_kwargs: JsonDict = {
                "user_agent": _REALISTIC_UA,
                "viewport": {"width": 1680, "height": 1050},
                "ignore_https_errors": True,
            }
            if has_session:
                context_kwargs["storage_state"] = str(session_path)
            context = await browser.new_context(**context_kwargs)
            context.set_default_timeout(settings.ai_visibility_timeout_seconds * 1000)
            context.set_default_navigation_timeout(settings.ai_visibility_timeout_seconds * 1000)
            page = await context.new_page()

            if not has_session:
                # Only reached when semrush_allow_headless_login is explicitly True (power users who
                # accept the account risk). Attempt the credential login, then VERIFY it actually
                # landed us logged in before persisting — otherwise a CAPTCHA/failed login saves a
                # logged-out session that blocks (and is never retried by) every future audit.
                await _login(page, settings)
                blocked = await _detect_block(page)
                if blocked:
                    return {"__blocked__": blocked}
                if session_path is not None:
                    session_path.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(session_path))

            await _open_dashboard(page, domain, settings)
            blocked = await _detect_block(page)

            # A STALE saved session (expired, or invalidated because someone signed into the same
            # account elsewhere — Semrush drops other sessions on a new login) lands here as
            # "no_session". Previously the credential fallback only ran when the file was ABSENT, so
            # a dead session file meant every future run failed until a human re-minted it. When the
            # operator has opted into headless login, retry it once here. A CAPTCHA still aborts.
            if (
                blocked == "no_session"
                and settings.semrush_allow_headless_login
                and _has_credentials(settings)
            ):
                await _login(page, settings)
                blocked = await _detect_block(page)
                if not blocked:
                    await _open_dashboard(page, domain, settings)
                    blocked = await _detect_block(page)

            if blocked:
                # Save what we saw for debugging, then report the block so the report can note it.
                with contextlib.suppress(Exception):
                    await _capture(page, settings, domain)
                return {"__blocked__": blocked}
            screenshots = await _capture(page, settings, domain)

            # Re-persist the session on every SUCCESSFUL dashboard load. Semrush rotates its auth
            # cookies and issues fresh expiries as you browse; writing the state back means each run
            # extends the session's life instead of aging out the one-time snapshot taken at connect
            # time. Only reached past `_detect_block`, so we never overwrite a good session with a
            # logged-out one. Best-effort: a write failure must not lose the scrape we just did.
            if session_path is not None:
                with contextlib.suppress(Exception):
                    session_path.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(session_path))

            extraction = extract_ai_visibility_from_images(
                screenshots, domain=domain, settings=settings
            )
            # Extraction succeeded — drop the (large) screenshot so audit-path artifacts don't
            # accumulate on disk. On failure we fall through to `except` and KEEP the file so a
            # vision/parse error is diagnosable; the block-path capture is likewise kept.
            for shot in screenshots:
                with contextlib.suppress(Exception):
                    Path(shot).unlink()
            return extraction.model_dump()
    except Exception:
        # UI drift, timeout, vision error — degrade to None (the collector marks it unavailable).
        return None
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()


def fetch_semrush_ai_visibility_sync(*, domain: str, settings: Settings) -> JsonDict | None:
    """Sync wrapper (Celery tasks are sync), mirroring ``crawler.crawl_site_sync``."""
    return asyncio.run(fetch_semrush_ai_visibility(domain, settings))
