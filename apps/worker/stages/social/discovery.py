"""Auto-discover a site's own social profile links from already-crawled HTML (Phase 2).

When the operator leaves the Instagram / Facebook / YouTube fields blank, a plain website audit can
still gain a Social Media Audit section by finding the social-icon links almost every builder site
puts in its header/footer. This is **pure and deterministic**: it only parses the HTML the crawler
already downloaded (no new network request), so audits stay reproducible.

Two guards keep it from attributing a *stranger's* profile to the audited business:
  * links that sit in a credit/attribution line ("Site by <agency>", "as seen in <press>") are
    ignored — unless the handle IS one of the audited domain's own labels (a marketing firm whose
    footer reads "Marketing by the pros" beside its own icon) — so an agency/partner/press
    profile is not scored as the client's; and
  * a discovered profile is only trusted when it looks like the site's own — a real placement-backed
    link (footer/header/nav) or a handle that resembles the site's brand — so a single passing body
    mention can't promote a website audit to combined.

The result is shaped exactly like ``audit_jobs.social_handles`` (``{platform: profile_url}``) and is
fed straight into the existing social collector — the Apify/YouTube providers already accept a full
profile URL, so a discovered URL needs no further parsing. Explicit operator handles always win per
platform; discovery only fills the platforms left blank (see ``tasks._resolve_social_handles``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from apps.worker.stages.crawler import _site_host, normalize_url
from apps.worker.stages.social.extractor import profile_url_name
from apps.worker.stages.technical_crawl_common import (
    MULTI_TENANT_PLATFORMS,
    PATH_TENANT_HOSTS,
    registrable_brand_label,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from apps.worker.stages.crawler import CrawledPage

JsonDict = dict[str, str]

# Registrable host -> platform. ``www.`` and ``m.`` prefixes are stripped before lookup. Only
# hosts that point at *profiles* are listed: youtu.be (always a video) and fb.me (an ambiguous
# redirector) are deliberately excluded so we never mistake a share link for a profile.
_PLATFORM_HOSTS = {
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "youtube.com": "youtube",
}

# First path segments that are NOT a profile handle (post permalinks, share/intent endpoints,
# app routes, etc.). A link whose first segment is reserved is ignored.
_RESERVED_SEGMENTS = {
    "instagram": {
        "p",
        "reel",
        "reels",
        "explore",
        "stories",
        "tv",
        "accounts",
        "about",
        "directory",
        "business",
        "developer",
        "developers",
        "legal",
        "privacy",
        "help",
        "web",
        "api",
    },
    "facebook": {
        "sharer",
        "sharer.php",
        "share",
        "share.php",
        "dialog",
        "plugins",
        "tr",
        "tr.php",
        "login",
        "login.php",
        "events",
        "groups",
        "watch",
        "marketplace",
        "gaming",
        "help",
        "policies",
        "privacy",
        "settings",
        "permalink.php",
        "story.php",
        "photo.php",
        "bookmarks",
        "notes",
        "careers",
        "business",
        "ads",
        "home.php",
    },
    "youtube": {"watch", "embed", "results", "feed", "playlist", "shorts", "hashtag", "redirect"},
}

# Handle/slug shapes per platform — deliberately reject leading/trailing punctuation so a
# non-profile path (e.g. ``/.well-known`` or a tracking fragment) can't be mistaken for a handle.
# Instagram: <=30 chars of letters/digits/_/., never starting or ending with a dot.
_IG_HANDLE = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?$")
# Facebook vanity / page slug: alnum/./- , must start AND end alphanumeric (covers the hyphenated
# "Name-Name-123456789" page-slug form too).
_FB_VANITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,78}[A-Za-z0-9]$")
# YouTube @handle is 3-30 chars; a channel id is exactly "UC" + 22 url-safe base64 chars.
_YT_HANDLE = re.compile(r"^@[A-Za-z0-9_.-]{3,30}$")
_YT_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_YT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_PLATFORMS = ("instagram", "facebook", "youtube")

# Discrete container-attr tokens that mark a footer/header/nav block. Matched as whole tokens (not
# substrings) so "subnav"/"asocial-proof" don't masquerade as navigation.
_NAV_TOKENS = {"nav", "navbar", "navigation", "menu", "social", "socials"}

# A real social-icon link sits in site chrome — footer (+3.0), header (+2.0), or a semantic <nav>
# element (+2.0) — or carries the brand in its handle (+2.0); each clears this floor on top of the
# 1.0 base. A generic nav/social class token is worth only 1.0, deliberately BELOW the floor, so a
# "social-proof"/"menu"-classed body container (testimonials, restaurant menus) can never get a
# stranger's profile scraped and scored as the client's.
_MIN_PLACEMENT_SCORE = 2.5
# Extra confidence when the discovered handle resembles the audited site's brand.
_BRAND_MATCH_BONUS = 2.0

# Surrounding text marking a link as a THIRD-PARTY credit, not the site's own profile:
# "Site by / Designed by / Powered by / Hosted by <agency>", "as seen in <press>", and
# attribution-shaped credit lines ("photo credit", "credits:"). A bare "credit(s)" is deliberately
# NOT enough — financing copy ("credit approval required", "we accept all major credit cards")
# must not discard the site's own link.
_ATTRIBUTION_RE = re.compile(
    r"\b(?:site|website|web\s*design|design(?:ed)?|develop(?:ed|ment)?|built|build|powered"
    r"|host(?:ed|ing)?|market(?:ing)?|brand(?:ing)?|crafted|created|managed|maintained)\s+by\b"
    r"|\bas\s+seen\s+(?:in|on)\b|\bfeatured\s+(?:in|on)\b"
    r"|\b(?:photo|image|video|design|photography)\s+credits?\b|\bcredits?\s*:",
    re.IGNORECASE,
)
# A credit line is short ("Site by Acme Agency"); a whole footer's text is long. Only the anchor's
# SHORT immediate neighbourhood counts as attribution, so a long footer that merely contains a
# copyright/credit line elsewhere doesn't taint a genuine social-icon link sitting beside it.
_ATTRIBUTION_CONTEXT_MAX_CHARS = 120

# Domain labels that are public-suffix noise, never a brand token.
_NON_BRAND_LABELS = {"www", "com", "net", "org", "biz", "gov", "edu", "co"}

# urlsplit strips tab/CR/LF anywhere in a URL (WHATWG), so the fast-path prefilter must strip
# them too — a soft-wrapped href ("instagram\n.com") still canonicalizes and must not be skipped
# before the canonicaliser gets to accept it.
_HREF_WS_TABLE = str.maketrans("", "", "\t\r\n")


def _host_platform(host: str | None) -> str | None:
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    elif host.startswith("m."):
        host = host[2:]
    return _PLATFORM_HOSTS.get(host)


def _profile_url(platform: str, parsed) -> str | None:
    """Map a parsed social URL to a canonical profile URL, or None when it isn't a profile.

    Instagram/Facebook handles are case-insensitive, so they're canonicalised to lowercase (a
    YouTube channel id is base64 and case-SENSITIVE, so it is kept verbatim)."""
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    reserved = _RESERVED_SEGMENTS[platform]
    if not segments:
        return None
    first = segments[0]
    low = first.lower()

    if platform == "instagram":
        if low in reserved or not _IG_HANDLE.match(first):
            return None
        return f"https://www.instagram.com/{low}/"

    if platform == "facebook":
        if low == "profile.php":
            ident = (parse_qs(parsed.query).get("id") or [""])[0]
            return f"https://www.facebook.com/profile.php?id={ident}" if ident.isdigit() else None
        # /pages/<Name>/<id> — the directory form. Require a real page slug + numeric id and reject
        # the /pages/category/... listing; anything else under /pages is NOT a profile.
        if low == "pages":
            if (
                len(segments) >= 3
                and segments[1].lower() != "category"
                and _FB_VANITY.match(segments[1])
                and segments[2].isdigit()
            ):
                return f"https://www.facebook.com/pages/{segments[1]}/{segments[2]}"
            return None
        # /pg/<PageName>(/...) — the old page-prefix form; the page name is the second segment.
        # Reserved words are rejected in the name position too (like the YouTube /c//user/
        # branch) — including the structural routes (pages/pg/profile.php) the plain-vanity
        # branch handles before its own reserved check — so neither /pg/sharer/... nor
        # /pg/pages/... can smuggle an app route past the vanity check.
        if low == "pg":
            name = segments[1].lower() if len(segments) >= 2 else ""
            if (
                name
                and name not in reserved
                and name not in ("pages", "pg", "profile.php")
                and _FB_VANITY.match(segments[1])
            ):
                return f"https://www.facebook.com/{name}/"
            return None
        # /people/<Name>/<id> and /p/<Name>-<id> — the modern page forms Facebook assigns
        # pages without a vanity username (the default for recently created business pages).
        # Kept in their FULL form: the name slug alone is not a working profile path, and the
        # plain-vanity fallthrough below would otherwise canonicalize these to the directory
        # root (facebook.com/people/) — a non-profile URL scored as the client's page.
        if low == "people":
            if (
                len(segments) >= 3
                and segments[1].lower() not in reserved
                and _FB_VANITY.match(segments[1])
                and segments[2].isdigit()
            ):
                return f"https://www.facebook.com/people/{segments[1]}/{segments[2]}"
            return None
        if low == "p":
            if (
                len(segments) >= 2
                and segments[1].lower() not in reserved
                and _FB_VANITY.match(segments[1])
                and re.search(r"-\d{5,}$", segments[1])
            ):
                return f"https://www.facebook.com/p/{segments[1]}"
            return None
        if low in reserved or not _FB_VANITY.match(first):
            return None
        return f"https://www.facebook.com/{low}/"

    if platform == "youtube":
        if first.startswith("@") and _YT_HANDLE.match(first):
            return f"https://www.youtube.com/{first}"
        if low == "channel" and len(segments) >= 2 and _YT_CHANNEL_ID.match(segments[1]):
            return f"https://www.youtube.com/channel/{segments[1]}"
        # /c/<name> and /user/<name> — reject reserved words in the name position too.
        if (
            low in {"c", "user"}
            and len(segments) >= 2
            and segments[1].lower() not in reserved
            and _YT_NAME.match(segments[1])
        ):
            return f"https://www.youtube.com/{low}/{segments[1]}"
        return None

    return None


def _ancestor_tokens(tag: Tag) -> set[str]:
    """Lowercased id/class/role/aria-label values split into discrete tokens (so 'subnav' or
    'asocial-proof' don't match the whole-token nav set)."""
    raw: list[str] = []
    for attr in ("id", "class", "role", "aria-label"):
        value = tag.get(attr)
        if isinstance(value, list):
            raw.extend(str(v) for v in value)
        elif value:
            raw.append(str(value))
    tokens: set[str] = set()
    for chunk in raw:
        tokens.update(token for token in re.split(r"[^a-z0-9]+", chunk.lower()) if token)
    return tokens


def _placement_bonus(anchor: Tag) -> float:
    """Score a link higher when it sits in a footer/header/nav/social block — where sites put
    their real social-icon links — so a footer profile link beats an inline mention in the body."""
    bonus = 0.0
    nav_bonus = 0.0
    seen: set[str] = set()
    for parent in anchor.parents:
        if len(seen) == 3:
            break  # footer + header + semantic nav all credited; deeper ancestors can't raise it
        if not isinstance(parent, Tag):
            continue
        name = (parent.name or "").lower()
        tokens = _ancestor_tokens(parent)
        if "footer" not in seen and (name == "footer" or "footer" in tokens):
            bonus += 3.0
            seen.add("footer")
        if "header" not in seen and (name == "header" or "header" in tokens):
            bonus += 2.0
            seen.add("header")
        # A semantic <nav> element is site chrome (2.0, like header). A mere nav/social class token
        # is only a weak hint (1.0) that alone stays below _MIN_PLACEMENT_SCORE — a "social"-classed
        # body container (social proof, testimonials) must not look like a placed icon. Only the
        # strongest nav signal on the ancestor chain counts, once.
        if "nav" not in seen:
            if name == "nav":
                nav_bonus = 2.0
                seen.add("nav")
            elif tokens & _NAV_TOKENS:
                nav_bonus = max(nav_bonus, 1.0)
    return bonus + nav_bonus


def _is_attribution_context(anchor: Tag) -> bool:
    """True when the link sits in a credit/attribution line ("Site by <agency>", "as seen in
    <press>") — so a third-party profile is never mistaken for the site's own."""
    text = anchor.get_text(" ", strip=True)
    parent = anchor.parent
    if isinstance(parent, Tag):
        parent_text = parent.get_text(" ", strip=True)
        # Only a SHORT neighbourhood counts — a whole footer's text would false-positive on an
        # unrelated credit/copyright line elsewhere within it.
        if len(parent_text) <= _ATTRIBUTION_CONTEXT_MAX_CHARS:
            text = f"{text} {parent_text}"
    return bool(_ATTRIBUTION_RE.search(text))


def _brand_tokens(site_url: str | None) -> set[str]:
    """Lowercased alphanumeric labels of the audited domain (minus the TLD/'www'), used to tell the
    site's OWN profile from a partner's by handle resemblance.

    The registrable brand label always counts (the ONE shared deriver, which also resolves the
    PATH-tenant hosts where the brand lives in the path: on sites.google.com/view/smithbuilders
    the host labels are the PLATFORM's — "google", "sites" — and without this the platform's own
    profile would earn the brand bonus AND the credit-line rescue while the client's real handle
    matched nothing)."""
    if not site_url:
        return set()
    tokens: set[str] = set()
    brand = registrable_brand_label(site_url)
    if len(brand) >= 3:
        tokens.add(re.sub(r"[^a-z0-9]", "", brand))
    host = _site_host(site_url)  # reuses the crawler's registrable-host logic (strips www.)
    if not host or host in PATH_TENANT_HOSTS:
        # On a path-tenant host every host label is the platform's, never the client's.
        return {token for token in tokens if token}
    labels = host.split(".")
    if len(labels) >= 2:
        labels = labels[:-1]  # drop the TLD
    for label in labels:
        norm = re.sub(r"[^a-z0-9]", "", label.lower())
        # A multi-tenant hosting label is the PLATFORM's brand, not the audited site's: on
        # smith.wordpress.com only "smith" may match, or the exact-brand credit rescue (and
        # the brand score bonus) would attribute the platform's own profile to the client.
        if len(norm) >= 3 and norm not in _NON_BRAND_LABELS and norm not in MULTI_TENANT_PLATFORMS:
            tokens.add(norm)
    return {token for token in tokens if token}


def _profile_handle(profile_url: str) -> str:
    """The handle/slug of a canonical profile URL (the numeric id for a profile.php link).

    The name-after-marker forms (``/pages/<Name>/<id>``, ``/people/<Name>/<id>``,
    ``/p/<Name-ID>``, ``/c/<name>``, ``/user/<name>``) carry the brand in the NAME segment —
    their trailing/appended numeric page id could never match a brand token — so the shared
    profile_url_name parser extracts it (one parser with the scored consistency key and the
    report display, so a URL form taught there is brand-matchable here too)."""
    parsed = urlparse(profile_url)
    if parsed.path.endswith("profile.php"):
        return (parse_qs(parsed.query).get("id") or [""])[0]
    segments = [seg for seg in parsed.path.split("/") if seg]
    name = profile_url_name(parsed.hostname, segments)
    if name is not None:
        return name
    return segments[-1].lstrip("@") if segments else ""


def _matches_brand(profile_url: str, brand_tokens: set[str]) -> bool:
    if not brand_tokens:
        return False
    handle = re.sub(r"[^a-z0-9]", "", _profile_handle(profile_url).lower())
    if len(handle) < 3:
        return False
    return any(token in handle or handle in token for token in brand_tokens)


def _exact_brand_match(profile_url: str, brand_tokens: set[str]) -> bool:
    """The handle IS one of the audited domain's labels — not merely overlapping one.

    Only this stronger form may override an explicit third-party credit marker: the bidirectional
    substring test behind the score bonus would rescue a credited photographer's ``@martinez`` on
    ``martinezconstruction.com`` (handle ⊂ token) and let it displace the site's own profile."""
    if not brand_tokens:
        return False
    handle = re.sub(r"[^a-z0-9]", "", _profile_handle(profile_url).lower())
    return len(handle) >= 3 and handle in brand_tokens


def discover_social_links(pages: Iterable[CrawledPage], site_url: str | None = None) -> JsonDict:
    """Find the site's own Instagram/Facebook/YouTube profile links across the crawled pages.

    Returns ``{platform: profile_url}`` (at most one URL per platform — the best-scored candidate).
    Empty when the site links to no profiles that look like its own. ``site_url`` lets discovery
    prefer handles matching the audited brand. Pure over the stored HTML — no network calls, so
    audits stay reproducible.
    """
    brand_tokens = _brand_tokens(site_url)
    # candidates[platform][profile_url] = accumulated score (higher = more likely the site's own)
    candidates: dict[str, dict[str, float]] = {p: {} for p in _PLATFORMS}

    for page in pages:
        html = getattr(page, "html", "") or ""
        if not html:
            continue
        base_url = getattr(page, "final_url", None) or getattr(page, "url", None) or site_url
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            raw_href = str(anchor.get("href", "")).strip()
            if not raw_href or raw_href.startswith("#"):
                continue
            # Fast path: a social profile link must carry one of the platform hosts as a
            # substring, so skip the full canonicalisation for the overwhelmingly non-social
            # majority of a page's anchors. Tab/CR/LF are stripped first, mirroring urlsplit.
            # Purely an optimisation — a false positive ("clubfb.com") still fails the host
            # check below, exactly as it did without the prefilter. (A relative href resolves
            # to a platform host only when the audited site IS one — pathological here.)
            lowered_href = raw_href.translate(_HREF_WS_TABLE).lower()
            if not any(host in lowered_href for host in _PLATFORM_HOSTS):
                continue
            # Reuse the crawler's URL canonicaliser: resolves protocol-relative (//host) and
            # relative hrefs against the page, strips credentials/fragments, rejects non-http(s).
            normalized = normalize_url(raw_href, base_url=base_url)
            if normalized is None:
                continue
            parsed = urlparse(normalized)
            platform = _host_platform(parsed.hostname)
            if platform is None:
                continue
            profile_url = _profile_url(platform, parsed)
            if profile_url is None:
                continue
            # Skip third-party credit links (agency/press/partner) — don't score them as ours.
            # Rescue requires an EXACT brand-label handle: a self-descriptive footer ("Marketing
            # by the pros" beside @acmemktg on acmemktg.com) or a compact credit line sharing
            # its parent with the site's own icon keeps the real profile. The looser substring
            # match stays veto-able — it would rescue a credited photographer's @martinez on
            # martinezconstruction.com and let it displace the client's real profile.
            if _is_attribution_context(anchor) and not _exact_brand_match(
                profile_url, brand_tokens
            ):
                continue
            score = 1.0 + _placement_bonus(anchor)
            if _matches_brand(profile_url, brand_tokens):
                score += _BRAND_MATCH_BONUS
            bucket = candidates[platform]
            bucket[profile_url] = bucket.get(profile_url, 0.0) + score

    result: JsonDict = {}
    for platform in _PLATFORMS:
        by_url = candidates[platform]
        if not by_url:
            continue
        # Earliest-seen URL wins a score tie (dict preserves insertion order; max keeps the first).
        best_url = max(by_url, key=lambda url: by_url[url])
        # Only trust it as the site's OWN when it's placement-backed or brand-matching — a single
        # bare body mention (score 1.0) is below the floor and won't promote the audit.
        if by_url[best_url] >= _MIN_PLACEMENT_SCORE:
            result[platform] = best_url
    return result
