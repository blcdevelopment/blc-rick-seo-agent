"""Live probe + session setup for the Semrush AI Visibility enrichment.

Two modes:

    # 1) Establish the saved login session ONCE (headed browser). Log in by hand — solve any
    #    CAPTCHA / 2FA — then press Enter here to persist the cookies. The bot reuses them after.
    python scripts/check_semrush_ai_visibility.py --login

    # 2) Run the REAL collection path for a domain (uses the saved session + OpenAI vision) and
    #    print the normalized facts + whether they render a report section.
    python scripts/check_semrush_ai_visibility.py builderleadconverter.com

All config (credentials, URLs, session path, headless) is read from ``.env`` via Settings; the
password is NEVER printed. Set ``AI_VISIBILITY_HEADLESS=false`` in ``.env`` to WATCH mode 2 drive
the browser (useful when tuning selectors). See docs/OPERATIONS.md §5 for the compliance note.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.shared.config import get_settings  # noqa: E402
from apps.worker.stages.ai_visibility.collector import collect_ai_visibility_facts  # noqa: E402
from apps.worker.stages.ai_visibility.report import build_ai_visibility_report_data  # noqa: E402
from apps.worker.stages.ai_visibility.semrush_scraper import _REALISTIC_UA  # noqa: E402


async def _looks_logged_in(page) -> bool:
    """True when Semrush shows a logged-IN view (no prominent 'Log in'/'Sign up' affordance).

    Guards against the classic mistake of pressing Enter before the login actually completes, which
    would persist a logged-OUT session (ad/tracking cookies only, no auth token) that fails silently
    on every audit.
    """
    try:
        await page.goto("https://www.semrush.com/dashboard/", wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle")
        url = (page.url or "").lower()
        if any(m in url for m in ("login", "signin", "sign-up", "signup")):
            return False
        for selector in (
            "a:has-text('Sign Up')",
            "button:has-text('Sign Up')",
            "a:has-text('Sign up')",
        ):
            if await page.locator(selector).count():
                return False
        return True
    except Exception:
        return False


async def _login_and_save() -> int:
    settings = get_settings()
    session_raw = (settings.semrush_session_state_path or "").strip()
    if not session_raw:
        print("SEMRUSH_SESSION_STATE_PATH is empty in .env — set it before saving a session.")
        return 1
    session_path = Path(session_raw)

    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=_REALISTIC_UA,
            viewport={"width": 1680, "height": 1050},
        )
        page = await context.new_page()
        await page.goto(settings.semrush_login_url, wait_until="domcontentloaded")
        print("\nA browser window opened at the Semrush login page.")
        print("Log in there by hand (solve any CAPTCHA / 2FA) until you see your dashboard.")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, input, "\nPress Enter here once you are fully logged in... "
        )
        # Verify we're ACTUALLY logged in before saving — otherwise we persist a logged-out session
        # (only marketing/tracking cookies) and every audit shows the "reconnect" note.
        print("[check] verifying you're logged in ...")
        if not await _looks_logged_in(page):
            print(
                "\n[!] You do NOT appear to be logged in — Semrush served a login/marketing page.\n"
                "    NOTHING was saved. Re-run --login and make sure you can SEE your Semrush\n"
                "    DASHBOARD (not the 'Log in / Sign up' page) BEFORE pressing Enter."
            )
            await browser.close()
            return 1
        session_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(session_path))
        await browser.close()
    print(f"\n[ok] Saved a LOGGED-IN Semrush session to {session_path}. The bot will reuse it.")
    return 0


def _run_extract(domain: str) -> int:
    settings = get_settings()
    if not settings.ai_visibility_enabled:
        print("AI_VISIBILITY_ENABLED is false in .env — set it true to run the collector.")
        return 1
    print(f"[semrush] collecting AI visibility for '{domain}' ... (opens a browser, then vision)")
    facts = collect_ai_visibility_facts(settings, domain=domain, retrieved_at="probe")
    print("\nnormalized facts:")
    print(json.dumps(facts, indent=2, default=str))

    status = facts.get("status")
    if status not in ("complete", "partial"):
        print(f"\nstatus={status} reason={facts.get('reason')} — no section would render.")
        print(
            "If this is a login/session issue, run: "
            "python scripts/check_semrush_ai_visibility.py --login"
        )
        return 0

    section = build_ai_visibility_report_data(facts)
    print(f"\nrenders a report section: {bool(section)}")
    if section:
        print(
            f"  visibility_score={section.get('visibility_score')} "
            f"metrics={len(section.get('metrics') or [])} "
            f"platforms={len(section.get('per_platform') or [])} "
            f"topics={len(section.get('topics') or [])} "
            f"competitors={len(section.get('competitors') or [])}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--login" in args:
        return asyncio.run(_login_and_save())

    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("usage: python scripts/check_semrush_ai_visibility.py [--login | <domain>]")
        return 1
    return _run_extract(positional[0].strip())


if __name__ == "__main__":
    raise SystemExit(main())
