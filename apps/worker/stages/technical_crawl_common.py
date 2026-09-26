"""Shared vocabulary for technical crawl facts.

Two collectors can fill the ``external_seo.technical_crawl`` slot — the optional
Screaming Frog CLI (licensed desktop tool) and the built-in site health sweep
(``site_health.py``). Both must emit the same summary keys and issue ids so the
rubric (``rubrics/seo.yaml``) and the report guidance
(``report_payload.TECHNICAL_ISSUE_GUIDANCE``) work identically for either tool.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

JsonDict = dict[str, Any]

# --- Site identity ----------------------------------------------------------------------------
# Second-level labels that never name the brand: TLD families (acme.co.uk) and multi-tenant
# hosting platforms (smithbuilders.wixsite.com) — in both cases the brand sits further left.
# ONE shared home (this module is imported by both technical-crawl collectors and the social
# stages) so the GSC branded split, the Places business query + identity gate, and social
# discovery's brand tokens can never disagree about what counts as a brand label.
GENERIC_SECOND_LEVELS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})
MULTI_TENANT_PLATFORMS = frozenset(
    {
        "blogspot",
        "business",
        "carrd",
        "github",
        "godaddysites",
        "myshopify",
        "netlify",
        "notion",
        "pages",
        "square",
        "squarespace",
        "strikingly",
        "vercel",
        "webflow",
        "weebly",
        "wixsite",
        "wordpress",
    }
)


# Shared hosts where the TENANT is a path segment, not a subdomain: the brand never lives in
# the host there, so host-label walking would yield the PLATFORM's brand ("google"). Structural
# path segments precede the tenant slug: sites.google.com/view/<tenant>, the legacy
# /site/<tenant>, the signed-in /u/<n>/view/<tenant>, and the Workspace /a/<domain>/<marker>/…
PATH_TENANT_HOSTS = frozenset({"sites.google.com"})
_PATH_TENANT_MARKERS = frozenset({"view", "site"})
# Structural PREFIXES that carry their own argument segment (/u/0/…, /a/acme.com/…) — both the
# marker and its argument are dropped before the tenant slug is read, or the "brand" would come
# back as "u"/"a" (a junk token that still funds a billed Places lookup).
_PATH_TENANT_PREFIXES = frozenset({"u", "a"})


def registrable_brand_label(url_or_host: str) -> str:
    """The label most likely to be the BRAND in a site URL/host: strip ``www.``, take the
    registrable label, walking LEFT past EVERY generic second-level and multi-tenant platform
    label — ``smith.co.uk`` -> ``smith``, ``smithbuilders.wixsite.com`` -> ``smithbuilders``,
    and the stacked form ``smith.blogspot.co.uk`` -> ``smith`` (a single step would stop at
    the platform name). On a PATH-tenant host the brand is the tenant path slug —
    ``sites.google.com/view/smithbuilders`` -> ``smithbuilders`` — and a bare path-tenant host
    yields "" (no brand derivable; "google" would misclassify every 'google' query as branded
    and send a billed Places lookup for the platform itself). The ONE brand deriver — consumed
    by the GSC branded split (``_brand_token``), the Places business query
    (``tasks._business_query``), and the Places identity gate, so they can never disagree
    about a site's brand."""
    host = url_or_host
    path = ""
    if "/" in host or ":" in host:
        parsed = urlparse(url_or_host)
        host = parsed.hostname or ""
        path = parsed.path or ""
    host = host.lower().rstrip(".").removeprefix("www.")
    if host in PATH_TENANT_HOSTS:
        segments = [seg for seg in path.split("/") if seg]
        while segments:
            head = segments[0].lower()
            if head in _PATH_TENANT_PREFIXES and len(segments) >= 2:
                segments = segments[2:]  # drop the prefix AND its argument (/u/0/, /a/acme.com/)
            elif head in _PATH_TENANT_MARKERS:
                segments = segments[1:]
            else:
                break
        return segments[0].lower() if segments else ""
    labels = [label for label in host.split(".") if label]
    if not labels:
        return ""
    index = len(labels) - 2 if len(labels) >= 2 else 0
    non_brand = GENERIC_SECOND_LEVELS | MULTI_TENANT_PLATFORMS
    while index > 0 and labels[index] in non_brand:
        index -= 1
    return labels[index]


# URL-path markers that identify a blog/article page, for the website-scope post count.
# Shared by BOTH technical-crawl collectors so the report's "what your website consists of"
# panel means the same thing whichever tool filled the slot.
POST_PATH_MARKERS = ("/blog/", "/blogs/", "/post/", "/posts/", "/news/", "/article/", "/articles/")


def looks_like_post(url: str) -> bool:
    """Heuristic: does this internal URL look like a blog/article post? (scope estimate only)."""
    lowered = url.lower()
    return any(marker in lowered for marker in POST_PATH_MARKERS)


ISSUE_LABELS: dict[str, str] = {
    "client_error_internal_urls": "Site URLs returning 'not found' or blocked errors (4xx)",
    "server_error_internal_urls": "Site URLs returning server errors (5xx)",
    "unreachable_internal_urls": "Site URLs that did not respond",
    "client_error_external_urls": "Outbound links returning 'not found' or blocked errors (4xx)",
    "server_error_external_urls": "Outbound links returning server errors (5xx)",
    "non_indexable_internal_urls": "Pages that block Google from indexing them",
    "redirect_chain_internal_urls": "Internal links that pass through a redirect chain (2+ hops)",
    "missing_titles": "Pages missing title tags",
    "duplicate_titles": "Pages with duplicate title tags",
    "missing_meta_descriptions": "Pages missing meta descriptions",
    "duplicate_meta_descriptions": "Pages with duplicate meta descriptions",
    "missing_h1": "Pages missing H1 headings",
    "images_missing_alt": "Images missing alt text",
    "missing_canonicals": "Pages missing canonical URLs",
}

_HIGH_SEVERITY_ISSUES = {
    "client_error_internal_urls",
    "server_error_internal_urls",
}
_MEDIUM_SEVERITY_ISSUES = {
    "unreachable_internal_urls",
    "client_error_external_urls",
    "server_error_external_urls",
    "non_indexable_internal_urls",
    "redirect_chain_internal_urls",
    "missing_titles",
    "duplicate_titles",
    "missing_meta_descriptions",
    "duplicate_meta_descriptions",
    "missing_h1",
    "images_missing_alt",
    "missing_canonicals",
}


def empty_summary() -> JsonDict:
    return {
        "urls_crawled": 0,
        "html_urls_crawled": 0,
        "client_error_internal_urls": 0,
        "server_error_internal_urls": 0,
        "unreachable_internal_urls": 0,
        "client_error_external_urls": 0,
        "server_error_external_urls": 0,
        "non_indexable_internal_urls": 0,
        "redirecting_internal_urls": 0,
        "redirect_chain_internal_urls": 0,
        "missing_titles": 0,
        "duplicate_titles": 0,
        "missing_meta_descriptions": 0,
        "duplicate_meta_descriptions": 0,
        "missing_h1": 0,
        "images_missing_alt": 0,
        "missing_canonicals": 0,
    }


def issue_severity(issue_id: str) -> str:
    if issue_id in _HIGH_SEVERITY_ISSUES:
        return "high"
    if issue_id in _MEDIUM_SEVERITY_ISSUES:
        return "medium"
    return "medium"


def issues_from_summary(
    summary: JsonDict,
    examples: dict[str, list[str]],
    *,
    source: str,
) -> list[JsonDict]:
    issues = []
    for key, label in ISSUE_LABELS.items():
        count = int(summary.get(key) or 0)
        if count <= 0:
            continue
        issues.append(
            {
                "id": key,
                "source": source,
                "severity": issue_severity(key),
                "title": label,
                "count": count,
                "examples": examples.get(key, [])[:10],
            }
        )
    return sorted(issues, key=lambda issue: (-int(issue["count"]), issue["id"]))
