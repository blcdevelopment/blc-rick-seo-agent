"""Website Scope panel — 'what the whole site consists of' (Dru's request)."""

from apps.worker.stages.report_payload import _website_scope
from apps.worker.stages.site_health import _looks_like_post


def test_scope_surfaces_discovered_counts() -> None:
    external = {
        "technical_crawl": {
            "summary": {
                "discovered_internal_urls": 513,
                "discovered_blog_posts": 120,
                "sitemap_url_count": 300,
                "discovered_external_urls": 90,
            }
        }
    }
    crawled = {"summary": {"successful_pages": 10}}
    seo = {"pages": [{"images": {"total": 40}}, {"images": {"total": 25}}]}
    scope = _website_scope(external, crawled, seo)
    assert scope == {
        "pages_discovered": 513,
        "pages_analyzed": 10,
        "blog_posts": 120,
        "sitemap_entries": 300,
        "outbound_links": 90,
        "images": 65,
    }


def test_scope_is_none_when_nothing_known() -> None:
    # e.g. a failed crawl with no data -> the section simply doesn't render.
    assert _website_scope({}, {}, {}) is None


def test_scope_partial_when_only_crawl_ran() -> None:
    # Sweep skipped (no discovered counts) but pages were rendered -> still shows what it can.
    scope = _website_scope({}, {"summary": {"successful_pages": 8}}, {})
    assert scope["pages_analyzed"] == 8
    assert scope["pages_discovered"] is None


def test_post_url_heuristic() -> None:
    assert _looks_like_post("https://site.com/blog/new-home-costs/")
    assert _looks_like_post("https://site.com/news/2026/update")
    assert not _looks_like_post("https://site.com/services/")
    assert not _looks_like_post("https://site.com/contact-us/")
