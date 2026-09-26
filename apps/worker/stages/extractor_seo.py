from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

JsonDict = dict[str, Any]

# Useful search-result length ranges (characters). Exposed as facts so the report can
# state the baseline a "length is outside the ideal range" finding was judged against.
TITLE_IDEAL_MIN_LENGTH, TITLE_IDEAL_MAX_LENGTH = 30, 65
META_DESCRIPTION_IDEAL_MIN_LENGTH, META_DESCRIPTION_IDEAL_MAX_LENGTH = 70, 160


def _get_page_value(page: object, key: str, default: Any = None) -> Any:
    if isinstance(page, Mapping):
        return page.get(key, default)
    return getattr(page, key, default)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _pct(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _is_internal_link(page_url: str, href: str) -> bool:
    absolute = urljoin(page_url, href)
    parsed_page = urlparse(page_url)
    parsed_link = urlparse(absolute)
    if parsed_link.scheme not in {"http", "https"}:
        return False
    page_host = (parsed_page.hostname or "").lower().removeprefix("www.")
    link_host = (parsed_link.hostname or "").lower().removeprefix("www.")
    return page_host == link_host


def _meta_content(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)})
    if not isinstance(tag, Tag):
        return None
    return _clean_text(str(tag.get("content", "")))


# schema.org @type buckets that matter for a local service-business audit (P2-12).
_ORGANIZATION_TYPES = {"organization", "corporation"}
_PRODUCT_SERVICE_TYPES = {"product", "service", "offer"}
_REVIEW_TYPES = {"review", "aggregaterating"}


def _schema_categories(types: set[str]) -> JsonDict:
    """Bucket raw schema.org @type values into audit-relevant categories. LocalBusiness has
    many subtypes (GeneralContractor, RoofingContractor, HomeAndConstructionBusiness, ...), so
    it is matched by suffix/substring rather than an exhaustive list."""
    lowered = {t.lower() for t in types}
    local_business = any(
        t == "localbusiness" or t.endswith("business") or "contractor" in t for t in lowered
    )
    return {
        "organization": local_business or bool(lowered & _ORGANIZATION_TYPES),
        "local_business": local_business,
        "breadcrumb": "breadcrumblist" in lowered,
        "faq": "faqpage" in lowered,
        "product_or_service": bool(lowered & _PRODUCT_SERVICE_TYPES),
        "website": "website" in lowered,
        "review": bool(lowered & _REVIEW_TYPES),
    }


def _context_is_schema_org(context: Any) -> bool:
    if isinstance(context, str):
        return "schema.org" in context.lower()
    if isinstance(context, dict):
        return any("schema.org" in str(item).lower() for item in context.values())
    if isinstance(context, list):
        return any(_context_is_schema_org(item) for item in context)
    return False


def _collect_jsonld(soup: BeautifulSoup) -> tuple[list[JsonDict], int, int]:
    """Parse every ``<script type="application/ld+json">`` on the page exactly once. Returns
    ``(flattened dict nodes, block count, invalid block count)``. The flattened node list is
    shared by schema-type detection and the local-business NAP extractor so the JSON-LD is never
    parsed twice."""
    objects: list[JsonDict] = []
    blocks = 0
    invalid = 0

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text("", strip=True)
        if not raw or not raw.strip():
            continue
        blocks += 1
        try:
            walk(json.loads(raw))
        except json.JSONDecodeError:
            # Malformed JSON-LD is silently ignored by search engines — surface it as a fact.
            invalid += 1
    return objects, blocks, invalid


def _extract_schema_types(
    soup: BeautifulSoup,
    jsonld_objects: list[JsonDict],
    json_ld_blocks: int,
    json_ld_invalid_blocks: int,
) -> JsonDict:
    json_ld_types: set[str] = set()
    microdata_types: set[str] = set()
    uses_schema_org = False

    for obj in jsonld_objects:
        if _context_is_schema_org(obj.get("@context")):
            uses_schema_org = True
        item_type = obj.get("@type")
        if isinstance(item_type, str):
            json_ld_types.add(item_type)
        elif isinstance(item_type, list):
            json_ld_types.update(str(entry) for entry in item_type if entry)

    for tag in soup.find_all(attrs={"itemscope": True}):
        if not isinstance(tag, Tag):
            continue
        item_type = tag.get("itemtype")
        if isinstance(item_type, str) and item_type.strip():
            microdata_types.add(item_type.strip())

    # Microdata itemtype is a URL (https://schema.org/LocalBusiness); use the final path
    # segment for category matching alongside the JSON-LD @type names.
    category_types = json_ld_types | {t.rstrip("/").rsplit("/", 1)[-1] for t in microdata_types}

    return {
        "has_schema": bool(json_ld_types or microdata_types),
        "json_ld_blocks": json_ld_blocks,
        "json_ld_invalid_blocks": json_ld_invalid_blocks,
        "json_ld_valid": json_ld_blocks > 0 and json_ld_invalid_blocks == 0,
        "json_ld_types": sorted(json_ld_types),
        "microdata_types": sorted(microdata_types),
        "uses_schema_org_context": uses_schema_org,
        "detected": _schema_categories(category_types),
    }


def _extract_security(soup: BeautifulSoup, url: str) -> JsonDict:
    """HTTPS + mixed-content hygiene (P2-18). Mixed content = a subresource loaded over plain
    http on an https page — browsers block/warn on these and search engines flag them."""
    is_https = url.lower().startswith("https://")
    mixed_content = 0
    if is_https:
        for tag in soup.find_all(["img", "script", "iframe", "audio", "video", "source"]):
            if isinstance(tag, Tag) and str(tag.get("src", "")).strip().lower().startswith(
                "http://"
            ):
                mixed_content += 1
        for tag in soup.find_all("link"):
            if not isinstance(tag, Tag):
                continue
            rel = tag.get("rel") or []
            rel_values = rel if isinstance(rel, list) else [rel]
            is_stylesheet = any("stylesheet" in str(value).lower() for value in rel_values)
            href = str(tag.get("href", "")).strip().lower()
            if is_stylesheet and href.startswith("http://"):
                mixed_content += 1
    return {"https": is_https, "mixed_content_count": mixed_content}


# AEO / answer-engine content readiness (P2-13). Deterministic content-structure signals an
# answer engine (and a human reader) relies on to extract and cite a page — all measured from one
# rendered DOM, no LLM and no extra fetch. Schema-based AEO signals (FAQ/HowTo/LocalBusiness) are
# already covered by _extract_schema_types; this focuses on the on-page *structure* (heading
# outline, question headings, scannable lists/tables). Framed as content-structure hygiene that
# helps both readers and machines parse the page, NOT a guaranteed "AI citation" lever.
_INTERROGATIVE_WORDS = frozenset(
    {
        "who",
        "what",
        "when",
        "where",
        "why",
        "how",
        "is",
        "are",
        "do",
        "does",
        "can",
        "could",
        "should",
        "will",
        "which",
    }
)


def _is_question_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first_word = stripped.split(maxsplit=1)[0].lower().strip(":,.;")
    return first_word in _INTERROGATIVE_WORDS


def _heading_levels(soup: BeautifulSoup) -> list[int]:
    return [
        int(tag.name[1])
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        if isinstance(tag, Tag) and tag.get_text(strip=True)
    ]


def _heading_hierarchy_ok(levels: list[int]) -> bool:
    """A clean outline: exactly one H1, the first heading is that H1, and no level is skipped on
    the way down (H2 -> H4 is a skip). Decreases (H3 -> H2) are always allowed."""
    if not levels or levels.count(1) != 1 or levels[0] != 1:
        return False
    previous = levels[0]
    for level in levels[1:]:
        if level - previous > 1:
            return False
        previous = level
    return True


def _is_chrome(tag: Tag) -> bool:
    """True when the tag sits inside site chrome (nav/header/footer or role=navigation), so a
    menu's <ul> is never miscounted as scannable body content."""
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        if (parent.name or "").lower() in {"nav", "header", "footer"}:
            return True
        if str(parent.get("role", "")).strip().lower() == "navigation":
            return True
    return False


def _main_content_root(soup: BeautifulSoup) -> Tag | BeautifulSoup:
    for selector in ("main", "article"):
        node = soup.find(selector)
        if isinstance(node, Tag):
            return node
    return soup.body if isinstance(soup.body, Tag) else soup


def _extract_aeo(soup: BeautifulSoup) -> JsonDict:
    levels = _heading_levels(soup)

    subheading_count = 0
    question_heading_count = 0
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if not isinstance(tag, Tag):
            continue
        text = tag.get_text(" ", strip=True)
        if not text:
            continue
        subheading_count += 1
        if _is_question_heading(text):
            question_heading_count += 1

    root = _main_content_root(soup)
    content_list_count = 0
    for lst in root.find_all(["ul", "ol"]):
        if not isinstance(lst, Tag) or _is_chrome(lst):
            continue
        # A genuine content list has real items; a 1-2 item list is usually layout/menu chrome.
        if len(lst.find_all("li", recursive=False)) >= 3:
            content_list_count += 1
    content_table_count = 0
    for table in root.find_all("table"):
        if not isinstance(table, Tag) or _is_chrome(table):
            continue
        # A data table has headers or more than a single row; drop one-row layout tables.
        if table.find("th") is not None or len(table.find_all("tr")) > 1:
            content_table_count += 1
    definition_list_count = sum(
        1 for dl in root.find_all("dl") if isinstance(dl, Tag) and not _is_chrome(dl)
    )

    return {
        "heading_hierarchy_ok": _heading_hierarchy_ok(levels),
        "subheading_count": subheading_count,
        "question_heading_count": question_heading_count,
        "content_list_count": content_list_count,
        "content_table_count": content_table_count,
        "definition_list_count": definition_list_count,
        "has_extractable_structure": (
            content_list_count + content_table_count + definition_list_count
        )
        > 0,
    }


# Local-SEO fundamentals (P2-17). Machine-readable NAP completeness in LocalBusiness/Organization
# JSON-LD (name + postal address + telephone), a declared service area, a Google Business Profile /
# Maps link, and a visible <address> block — the local-ranking fundamentals a directory or AI
# assistant uses to resolve the business to a place. The JSON-LD is parsed once (shared with
# schema-type detection); visible phone/email for the *conversion* lens stays in extractor_uxui so
# the two aren't duplicated.
_BUSINESS_SCHEMA_TYPES = {"localbusiness", "organization", "corporation"}
_MAP_OR_GBP_HINTS = (
    "google.com/maps",
    "maps.google.",
    "g.page",
    "goo.gl/maps",
    "maps.app.goo.gl",
    "business.google.com",
)


def _is_business_schema_type(item_type: Any) -> bool:
    values = item_type if isinstance(item_type, list) else [item_type]
    for value in values:
        lowered = str(value).lower()
        if (
            lowered in _BUSINESS_SCHEMA_TYPES
            or lowered.endswith("business")
            or "contractor" in lowered
        ):
            return True
    return False


def _is_nonempty(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict):
        return bool(value)
    return value is not None


def _schema_address_present(address: Any) -> bool:
    if isinstance(address, str):
        return bool(address.strip())
    if isinstance(address, dict):
        return any(
            _is_nonempty(address.get(field))
            for field in ("streetAddress", "addressLocality", "addressRegion", "postalCode")
        )
    if isinstance(address, list):
        return any(_schema_address_present(item) for item in address)
    return False


def _extract_local(soup: BeautifulSoup, jsonld_objects: list[JsonDict]) -> JsonDict:
    business = next(
        (obj for obj in jsonld_objects if _is_business_schema_type(obj.get("@type"))),
        None,
    )
    schema_name = business is not None and _is_nonempty(business.get("name"))
    schema_address = business is not None and _schema_address_present(business.get("address"))
    schema_phone = business is not None and _is_nonempty(business.get("telephone"))
    schema_area_served = business is not None and (
        _is_nonempty(business.get("areaServed")) or _is_nonempty(business.get("geo"))
    )

    has_address_element = soup.find("address") is not None
    has_map_or_gbp_link = any(
        any(hint in str(tag.get("href", "")).lower() for hint in _MAP_OR_GBP_HINTS)
        for tag in soup.find_all("a", href=True)
        if isinstance(tag, Tag)
    )

    return {
        "has_business_schema": business is not None,
        "schema_name": bool(schema_name),
        "schema_address": bool(schema_address),
        "schema_phone": bool(schema_phone),
        "schema_area_served": bool(schema_area_served),
        "nap_schema_complete": bool(schema_name and schema_address and schema_phone),
        "has_address_element": has_address_element,
        "has_map_or_gbp_link": has_map_or_gbp_link,
    }


# Static-HTML accessibility screen (P2-15). A DETERMINISTIC subset of WCAG A/AA checks computed
# from the stored server-rendered markup only — no browser, no JavaScript, no computed CSS, no
# extra fetch (so axe-core, which needs a live DOM, is deliberately NOT used; see docs). Only the
# low-false-positive, structural/attribute-level checks are included, and the high-value naming
# checks implement full accessible-name precedence + an aria-hidden subtree carve-out so they
# don't false-positive on icon links, wrapping labels, or decorative content. Render-dependent
# criteria (colour contrast, focus order, reflow, computed ARIA) are out of scope by design.
_LABELABLE_SKIP_INPUT_TYPES = {"hidden", "submit", "reset", "button", "image"}
_BUTTON_INPUT_TYPES = {"submit", "reset", "button", "image"}
_ID_REFERENCING_ATTRS = (
    "for",
    "aria-labelledby",
    "aria-describedby",
    "aria-controls",
    "aria-owns",
    "headers",
)


def _aria_hidden(tag: Tag) -> bool:
    return str(tag.get("aria-hidden", "")).strip().lower() == "true"


def _accessible_inner_text(element: Tag) -> str:
    """Visible text of an element with aria-hidden='true' subtrees removed, so a decorative icon's
    stray characters never count as (or mask) an accessible name. sr-only text stays in the markup,
    so this conservatively under-counts rather than over-flags."""
    clone = BeautifulSoup(str(element), "html.parser")
    for hidden in clone.find_all(attrs={"aria-hidden": "true"}):
        if isinstance(hidden, Tag):
            hidden.decompose()
    return clone.get_text(" ", strip=True)


def _labelledby_resolves(soup: BeautifulSoup, value: str) -> bool:
    """An aria-labelledby/aria-describedby names a control only if every referenced id resolves to
    an in-page element that itself carries text."""
    ids = value.split()
    if not ids:
        return False
    for ref_id in ids:
        target = soup.find(id=ref_id)
        if not isinstance(target, Tag) or not target.get_text(strip=True):
            return False
    return True


def _has_aria_or_title_name(soup: BeautifulSoup, element: Tag) -> bool:
    if str(element.get("aria-label", "")).strip():
        return True
    labelledby = str(element.get("aria-labelledby", "")).strip()
    if labelledby and _labelledby_resolves(soup, labelledby):
        return True
    return bool(str(element.get("title", "")).strip())


def _has_accessible_name(soup: BeautifulSoup, element: Tag) -> bool:
    """Accessible-name precedence for a link/button: visible text (minus aria-hidden), aria-label,
    a resolving aria-labelledby, title, or a descendant image/SVG with its own text alternative."""
    if _accessible_inner_text(element):
        return True
    if _has_aria_or_title_name(soup, element):
        return True
    for img in element.find_all("img"):
        if isinstance(img, Tag) and str(img.get("alt", "")).strip():
            return True
    for svg in element.find_all("svg"):
        if not isinstance(svg, Tag):
            continue
        title = svg.find("title")
        if isinstance(title, Tag) and title.get_text(strip=True):
            return True
    return False


def _form_control_is_labeled(soup: BeautifulSoup, control: Tag) -> bool:
    for parent in control.parents:
        if isinstance(parent, Tag) and parent.name == "label" and parent.get_text(strip=True):
            return True
    control_id = str(control.get("id", "")).strip()
    if control_id:
        for label in soup.find_all("label", attrs={"for": control_id}):
            if isinstance(label, Tag) and label.get_text(strip=True):
                return True
    # Placeholder is intentionally NOT a valid label (matches axe / WCAG 3.3.2).
    return _has_aria_or_title_name(soup, control)


def _button_is_named(soup: BeautifulSoup, element: Tag) -> bool:
    if element.name == "input":
        input_type = str(element.get("type", "")).lower()
        if input_type in {"submit", "reset"}:
            return True  # the user agent supplies default visible text
        if input_type == "image" and str(element.get("alt", "")).strip():
            return True
        if input_type == "button" and str(element.get("value", "")).strip():
            return True
        return _has_aria_or_title_name(soup, element)
    return _has_accessible_name(soup, element)


def _viewport_blocks_zoom(soup: BeautifulSoup) -> bool | None:
    metas = [
        tag
        for tag in soup.find_all("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
        if isinstance(tag, Tag)
    ]
    if not metas:
        return None  # no viewport meta => the zoom check is inapplicable (rule skips)
    content = str(metas[-1].get("content", "")).lower()  # if several, the browser uses the last
    props: dict[str, str] = {}
    for part in content.split(","):
        key, sep, value = part.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    if props.get("user-scalable") in {"no", "0"}:
        return True
    max_scale = props.get("maximum-scale")
    if max_scale is not None:
        try:
            if float(max_scale) < 2:
                return True
        except ValueError:
            pass
    return False


def _count_positive_tabindex(soup: BeautifulSoup) -> int:
    count = 0
    for tag in soup.find_all(attrs={"tabindex": True}):
        if not isinstance(tag, Tag):
            continue
        try:
            if int(str(tag.get("tabindex")).strip()) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _duplicate_referenced_ids(soup: BeautifulSoup) -> tuple[int, int]:
    """(# of ids referenced by label/ARIA, # of those that are duplicated). WCAG 2.2 removed
    generic duplicate-id (4.1.1 Parsing); only a duplicated id that a reference points at still
    breaks the association (axe duplicate-id-aria, 4.1.2)."""
    id_counts: dict[str, int] = {}
    for tag in soup.find_all(attrs={"id": True}):
        if isinstance(tag, Tag):
            tag_id = str(tag.get("id", "")).strip()
            if tag_id:
                id_counts[tag_id] = id_counts.get(tag_id, 0) + 1
    referenced: set[str] = set()
    for attr in _ID_REFERENCING_ATTRS:
        for tag in soup.find_all(attrs={attr: True}):
            if not isinstance(tag, Tag):
                continue
            value = tag.get(attr)
            tokens = value if isinstance(value, list) else str(value).split()
            for token in tokens:
                cleaned = str(token).strip()
                if cleaned:
                    referenced.add(cleaned)
    duplicated = sum(1 for ref in referenced if id_counts.get(ref, 0) > 1)
    return len(referenced), duplicated


def _extract_a11y(soup: BeautifulSoup) -> JsonDict:
    html_tag = soup.find("html")
    has_lang = isinstance(html_tag, Tag) and bool(
        str(html_tag.get("lang", "")).strip() or str(html_tag.get("xml:lang", "")).strip()
    )
    has_main_landmark = (
        soup.find("main") is not None or soup.find(attrs={"role": "main"}) is not None
    )

    form_controls = [
        control
        for control in soup.find_all(["input", "select", "textarea"])
        if isinstance(control, Tag)
        and str(control.get("type", "")).lower() not in _LABELABLE_SKIP_INPUT_TYPES
    ]
    unlabeled_form_controls = sum(
        1 for control in form_controls if not _form_control_is_labeled(soup, control)
    )

    links = [
        anchor
        for anchor in soup.find_all("a", href=True)
        if isinstance(anchor, Tag)
        and not _aria_hidden(anchor)
        and not str(anchor.get("href", "")).strip().startswith("#")
    ]
    empty_links = sum(1 for anchor in links if not _has_accessible_name(soup, anchor))

    buttons = [
        tag for tag in soup.find_all("button") if isinstance(tag, Tag) and not _aria_hidden(tag)
    ]
    buttons += [
        tag
        for tag in soup.find_all("input")
        if isinstance(tag, Tag)
        and str(tag.get("type", "")).lower() in _BUTTON_INPUT_TYPES
        and not _aria_hidden(tag)
    ]
    buttons += [
        tag
        for tag in soup.find_all(attrs={"role": "button"})
        if isinstance(tag, Tag) and tag.name not in {"button", "input"} and not _aria_hidden(tag)
    ]
    empty_buttons = sum(1 for button in buttons if not _button_is_named(soup, button))

    referenced_id_count, duplicate_referenced_ids = _duplicate_referenced_ids(soup)

    return {
        "has_lang": has_lang,
        "has_main_landmark": has_main_landmark,
        "viewport_blocks_zoom": _viewport_blocks_zoom(soup),
        "positive_tabindex_count": _count_positive_tabindex(soup),
        "form_control_count": len(form_controls),
        "unlabeled_form_controls": unlabeled_form_controls,
        "link_count": len(links),
        "empty_links": empty_links,
        "button_count": len(buttons),
        "empty_buttons": empty_buttons,
        "referenced_id_count": referenced_id_count,
        "duplicate_referenced_ids": duplicate_referenced_ids,
    }


def extract_seo_facts_for_page(page: object) -> JsonDict:
    url = str(_get_page_value(page, "final_url", None) or _get_page_value(page, "url", ""))
    html = str(_get_page_value(page, "html", "") or "")
    soup = BeautifulSoup(html, "html.parser")
    jsonld_objects, json_ld_blocks, json_ld_invalid_blocks = _collect_jsonld(soup)

    title_text = _clean_text(soup.title.string if soup.title and soup.title.string else None)
    description = _meta_content(soup, "description")
    canonical_tag = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    canonical = None
    if isinstance(canonical_tag, Tag):
        canonical_href = str(canonical_tag.get("href", "")).strip()
        canonical = urljoin(url, canonical_href) if canonical_href else None

    headings: dict[str, list[str]] = {}
    for level in range(1, 7):
        key = f"h{level}"
        headings[key] = [
            text
            for tag in soup.find_all(key)
            if (text := _clean_text(tag.get_text(" ", strip=True)))
        ]

    anchors = [tag for tag in soup.find_all("a", href=True) if isinstance(tag, Tag)]
    empty_links = 0
    internal_links = 0
    external_links = 0
    for anchor in anchors:
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            empty_links += 1
        elif _is_internal_link(url, href):
            internal_links += 1
        else:
            external_links += 1

    images = [tag for tag in soup.find_all("img") if isinstance(tag, Tag)]
    images_with_alt = sum(1 for image in images if _clean_text(str(image.get("alt", ""))))
    robots_content = (_meta_content(soup, "robots") or "").lower()
    robots_directives = {
        directive.strip() for directive in re.split(r"[,\s]+", robots_content) if directive.strip()
    }

    return {
        "url": url,
        "status_code": _get_page_value(page, "status_code", None),
        "title": {
            "text": title_text,
            "length": len(title_text or ""),
            "present": title_text is not None,
            "is_reasonable_length": (
                TITLE_IDEAL_MIN_LENGTH <= len(title_text or "") <= TITLE_IDEAL_MAX_LENGTH
            ),
            "ideal_min_length": TITLE_IDEAL_MIN_LENGTH,
            "ideal_max_length": TITLE_IDEAL_MAX_LENGTH,
        },
        "meta_description": {
            "text": description,
            "length": len(description or ""),
            "present": description is not None,
            "is_reasonable_length": (
                META_DESCRIPTION_IDEAL_MIN_LENGTH
                <= len(description or "")
                <= META_DESCRIPTION_IDEAL_MAX_LENGTH
            ),
            "ideal_min_length": META_DESCRIPTION_IDEAL_MIN_LENGTH,
            "ideal_max_length": META_DESCRIPTION_IDEAL_MAX_LENGTH,
        },
        "canonical": canonical,
        "headings": {
            "counts": {key: len(values) for key, values in headings.items()},
            "h1_texts": headings["h1"],
            "h2_texts": headings["h2"],
        },
        "links": {
            "total": len(anchors),
            "internal_count": internal_links,
            "external_count": external_links,
            "empty_or_non_http_count": empty_links,
        },
        "images": {
            "total": len(images),
            "with_alt": images_with_alt,
            "missing_alt": len(images) - images_with_alt,
            "alt_coverage_pct": _pct(images_with_alt, len(images)),
        },
        "schema": _extract_schema_types(
            soup, jsonld_objects, json_ld_blocks, json_ld_invalid_blocks
        ),
        "security": _extract_security(soup, url),
        "aeo": _extract_aeo(soup),
        "local": _extract_local(soup, jsonld_objects),
        "a11y": _extract_a11y(soup),
        "robots": {
            "directives": sorted(robots_directives),
            "noindex": "noindex" in robots_directives,
            "nofollow": "nofollow" in robots_directives,
        },
    }


def extract_seo_facts(pages: Iterable[object]) -> JsonDict:
    page_facts = [extract_seo_facts_for_page(page) for page in pages]
    analyzed = len(page_facts)
    image_total = sum(page["images"]["total"] for page in page_facts)
    images_with_alt = sum(page["images"]["with_alt"] for page in page_facts)

    return {
        "status": "complete" if page_facts else "empty",
        "pages_analyzed": analyzed,
        "summary": {
            "titles_present_pct": _pct(
                sum(1 for page in page_facts if page["title"]["present"]),
                analyzed,
            ),
            "meta_descriptions_present_pct": _pct(
                sum(1 for page in page_facts if page["meta_description"]["present"]),
                analyzed,
            ),
            "h1_present_pct": _pct(
                sum(1 for page in page_facts if page["headings"]["counts"]["h1"] == 1),
                analyzed,
            ),
            "pages_with_schema": sum(1 for page in page_facts if page["schema"]["has_schema"]),
            "pages_with_organization_schema": sum(
                1 for page in page_facts if page["schema"]["detected"]["organization"]
            ),
            "pages_with_invalid_json_ld": sum(
                1 for page in page_facts if page["schema"]["json_ld_invalid_blocks"] > 0
            ),
            "has_local_business_schema": any(
                page["schema"]["detected"]["local_business"] for page in page_facts
            ),
            "has_breadcrumb_schema": any(
                page["schema"]["detected"]["breadcrumb"] for page in page_facts
            ),
            "image_alt_coverage_pct": _pct(images_with_alt, image_total),
            "total_internal_links": sum(page["links"]["internal_count"] for page in page_facts),
            "total_external_links": sum(page["links"]["external_count"] for page in page_facts),
            "noindex_pages": sum(1 for page in page_facts if page["robots"]["noindex"]),
            "all_pages_https": all(page["security"]["https"] for page in page_facts),
            "pages_with_mixed_content": sum(
                1 for page in page_facts if page["security"]["mixed_content_count"] > 0
            ),
            "total_mixed_content": sum(
                page["security"]["mixed_content_count"] for page in page_facts
            ),
            "all_pages_heading_hierarchy_ok": all(
                page["aeo"]["heading_hierarchy_ok"] for page in page_facts
            ),
            "total_question_headings": sum(
                page["aeo"]["question_heading_count"] for page in page_facts
            ),
            "has_extractable_structure": any(
                page["aeo"]["has_extractable_structure"] for page in page_facts
            ),
            "has_complete_nap_schema": any(
                page["local"]["nap_schema_complete"] for page in page_facts
            ),
            "has_service_area_markup": any(
                page["local"]["schema_area_served"] for page in page_facts
            ),
            "has_map_or_gbp_link": any(page["local"]["has_map_or_gbp_link"] for page in page_facts),
            "has_visible_address": any(page["local"]["has_address_element"] for page in page_facts),
            # Accessibility (P2-15). Element-dependent counts return None (rule skips, rescales)
            # when no applicable element exists, so a site with no forms/buttons is not vacuously
            # credited; lang and main-landmark apply to every page so they are never gated.
            "all_pages_have_lang": all(page["a11y"]["has_lang"] for page in page_facts),
            "all_pages_have_main_landmark": all(
                page["a11y"]["has_main_landmark"] for page in page_facts
            ),
            "viewport_allows_zoom": (
                None
                if all(page["a11y"]["viewport_blocks_zoom"] is None for page in page_facts)
                else not any(page["a11y"]["viewport_blocks_zoom"] is True for page in page_facts)
            ),
            "total_positive_tabindex": sum(
                page["a11y"]["positive_tabindex_count"] for page in page_facts
            ),
            "unlabeled_form_controls": (
                sum(page["a11y"]["unlabeled_form_controls"] for page in page_facts)
                if sum(page["a11y"]["form_control_count"] for page in page_facts) > 0
                else None
            ),
            "empty_links": (
                sum(page["a11y"]["empty_links"] for page in page_facts)
                if sum(page["a11y"]["link_count"] for page in page_facts) > 0
                else None
            ),
            "empty_buttons": (
                sum(page["a11y"]["empty_buttons"] for page in page_facts)
                if sum(page["a11y"]["button_count"] for page in page_facts) > 0
                else None
            ),
            "duplicate_referenced_ids": (
                sum(page["a11y"]["duplicate_referenced_ids"] for page in page_facts)
                if sum(page["a11y"]["referenced_id_count"] for page in page_facts) > 0
                else None
            ),
        },
        "pages": page_facts,
    }
