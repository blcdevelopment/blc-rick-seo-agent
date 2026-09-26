"""Built-in site health sweep — the Docker-friendly technical crawl.

Produces the same ``technical_crawl`` fact shape as the optional Screaming Frog
CLI collector (see ``technical_crawl_common.py``) without any licensed desktop
tool, JVM, or GUI dependency, so the Technical SEO section of the report works
in the production Docker stack.

What it does, all deterministically given the site's state:

1. On-page checks over the pages the Playwright crawler already rendered
   (missing/duplicate titles and meta descriptions, missing H1s, missing
   canonicals, images without alt text, noindex pages) — computed from the
   stored ``seo_facts``; no new fetching.
2. A status-code sweep (plain HTTP HEAD/GET, no rendering) over every URL the
   crawl discovered — internal links, links found on all rendered pages, and
   the XML sitemap — recording 4xx/5xx/unreachable URLs with examples.

Every number in the output is a count of concrete URLs the sweep actually
checked; example URLs are stored alongside each count so the report can show
where each problem lives. SSRF guards from the crawler are applied to every
URL before it is requested.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from celery.exceptions import SoftTimeLimitExceeded

from apps.shared.config import Settings
from apps.worker.stages.crawler import (
    _hostname_is_private,
    _ip_is_blocked,
    _resolve_host_ips,
    _site_host,
    is_same_site,
    normalize_url,
)
from apps.worker.stages.technical_crawl_common import (
    empty_summary,
    issues_from_summary,
    looks_like_post,
)

JsonDict = dict[str, Any]

SITE_HEALTH_SOURCE = "site_health_sweep"
_RETRY_GET_STATUSES = {403, 405, 406, 501}
# 429 is a rate-limit signal, handled in two steps: the checker first honors Retry-After and
# retries the SAME method a bounded number of times, and only when the HEAD still comes back
# 429 does it fall back to one GET — some hosts/CDNs rate-limit HEAD specifically while
# serving GET fine, and without the fallback their healthy pages would be counted as
# client errors (scored as broken internal URLs).
_RATE_LIMIT_STATUS = 429
_MAX_RATE_LIMIT_RETRIES = 2
_RETRY_AFTER_CAP_SECONDS = 30
# Response fingerprints that indicate a WAF/bot-management layer answered instead of
# the origin — used only to explain failures honestly, never to bypass anything.
_WAF_HEADER_MARKERS = ("cf-ray", "cf-mitigated")
_WAF_SERVER_TOKENS = ("cloudflare", "akamai", "sucuri", "imperva", "incapsula")
# One diagnostic recheck with a regular browser profile distinguishes "the site blocks
# automated checkers" from "the site is down" before the report says anything.
_BROWSER_RECHECK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Outbound links are checked by a server-side bot, which cannot distinguish a
# genuinely broken external link from a host that simply blocks automated
# traffic. Big platforms (Spotify, social networks, anything behind Cloudflare)
# routinely return 401/403/405/406/408/409/421/429/451 to a bot while serving a
# real browser a 200, so those statuses are INCONCLUSIVE and must never be
# reported as a broken link. Only the unambiguous "this page is gone" codes
# count as a broken outbound link; everything else is dropped rather than
# over-claimed. (Internal URLs are the client's own site and stay reliable, so
# every 4xx there is still surfaced.)
_BROKEN_EXTERNAL_CLIENT_STATUSES = {404, 410}
_MAX_REDIRECTS = 10

# The blog-post heuristic (looks_like_post) lives in technical_crawl_common so both
# technical-crawl collectors count posts identically; re-exported here for the tests.
_looks_like_post = looks_like_post


class _BlockedHost(RuntimeError):
    """Raised when a request URL or redirect target fails the SSRF host guard."""


class _DeadlineExceeded(RuntimeError):
    """Raised when the sweep's time budget expires while a check waits for a global slot.

    The per-host/global semaphore split made the pre-request budget test admission-only
    (distinct-host checks all pass it at t~0 under uncontended host lanes, then queue on the
    global slot) — this exception restores enforcement at the moment a request would actually
    go out, and the caller tallies it as ``skipped_budget``."""


def _error_class(exc: Exception) -> str:
    """Coarse transport-failure class, retained so 'did not respond' is explainable."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirect_loop"
    text = str(exc).lower()
    if isinstance(exc, httpx.ConnectError):
        return (
            "tls"
            if "ssl" in text or "tls" in text or "certificate" in text
            else "connection_failed"
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol"
    return "network"


def _looks_waf(response: httpx.Response) -> bool:
    headers = response.headers
    if any(marker in headers for marker in _WAF_HEADER_MARKERS):
        return True
    server = (headers.get("server") or "").lower()
    return any(token in server for token in _WAF_SERVER_TOKENS)


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    raw = (response.headers.get("retry-after") or "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        seconds = float(2**attempt)
    return max(0.0, min(seconds, _RETRY_AFTER_CAP_SECONDS))


async def _browser_ua_recheck(url: str | None, settings: Settings) -> bool:
    """One best-effort GET with a regular browser profile. True = the URL answered, so
    the earlier failures were the server filtering automated traffic, not a dead page.

    ``follow_redirects`` stays False: the sample URL's host was SSRF-vetted at sweep
    time, but a redirect target is attacker-controlled (an open redirect to a private
    or cloud-metadata host) — and any response, 3xx included, already proves the server
    answers a browser, which is all this diagnostic needs.
    """
    if not url:
        return False
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            verify=False,
            headers={
                "User-Agent": _BROWSER_RECHECK_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=settings.site_health_request_timeout_seconds,
        ) as client:
            response = await client.get(url)
        return response.status_code < 500
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return False


def collect_site_health_facts(
    *,
    url: str,
    seo_facts: JsonDict,
    crawled_pages: JsonDict,
    rendered_pages: Sequence[Any] | None,
    settings: Settings,
    deadline: float | None = None,
) -> JsonDict:
    started_at = _utc_now()
    if not settings.site_health_enabled:
        return _skipped("disabled", started_at)

    try:
        return asyncio.run(
            _collect(
                url=url,
                seo_facts=seo_facts,
                crawled_pages=crawled_pages,
                rendered_pages=rendered_pages,
                settings=settings,
                started_at=started_at,
                deadline=deadline,
            )
        )
    except SoftTimeLimitExceeded:
        # Celery's soft limit means the whole task is out of budget — the audit
        # must fail fast, not record a "failed sweep" and keep going.
        raise
    except Exception as exc:
        return _failed(_trim(str(exc)), started_at)


async def _collect(
    *,
    url: str,
    seo_facts: JsonDict,
    crawled_pages: JsonDict,
    rendered_pages: Sequence[Any] | None,
    settings: Settings,
    started_at: str,
    deadline: float | None = None,
) -> JsonDict:
    site_url = str(crawled_pages.get("final_url") or url)
    summary = empty_summary()
    examples: dict[str, list[str]] = defaultdict(list)
    notes: list[str] = []

    pages = [page for page in _list(seo_facts.get("pages")) if isinstance(page, dict)]
    _apply_on_page_checks(pages, summary, examples)
    summary["urls_crawled"] = len(pages)
    summary["html_urls_crawled"] = len(pages)

    rendered_urls = _rendered_urls(crawled_pages, rendered_pages)
    internal_urls, external_urls, link_notes = _link_inventory(
        site_url=site_url,
        crawled_pages=crawled_pages,
        rendered_pages=rendered_pages,
        settings=settings,
    )
    notes.extend(link_notes)

    host_allowed_cache: dict[str, bool] = {}
    async with httpx.AsyncClient(
        follow_redirects=False,
        verify=False,
        headers={
            "User-Agent": settings.crawler_user_agent,
            # Send the headers a real browser sends so hosts that vary their
            # response on them (and bot-wall a bare request) answer truthfully.
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=settings.site_health_request_timeout_seconds,
    ) as client:
        # The sitemap phase runs BEFORE the sweep's own clock starts, so it must respect the
        # whole-audit deadline itself: a hanging sitemap host (index + children, each with its
        # own timeout and redirect chain) could otherwise overrun the audit before a single
        # link was checked.
        if deadline is not None and time.monotonic() > deadline:
            sitemap_urls, sitemap_status = [], "skipped"
        else:
            sitemap_urls, sitemap_status = await _sitemap_urls(
                client, site_url, settings, host_allowed_cache, deadline=deadline
            )

        internal_urls = sorted(
            (set(internal_urls) | set(sitemap_urls)) - rendered_urls,
        )
        # Website-scope counts ("what the whole site consists of") — captured over ALL discovered
        # internal pages (rendered + link/sitemap-discovered) BEFORE the coverage cap truncates the
        # sweep list, so the report can state the true site size, not just what was checked.
        # Rendered pages contribute ONE entry each (their final URL) — `rendered_urls` keeps both
        # the pre- and post-redirect forms for sweep exclusion, but counting both would
        # double-count every page reached via a redirect (http->https, apex->www).
        all_internal_urls = set(internal_urls) | _rendered_final_urls(crawled_pages, rendered_pages)
        discovered_internal_urls = len(all_internal_urls)
        discovered_blog_posts = sum(1 for url in all_internal_urls if _looks_like_post(url))
        if len(internal_urls) > settings.site_health_max_internal_urls:
            notes.append(
                f"Checked the first {settings.site_health_max_internal_urls} of "
                f"{len(internal_urls)} discovered internal URLs."
            )
            internal_urls = internal_urls[: settings.site_health_max_internal_urls]

        external_urls = sorted(set(external_urls))
        discovered_external_urls = len(external_urls)
        if not settings.site_health_check_external_links:
            external_urls = []
        elif len(external_urls) > settings.site_health_max_external_urls:
            notes.append(
                f"Checked the first {settings.site_health_max_external_urls} of "
                f"{len(external_urls)} outbound links."
            )
            external_urls = external_urls[: settings.site_health_max_external_urls]

        checks = await _sweep(
            client,
            internal_urls=internal_urls,
            external_urls=external_urls,
            settings=settings,
            summary=summary,
            examples=examples,
            notes=notes,
            host_allowed_cache=host_allowed_cache,
            global_deadline=deadline,
        )

    summary["sitemap_url_count"] = len(sitemap_urls)
    summary["internal_urls_checked"] = checks["internal_checked"]
    summary["external_urls_checked"] = checks["external_checked"]
    summary["urls_checked"] = checks["internal_checked"] + checks["external_checked"]
    # Website-scope totals (site size), independent of how many were checked.
    summary["discovered_internal_urls"] = discovered_internal_urls
    summary["discovered_blog_posts"] = discovered_blog_posts
    summary["discovered_external_urls"] = discovered_external_urls
    if checks.get("error_classes"):
        # Retained so "did not respond" is diagnosable (timeout vs reset vs TLS).
        summary["unreachable_error_classes"] = checks["error_classes"]

    # Sweep results arrive in network-completion order; sort the example URLs so
    # the same site state renders the same report.
    for key in examples:
        examples[key].sort()

    # A tripped breaker means the host stopped answering this checker: the link results
    # are incomplete and must not be scored or rendered as broken-link findings. The
    # non-complete status makes the scoring trust gate strip the summary and the report
    # fall back to the honest "not available" framing with the explanatory note.
    #
    # The SAME honesty applies to every other way the sweep can end up without real coverage,
    # because "complete" is what lets the trust gate score these link facts as a full, clean
    # pass. Three more gates, each measured against `internal_conclusive` — the internal URLs
    # that actually ANSWERED (not throttled, not a transport failure):
    #   * widespread throttling — 429s never trip the transport-failure breaker, so they get
    #     their own gate (and a throttled URL is not evidence of a broken page);
    #   * NO usable coverage at all — a small site where every URL was throttled or refused
    #     stays under the absolute breaker threshold, yet scoring its zero-coverage summary as
    #     clean is exactly the false all-clear the breaker exists to prevent;
    #   * a budget-truncated sweep — it only saw part of the site, so its counts are a floor,
    #     not a verdict.
    bot_blocked = bool(checks.get("bot_blocked"))
    rate_limited = int(checks.get("rate_limited_internal") or 0)
    conclusive = int(checks.get("internal_conclusive") or 0)
    unreachable = int(summary.get("unreachable_internal_urls") or 0)
    skipped_budget = int(checks.get("skipped_budget") or 0)
    # Throttling DOMINATES what answered (strictly — a 50/50 split on a two-URL site is not
    # "widespread", and downgrading there would strip a real finding from the one URL that did
    # answer). The all-throttled case needs no majority: `no_coverage` below catches it.
    rate_limited_widely = rate_limited >= settings.site_health_breaker_threshold or (
        rate_limited > 0 and rate_limited > conclusive
    )
    no_coverage = conclusive == 0 and (rate_limited + unreachable) > 0
    truncated = skipped_budget > 0
    result: JsonDict = {
        "status": (
            "partial"
            if (bot_blocked or rate_limited_widely or no_coverage or truncated)
            else "complete"
        ),
        "source": SITE_HEALTH_SOURCE,
        "summary": summary,
        "issues": issues_from_summary(summary, examples, source=SITE_HEALTH_SOURCE),
        "checks": {
            "sitemap_status": sitemap_status,
            "internal_urls_checked": checks["internal_checked"],
            "internal_urls_answered": conclusive,
            "external_urls_checked": checks["external_checked"],
            "inconclusive_external_urls": checks["inconclusive_external"],
            "rate_limited_internal_urls": checks.get("rate_limited_internal", 0),
            "blocked_urls": checks["blocked"],
            "rendered_pages_excluded": len(rendered_urls),
            "waf_signals": checks.get("waf_signals", 0),
            "skipped_breaker": checks.get("skipped_breaker", 0),
            "skipped_budget": skipped_budget,
            "browser_recheck_ok": checks.get("browser_recheck_ok"),
        },
        "notes": notes,
        "files": [],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }
    # Name the DOMINANT cause of the missing coverage — each reason maps to its own
    # client-facing explanation (report_payload.SKIP_REASON_LABELS), so a host that simply
    # throttled every request must not be reported with the vaguer "nothing answered" copy.
    if bot_blocked:
        result["reason"] = "bot_blocked"
    elif no_coverage:
        result["reason"] = "rate_limited" if rate_limited >= unreachable else "no_link_coverage"
    elif rate_limited_widely:
        result["reason"] = "rate_limited"
    elif truncated:
        result["reason"] = "time_budget"
    return result


def _apply_on_page_checks(
    pages: list[JsonDict],
    summary: JsonDict,
    examples: dict[str, list[str]],
) -> None:
    """Duplicate/missing metadata checks over the rendered pages' stored facts."""
    title_counts: dict[str, int] = defaultdict(int)
    meta_counts: dict[str, int] = defaultdict(int)
    for page in pages:
        title = _clean(_dict(page.get("title")).get("text"))
        if title:
            title_counts[title.lower()] += 1
        meta = _clean(_dict(page.get("meta_description")).get("text"))
        if meta:
            meta_counts[meta.lower()] += 1

    for page in pages:
        address = str(page.get("url") or "")
        title = _clean(_dict(page.get("title")).get("text"))
        meta = _clean(_dict(page.get("meta_description")).get("text"))

        if not title:
            _count(summary, examples, "missing_titles", address)
        elif title_counts[title.lower()] > 1:
            _count(summary, examples, "duplicate_titles", address)

        if not meta:
            _count(summary, examples, "missing_meta_descriptions", address)
        elif meta_counts[meta.lower()] > 1:
            _count(summary, examples, "duplicate_meta_descriptions", address)

        h1_count = _dict(_dict(page.get("headings")).get("counts")).get("h1")
        if not h1_count:
            _count(summary, examples, "missing_h1", address)

        if not _clean(page.get("canonical")):
            _count(summary, examples, "missing_canonicals", address)

        missing_alt = int(_dict(page.get("images")).get("missing_alt") or 0)
        if missing_alt > 0:
            summary["images_missing_alt"] += missing_alt
            _example(examples, "images_missing_alt", address)

        if _dict(page.get("robots")).get("noindex"):
            _count(summary, examples, "non_indexable_internal_urls", address)


def _rendered_url_pairs(
    crawled_pages: JsonDict, rendered_pages: Sequence[Any] | None
) -> list[tuple[str, str]]:
    """(url, final_url) per rendered page, from both page sources — the ONE walk both
    rendered-URL sets derive from, so the sweep-exclusion set and the client-facing count
    can never disagree about which pages were rendered."""
    pairs: list[tuple[str, str]] = []
    for page in _list(crawled_pages.get("pages")):
        if isinstance(page, dict):
            pairs.append((str(page.get("url") or ""), str(page.get("final_url") or "")))
    for page in rendered_pages or []:
        pairs.append(
            (str(getattr(page, "url", "") or ""), str(getattr(page, "final_url", "") or ""))
        )
    return pairs


def _rendered_urls(crawled_pages: JsonDict, rendered_pages: Sequence[Any] | None) -> set[str]:
    """Every known form (pre- and post-redirect) of each rendered page — the sweep-EXCLUSION
    set, so an already-rendered page is not re-checked under either of its URLs."""
    urls: set[str] = set()
    for url, final_url in _rendered_url_pairs(crawled_pages, rendered_pages):
        for candidate in (url, final_url):
            normalized = normalize_url(candidate)
            if normalized:
                urls.add(normalized)
    return urls


def _rendered_final_urls(crawled_pages: JsonDict, rendered_pages: Sequence[Any] | None) -> set[str]:
    """One entry per rendered page (its final URL) — the client-facing site-size COUNT set;
    counting both redirect forms would inflate "Pages discovered" on every redirecting site."""
    urls: set[str] = set()
    for url, final_url in _rendered_url_pairs(crawled_pages, rendered_pages):
        normalized = normalize_url(final_url or url)
        if normalized:
            urls.add(normalized)
    return urls


def _link_inventory(
    *,
    site_url: str,
    crawled_pages: JsonDict,
    rendered_pages: Sequence[Any] | None,
    settings: Settings,
) -> tuple[list[str], list[str], list[str]]:
    """Collect internal/external link targets from the crawl.

    On the first audit run the rendered pages (with HTML) are in memory, so links
    from every rendered page are inventoried. On enrichment reruns only the stored
    crawl JSON is available, which keeps the homepage-discovered internal links but
    no outbound links — that limitation is recorded as a note.
    """
    internal: set[str] = set()
    external: set[str] = set()
    notes: list[str] = []

    for link in _list(crawled_pages.get("discovered_links")):
        if isinstance(link, dict):
            normalized = normalize_url(str(link.get("url") or ""))
            if normalized:
                internal.add(normalized)

    if rendered_pages:
        for page in rendered_pages:
            html = str(getattr(page, "html", "") or "")
            base = str(getattr(page, "final_url", "") or getattr(page, "url", "") or site_url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            for anchor in soup.find_all("a", href=True):
                normalized = normalize_url(str(anchor.get("href", "")).strip(), base)
                if normalized is None:
                    continue
                if is_same_site(site_url, normalized):
                    internal.add(normalized)
                else:
                    external.add(normalized)
    elif settings.site_health_check_external_links:
        notes.append(
            "Outbound links were not rechecked on this enrichment rerun; run a full "
            "audit to refresh outbound link checks."
        )

    return sorted(internal), sorted(external), notes


_SITEMAP_MAX_DEPTH = 2


async def _sitemap_urls(
    client: httpx.AsyncClient,
    site_url: str,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    deadline: float | None = None,
) -> tuple[list[str], str]:
    if settings.site_health_sitemap_max_urls <= 0:
        return [], "disabled"

    parsed = urlparse(site_url)
    sitemap_url = urlunparse((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "", ""))
    urls, status = await _fetch_sitemap(
        client,
        sitemap_url,
        site_url,
        settings=settings,
        host_allowed_cache=host_allowed_cache,
        depth=0,
        deadline=deadline,
    )
    return sorted(urls)[: settings.site_health_sitemap_max_urls], status


async def _fetch_sitemap(
    client: httpx.AsyncClient,
    sitemap_url: str,
    site_url: str,
    *,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    depth: int,
    deadline: float | None = None,
) -> tuple[set[str], str]:
    # Sitemap-index child URLs come straight from attacker-controllable XML, so
    # every fetch gets the same SSRF host guard as swept URLs, and recursion is
    # depth-capped so a self-referencing index cannot loop forever. The whole-audit
    # deadline is honored here too (this phase runs before the sweep's own clock starts).
    if depth > _SITEMAP_MAX_DEPTH:
        return set(), "max_depth"
    if not await asyncio.to_thread(_host_allowed, sitemap_url, settings, host_allowed_cache):
        return set(), "blocked_host"
    try:
        response, _ = await _guarded_request(
            client, "GET", sitemap_url, settings, host_allowed_cache, deadline
        )
    except _BlockedHost:
        return set(), "blocked_host"
    except _DeadlineExceeded:
        return set(), "skipped"
    except httpx.HTTPError:
        return set(), "unavailable"
    if response.status_code == 404:
        return set(), "missing"
    if response.status_code >= 400:
        return set(), f"http_{response.status_code}"

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return set(), "unparseable"

    tag = root.tag.lower()
    locations = [
        " ".join(node.text.split())
        for node in root.iter()
        if node.tag.lower().endswith("loc") and node.text and node.text.strip()
    ]

    urls: set[str] = set()
    if tag.endswith("sitemapindex"):
        for child_url in locations[:3]:
            child_urls, _ = await _fetch_sitemap(
                client,
                child_url,
                site_url,
                settings=settings,
                host_allowed_cache=host_allowed_cache,
                depth=depth + 1,
                deadline=deadline,
            )
            urls.update(child_urls)
        return urls, "loaded_index"

    for location in locations:
        normalized = normalize_url(location)
        if normalized and is_same_site(site_url, normalized):
            urls.add(normalized)
    return urls, "loaded"


async def _sweep(
    client: httpx.AsyncClient,
    *,
    internal_urls: list[str],
    external_urls: list[str],
    settings: Settings,
    summary: JsonDict,
    examples: dict[str, list[str]],
    notes: list[str],
    host_allowed_cache: dict[str, bool],
    global_deadline: float | None = None,
) -> JsonDict:
    # Politeness binds PER TARGET HOST: the audited site takes many requests and needs the
    # narrow lanes + spacing, but outbound links live on DISTINCT third-party hosts and get
    # one or two requests each — serializing them through the same lanes would only burn the
    # time budget. Each host gets its own lane pair + spacing; a global semaphore still caps
    # total concurrency.
    global_lanes = max(1, settings.site_health_concurrency)
    per_host_lanes = max(1, min(global_lanes, settings.site_health_per_host_concurrency))
    delay_seconds = max(0, settings.site_health_request_delay_ms) / 1000.0
    semaphore = asyncio.Semaphore(global_lanes)
    host_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(per_host_lanes)
    )
    seen_hosts: set[str] = set()
    deadline = time.monotonic() + settings.site_health_total_budget_seconds
    if global_deadline is not None:
        # Never run past the whole-audit deadline, even if the sweep's own budget is
        # larger than the time the pipeline has left.
        deadline = min(deadline, global_deadline)
    counters = {
        "internal_checked": 0,
        "external_checked": 0,
        "blocked": 0,
        "skipped_budget": 0,
        "inconclusive_external": 0,
        "rate_limited_internal": 0,
        # Internal URLs that ANSWERED (not throttled, not a transport failure) — the real
        # coverage denominator for the honesty gates in _collect.
        "internal_conclusive": 0,
        "skipped_breaker": 0,
        "waf_signals": 0,
    }
    error_classes: dict[str, int] = defaultdict(int)
    # Circuit breaker: consecutive INTERNAL transport failures mean the host (or its
    # firewall) has stopped answering this checker — keep hammering and every remaining
    # URL becomes a false "dead link".
    breaker = {"consecutive": 0, "tripped": False, "sample_url": None}
    lock = asyncio.Lock()

    async def check(url: str, *, internal: bool) -> None:
        # www and the apex are aliases of ONE origin (sitemaps routinely list the apex while
        # pages link www): fold them into one lane pair + one pacing budget, or the "per-host"
        # limit would run double the configured rate against a single server.
        host = _site_host(url)
        # Only the HOST lane is held across the whole check (politeness pacing and Retry-After
        # waits are per-host concerns). The GLOBAL slot is acquired around each actual HTTP
        # request inside _check_url — a lane sleeping out a 30s Retry-After must not occupy
        # one of the few global slots and starve every other host's checks. Ordering stays
        # host lane -> global slot everywhere => no deadlock.
        async with host_semaphores[host]:
            async with lock:
                if breaker["tripped"]:
                    counters["skipped_breaker"] += 1
                    return
            # Budget is checked AFTER acquiring the semaphores: all coroutines are
            # scheduled up front by gather(), so a pre-semaphore check would run at
            # t~0 for every URL and the budget could never actually fire.
            if time.monotonic() > deadline:
                async with lock:
                    counters["skipped_budget"] += 1
                return
            if delay_seconds:
                # Polite pacing with jitter, scoped to the TARGET host: the first request
                # to a host goes straight out (a single outbound link costs no delay),
                # repeat requests to the same host are spaced as before. Only timing
                # varies — the collected facts stay deterministic.
                async with lock:
                    repeat_host = host in seen_hosts
                    seen_hosts.add(host)
                if repeat_host:
                    await asyncio.sleep(delay_seconds * (0.75 + random.random() * 0.5))
            # getaddrinfo is blocking; keep it off the event loop so one slow DNS
            # lookup cannot stall every in-flight request.
            allowed = await asyncio.to_thread(_host_allowed, url, settings, host_allowed_cache)
            if not allowed:
                async with lock:
                    counters["blocked"] += 1
                return
            try:
                status_code, x_robots, error, redirect_hops, err_class, waf = await _check_url(
                    client,
                    url,
                    settings,
                    host_allowed_cache,
                    global_slot=semaphore,
                    deadline=deadline,
                )
            except _BlockedHost:
                async with lock:
                    counters["blocked"] += 1
                return
            except _DeadlineExceeded:
                # The budget expired while this check queued for a global slot (the pre-check
                # above is admission-only once every distinct host holds its own lane).
                async with lock:
                    counters["skipped_budget"] += 1
                return

        async with lock:
            counters["internal_checked" if internal else "external_checked"] += 1
            if waf:
                counters["waf_signals"] += 1
            if error is not None:
                key = "unreachable_internal_urls" if internal else "unreachable_external_urls"
                summary[key] = int(summary.get(key) or 0) + 1
                if internal:
                    # Only internal unreachables surface as a report issue; dead
                    # outbound hosts are often bot-blocking and would over-claim.
                    _example(examples, "unreachable_internal_urls", url)
                    error_classes[err_class or "network"] += 1
                    breaker["consecutive"] += 1
                    if breaker["sample_url"] is None:
                        breaker["sample_url"] = url
                    if breaker["consecutive"] >= settings.site_health_breaker_threshold:
                        breaker["tripped"] = True
                return
            if status_code is None:
                return
            if internal:
                # The client's own site: every error is a reliable, actionable
                # signal because we control the request.
                if status_code == _RATE_LIMIT_STATUS:
                    # Still 429 after the polite retries + the one-shot GET: the host is
                    # THROTTLING the checker, not serving a broken page. Counting it as a
                    # client error would score healthy URLs as broken; tally it as
                    # inconclusive instead (widespread throttling downgrades the whole
                    # source to `partial` after the sweep) — and take it back OUT of the
                    # checked total, because the client-facing note says these URLs are
                    # "reported as unchecked" and the rendered count must agree. This runs
                    # BEFORE the redirect tallies: an unchecked URL must not feed the scored
                    # redirect facts either, or issue counts could exceed urls_checked.
                    counters["internal_checked"] -= 1
                    counters["rate_limited_internal"] += 1
                    return
                # The URL ANSWERED — real coverage. Tracked separately from internal_checked
                # (which also counts transport failures) because the honesty gates measure
                # throttling/refusals against what actually answered, and it is what resets
                # the transport-failure breaker: a 429 is the host refusing, not a healthy
                # response, so it must never reset the consecutive-failure count.
                counters["internal_conclusive"] += 1
                breaker["consecutive"] = 0
                # Internal links should point at the final URL; a redirect wastes crawl
                # budget and a 2+-hop chain is a real crawlability problem (scored).
                if redirect_hops >= 1:
                    summary["redirecting_internal_urls"] = (
                        int(summary.get("redirecting_internal_urls") or 0) + 1
                    )
                if redirect_hops >= 2:
                    _count(summary, examples, "redirect_chain_internal_urls", url)
                if 400 <= status_code <= 499:
                    _count(summary, examples, "client_error_internal_urls", url)
                elif status_code >= 500:
                    _count(summary, examples, "server_error_internal_urls", url)
                elif x_robots and "noindex" in x_robots.lower():
                    _count(summary, examples, "non_indexable_internal_urls", url)
                return
            # Outbound link: only count statuses we can trust from a server-side
            # bot. 404/410 means the destination page is genuinely gone and a 5xx
            # is a real server error; auth/forbidden/rate-limit/legal codes are
            # bot-blocking noise (the link works for real visitors), so they are
            # tallied for QA but never reported as broken.
            if status_code in _BROKEN_EXTERNAL_CLIENT_STATUSES:
                _count(summary, examples, "client_error_external_urls", url)
            elif status_code >= 500:
                _count(summary, examples, "server_error_external_urls", url)
            elif 400 <= status_code <= 499:
                counters["inconclusive_external"] += 1

    await asyncio.gather(
        *(check(url, internal=True) for url in internal_urls),
        *(check(url, internal=False) for url in external_urls),
    )

    if counters["internal_checked"] or counters["external_checked"]:
        # Answer "what was the crawl rate?" in the report itself.
        notes.append(
            f"Link checks ran politely: up to {per_host_lanes} concurrent requests per host "
            f"with ~{settings.site_health_request_delay_ms} ms spacing between same-host "
            f"requests (at most {global_lanes} in flight overall)."
        )
    if counters["skipped_budget"]:
        notes.append(
            f"{counters['skipped_budget']} discovered URLs were not checked because the "
            f"site health check reached its {settings.site_health_total_budget_seconds}s "
            "time budget."
        )
    if counters["rate_limited_internal"]:
        plural = "s" if counters["rate_limited_internal"] != 1 else ""
        notes.append(
            f"{counters['rate_limited_internal']} internal URL{plural} answered only with "
            "rate-limit responses (HTTP 429) after polite retries; they are reported as "
            "unchecked rather than broken."
        )

    counters["error_classes"] = dict(sorted(error_classes.items()))
    counters["bot_blocked"] = bool(breaker["tripped"])
    if breaker["tripped"]:
        recheck_ok = await _browser_ua_recheck(breaker["sample_url"], settings)
        counters["browser_recheck_ok"] = recheck_ok
        detail = (
            "the same URL answered a regular browser profile, so the server is filtering "
            "automated traffic"
            if recheck_ok
            else "the server stopped answering our checker entirely"
        )
        notes.append(
            f"Link checking stopped early after "
            f"{settings.site_health_breaker_threshold} consecutive network failures "
            f"({detail}); {counters['skipped_breaker']} URLs were left unchecked. Link "
            "results are incomplete, are not scored, and flagged URLs should be verified "
            "manually in a browser."
        )
    return counters


async def _slotted_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    global_slot: asyncio.Semaphore | None,
    deadline: float | None = None,
) -> tuple[httpx.Response, int]:
    """One guarded request, holding the sweep's GLOBAL slot only for the request itself —
    Retry-After / politeness sleeps in the callers run outside it, so a throttled host
    can't starve the other hosts' checks (the per-host lane still serializes the host).
    The budget is re-checked HERE, at the moment the request would go out: the caller's
    pre-check is admission-only once every distinct host holds its own lane, and a check
    that queued past the deadline must be skipped, not sent."""
    if global_slot is None:
        return await _guarded_request(client, method, url, settings, host_allowed_cache, deadline)
    async with global_slot:
        return await _guarded_request(client, method, url, settings, host_allowed_cache, deadline)


async def _request_polite(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    global_slot: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> tuple[httpx.Response, int]:
    """One request with rate-limit awareness: a 429 is honored (Retry-After, capped)
    and retried a bounded number of times instead of hammered."""
    response, hops = await _slotted_request(
        client, method, url, settings, host_allowed_cache, global_slot, deadline
    )
    for attempt in range(_MAX_RATE_LIMIT_RETRIES):
        if response.status_code != _RATE_LIMIT_STATUS:
            break
        wait_seconds = _retry_after_seconds(response, attempt)
        if deadline is not None and time.monotonic() + wait_seconds > deadline:
            # Out of budget: keep the honest 429 outcome (tallied as rate-limited/unchecked)
            # rather than sleeping past the sweep's deadline for another attempt.
            break
        await asyncio.sleep(wait_seconds)
        response, hops = await _slotted_request(
            client, method, url, settings, host_allowed_cache, global_slot, deadline
        )
    return response, hops


def _check_outcome(
    response: httpx.Response, hops: int
) -> tuple[int | None, str | None, str | None, int, str | None, bool]:
    return (
        response.status_code,
        response.headers.get("x-robots-tag"),
        None,
        hops,
        None,
        _looks_waf(response),
    )


def _check_failure(
    exc: httpx.HTTPError,
) -> tuple[int | None, str | None, str | None, int, str | None, bool]:
    return None, None, _trim(str(exc) or exc.__class__.__name__), 0, _error_class(exc), False


async def _check_url(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    global_slot: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> tuple[int | None, str | None, str | None, int, str | None, bool]:
    """Return (status_code, x_robots_tag_header, error, redirect_hops, error_class,
    waf_suspected).

    GET is retried only when HEAD specifically is the problem (servers that
    reject or mishandle HEAD, or rate-limit it with a persistent 429). Timeouts and
    connection failures would fail a GET identically, so retrying them would just
    double the worst-case latency.
    """
    try:
        response, hops = await _request_polite(
            client, "HEAD", url, settings, host_allowed_cache, global_slot, deadline
        )
        if response.status_code in _RETRY_GET_STATUSES:
            response, hops = await _request_polite(
                client, "GET", url, settings, host_allowed_cache, global_slot, deadline
            )
        elif response.status_code == _RATE_LIMIT_STATUS:
            # The polite ladder already honored Retry-After for the HEAD attempts; the GET
            # fallback is a SINGLE shot. A host rate-limiting both methods must not receive a
            # second full retry ladder (that would double the traffic to a host that just
            # asked the checker to back off).
            response, hops = await _slotted_request(
                client, "GET", url, settings, host_allowed_cache, global_slot, deadline
            )
        return _check_outcome(response, hops)
    except httpx.RemoteProtocolError:
        try:
            # Same global slot + deadline as every other request — this fallback once ran
            # unslotted, letting HEAD-hostile hosts push the sweep past its concurrency cap.
            response, hops = await _request_polite(
                client, "GET", url, settings, host_allowed_cache, global_slot, deadline
            )
            return _check_outcome(response, hops)
        except httpx.HTTPError as exc:
            return _check_failure(exc)
    except httpx.HTTPError as exc:
        return _check_failure(exc)


async def _guarded_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    settings: Settings,
    host_allowed_cache: dict[str, bool],
    deadline: float | None = None,
) -> tuple[httpx.Response, int]:
    """Follow redirects only after each target passes the SSRF host guard. Returns the final
    response and the number of redirect hops followed to reach it (0 = no redirect).

    The deadline is re-checked before EVERY hop: a chain of up to _MAX_REDIRECTS targets, each
    with its own request timeout, is otherwise unbounded work admitted by a single pre-request
    budget check — enough to run a slow host far past the sweep's budget and eat the pipeline
    tail reserve the audit holds back for scoring and rendering."""
    current_url = normalize_url(url)
    if current_url is None:
        raise _BlockedHost

    for redirect_count in range(_MAX_REDIRECTS + 1):
        if deadline is not None and time.monotonic() > deadline:
            raise _DeadlineExceeded(current_url)
        allowed = await asyncio.to_thread(
            _host_allowed,
            current_url,
            settings,
            host_allowed_cache,
        )
        if not allowed:
            raise _BlockedHost

        response = await client.request(method, current_url)
        if not response.is_redirect:
            return response, redirect_count

        location = response.headers.get("location")
        if not location:
            return response, redirect_count
        if redirect_count >= _MAX_REDIRECTS:
            raise httpx.TooManyRedirects(
                f"Exceeded {_MAX_REDIRECTS} redirects while requesting {url}",
                request=response.request,
            )

        next_url = normalize_url(urljoin(str(response.url), location))
        if next_url is None:
            raise _BlockedHost
        current_url = next_url

    raise httpx.TooManyRedirects(
        f"Exceeded {_MAX_REDIRECTS} redirects while requesting {url}",
        request=httpx.Request(method, url),
    )


def _host_allowed(url: str, settings: Settings, cache: dict[str, bool]) -> bool:
    """SSRF guard: never sweep private/loopback/reserved hosts unless explicitly allowed."""
    if settings.crawler_allow_private_hosts:
        return True
    hostname = (urlparse(url).hostname or "").lower()
    if not hostname:
        return False
    if hostname in cache:
        return cache[hostname]

    allowed = not _hostname_is_private(hostname)
    if allowed:
        try:
            resolved = _resolve_host_ips(hostname)
            allowed = bool(resolved) and not any(_ip_is_blocked(ip) for ip in resolved)
        except OSError:
            allowed = False
    cache[hostname] = allowed
    return allowed


def _count(
    summary: JsonDict,
    examples: dict[str, list[str]],
    key: str,
    address: str,
) -> None:
    summary[key] = int(summary.get(key) or 0) + 1
    _example(examples, key, address)


def _example(examples: dict[str, list[str]], key: str, address: str) -> None:
    if address and len(examples[key]) < 10 and address not in examples[key]:
        examples[key].append(address)


def _skipped(reason: str, started_at: str) -> JsonDict:
    return {
        "status": "skipped",
        "source": SITE_HEALTH_SOURCE,
        "reason": reason,
        "summary": {},
        "issues": [],
        "files": [],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _failed(reason: str, started_at: str) -> JsonDict:
    return {
        "status": "failed",
        "source": SITE_HEALTH_SOURCE,
        "error": reason,
        "summary": {},
        "issues": [],
        "files": [],
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean(value: Any) -> str | None:
    cleaned = " ".join(str(value or "").split())
    return cleaned or None


def _trim(value: str) -> str:
    cleaned = " ".join(value.split())
    return cleaned[:500] or "site health sweep error"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
