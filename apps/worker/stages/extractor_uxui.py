from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from bs4 import BeautifulSoup, Tag

JsonDict = dict[str, Any]

CTA_VERBS = {
    "book",
    "call",
    "contact",
    "estimate",
    "get",
    "quote",
    "request",
    "schedule",
    "start",
    "talk",
}
BUTTON_TOKENS = {"btn", "button", "cta", "primary", "estimate", "quote"}
TRUST_PATTERNS = (
    r"testimonial",
    r"review",
    r"rating",
    r"trusted by",
    r"award",
    r"certified",
    r"licensed",
    r"insured",
    r"\bbbb\b",
    r"houzz",
    r"google reviews?",
    r"\u2605",
)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _get_page_value(page: object, key: str, default: Any = None) -> Any:
    if isinstance(page, Mapping):
        return page.get(key, default)
    return getattr(page, key, default)


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _visible_text(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(clone.get_text(" ", strip=True).split())


def _tag_tokens(tag: Tag) -> str:
    values: list[str] = []
    for attr in ("id", "class", "role", "aria-label"):
        raw = tag.get(attr, "")
        if isinstance(raw, list):
            values.extend(str(value) for value in raw)
        else:
            values.append(str(raw))
    return " ".join(values).lower()


def _has_ancestor_signal(tag: Tag, tokens: set[str]) -> bool:
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        parent_name = (parent.name or "").lower()
        if parent_name in tokens:
            return True
        if any(token in _tag_tokens(parent) for token in tokens):
            return True
    return False


def _is_required_field(field: Tag) -> bool:
    if field.has_attr("required"):
        return True
    # `aria-required` is a string attribute: only "true" marks the field required.
    # A bare truthiness check would wrongly count `aria-required="false"`.
    return str(field.get("aria-required", "")).strip().lower() == "true"


def _candidate_text(tag: Tag) -> str | None:
    if tag.name == "input":
        return _clean_text(str(tag.get("value") or tag.get("aria-label") or ""))
    return _clean_text(str(tag.get("aria-label") or tag.get_text(" ", strip=True)))


def _extract_ctas(soup: BeautifulSoup) -> list[JsonDict]:
    ctas: list[JsonDict] = []
    selectors = ["a[href]", "button", "input[type='submit']", "input[type='button']"]
    for order, tag in enumerate(soup.select(",".join(selectors))):
        if not isinstance(tag, Tag):
            continue
        text = _candidate_text(tag)
        if not text:
            continue

        lowered = text.lower()
        tokens = _tag_tokens(tag)
        matched_verbs = sorted(verb for verb in CTA_VERBS if re.search(rf"\b{verb}\b", lowered))
        styled_as_button = any(token in tokens for token in BUTTON_TOKENS) or tag.name == "button"
        hero_or_header = _has_ancestor_signal(tag, {"hero", "header", "banner"})
        is_phone_link = tag.name == "a" and str(tag.get("href", "")).startswith("tel:")
        if not (matched_verbs or styled_as_button or is_phone_link):
            continue

        score = 0
        signals: list[str] = []
        if matched_verbs:
            score += 5
            signals.append("action_verb")
        if styled_as_button:
            score += 3
            signals.append("button_styling")
        if hero_or_header:
            score += 2
            signals.append("hero_or_header")
        if is_phone_link:
            score += 1
            signals.append("phone_link")

        ctas.append(
            {
                "text": text[:120],
                "element": tag.name,
                "href": str(tag.get("href", "")) or None,
                "score": score,
                "signals": signals,
                "above_fold_heuristic": order < 12 or hero_or_header,
            }
        )

    return sorted(ctas, key=lambda cta: (-cta["score"], cta["text"]))


# Known lead-capture embeds: the provider's loader script or iframe src is present in the
# initial HTML even when the <form> element only mounts later (click-triggered popups,
# lazy-loaded iframes) — the exact pattern form builders produce, including the
# LeadConnector/GoHighLevel stack BLC's own sites use. Signature-scanning the stored HTML
# detects "lead capture present" with zero extra network (Wappalyzer-style).
_EMBED_PROVIDER_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "leadconnector",
        (
            "api.leadconnectorhq.com/widget/form",
            "api.leadconnectorhq.com/widget/survey",
            "api.leadconnectorhq.com/widget/booking",
            "link.msgsndr.com/js/form_embed.js",
            "widgets.leadconnectorhq.com",
        ),
    ),
    ("hubspot", ("js.hsforms.net", "js.hs-scripts.com", "hbspt.forms.create", ".hsforms.com")),
    (
        "typeform",
        (
            "embed.typeform.com",
            "data-tf-widget",
            "data-tf-popup",
            "data-tf-live",
            "form.typeform.com/to/",
        ),
    ),
    ("jotform", ("form.jotform.com", "jotform.com/jsform", "jotformiframe")),
    (
        "calendly",
        (
            "assets.calendly.com/assets/external/widget.js",
            "calendly-inline-widget",
            "calendly-badge-widget",
        ),
    ),
    ("gravity_forms", ("gform_wrapper", "/wp-content/plugins/gravityforms/")),
    ("intercom", ("widget.intercom.io/widget",)),
    ("drift", ("js.driftt.com/include",)),
)


def _detect_embedded_providers(html: str) -> list[str]:
    lowered = html.lower()
    return [
        name
        for name, tokens in _EMBED_PROVIDER_SIGNATURES
        if any(token in lowered for token in tokens)
    ]


def _extract_forms(soup: BeautifulSoup) -> list[JsonDict]:
    forms: list[JsonDict] = []
    for form in soup.find_all("form"):
        if not isinstance(form, Tag):
            continue
        fields = form.find_all(["input", "textarea", "select"])
        required_fields = [
            field for field in fields if isinstance(field, Tag) and _is_required_field(field)
        ]
        submit = form.find(["button", "input"], attrs={"type": re.compile("submit", re.I)})
        forms.append(
            {
                "field_count": len(fields),
                "required_field_count": len(required_fields),
                "has_submit": submit is not None,
                "action": str(form.get("action", "")) or None,
            }
        )
    return forms


def _has_contact_page_link(soup: BeautifulSoup) -> bool:
    """A link to a contact page counts as a low-pressure contact path even when the site
    deliberately hides raw email addresses (a common anti-spam choice)."""
    for tag in soup.find_all("a", href=True):
        if not isinstance(tag, Tag):
            continue
        href = str(tag.get("href", "")).lower()
        if href.startswith(("mailto:", "tel:")):
            continue
        text = " ".join(tag.get_text(" ", strip=True).lower().split())
        if "contact" in href or text in {"contact", "contact us", "get in touch"}:
            return True
    return False


def _extract_contact_signals(soup: BeautifulSoup, visible_text: str) -> JsonDict:
    hrefs = " ".join(
        str(tag.get("href", "")) for tag in soup.find_all("a", href=True) if isinstance(tag, Tag)
    )
    phones = sorted(set(PHONE_RE.findall(f"{visible_text} {hrefs}")))
    emails = sorted(set(EMAIL_RE.findall(f"{visible_text} {hrefs}")))
    return {
        "phone_numbers": phones[:10],
        "emails": emails[:10],
        "has_phone": bool(phones),
        "has_email": bool(emails),
        "has_contact_page_link": _has_contact_page_link(soup),
        "tel_links": sum(
            1
            for tag in soup.find_all("a", href=True)
            if isinstance(tag, Tag) and str(tag.get("href", "")).startswith("tel:")
        ),
        "mailto_links": sum(
            1
            for tag in soup.find_all("a", href=True)
            if isinstance(tag, Tag) and str(tag.get("href", "")).startswith("mailto:")
        ),
    }


def _extract_trust_signals(soup: BeautifulSoup, visible_text: str) -> JsonDict:
    lowered = visible_text.lower()
    matches = sorted(
        pattern for pattern in TRUST_PATTERNS if re.search(pattern, lowered, flags=re.IGNORECASE)
    )
    trust_images = [
        str(image.get("alt", ""))
        for image in soup.find_all("img")
        if isinstance(image, Tag)
        and re.search(r"award|badge|certified|bbb|houzz|review", str(image.get("alt", "")), re.I)
    ]
    return {
        "count": len(matches) + len(trust_images),
        "matched_patterns": matches,
        "image_alt_matches": sorted(set(filter(None, trust_images)))[:10],
        "has_trust_signals": bool(matches or trust_images),
    }


def extract_uxui_facts_for_page(page: object) -> JsonDict:
    url = str(_get_page_value(page, "final_url", None) or _get_page_value(page, "url", ""))
    html = str(_get_page_value(page, "html", "") or "")
    soup = BeautifulSoup(html, "html.parser")
    visible_text = _visible_text(soup)
    ctas = _extract_ctas(soup)
    forms = _extract_forms(soup)
    contact = _extract_contact_signals(soup, visible_text)
    trust = _extract_trust_signals(soup, visible_text)
    embedded_providers = _detect_embedded_providers(html)
    # Populated by the crawler's runtime frame pass (forms living inside iframes are
    # invisible to page HTML); 0 for stored results that predate it.
    frame_form_count = int(_get_page_value(page, "frame_form_count", 0) or 0)
    frame_form_field_count = int(_get_page_value(page, "frame_form_field_count", 0) or 0)
    if forms:
        form_detected = "static_form"
    elif frame_form_count:
        form_detected = "runtime_iframe_form"
    elif embedded_providers:
        form_detected = "provider_embed"
    else:
        form_detected = "none"
    # The homepage field-count rule judges a form's SIZE (1-5 fields ideal). We only ever
    # assert a size we actually measured: a positive count of real static inputs, or the
    # crawler's frame pass count. A count of 0 never means "a usable form with zero fields"
    # — it means we could not measure the form (a JS/popup form that loads on click, an
    # embed, a lazy iframe, or no form at all). In every such case the fact is None so the
    # rule SKIPS (rescales) instead of printing a false "0 homepage form fields". Whether a
    # form EXISTS at all is judged separately by uxui.forms.present (which credits embeds
    # and frame forms), so skipping the size check here never hides a missing form.
    static_field_count = sum(form["field_count"] for form in forms)  # 0 when there are no forms
    total_field_count: int | None
    if static_field_count >= 1:
        total_field_count = static_field_count
    elif frame_form_field_count >= 1:
        total_field_count = frame_form_field_count
    else:
        total_field_count = None

    nav_links = [
        anchor
        for nav in soup.find_all("nav")
        if isinstance(nav, Tag)
        for anchor in nav.find_all("a", href=True)
        if isinstance(anchor, Tag)
    ]
    footer_links = [
        anchor
        for footer in soup.find_all("footer")
        if isinstance(footer, Tag)
        for anchor in footer.find_all("a", href=True)
        if isinstance(anchor, Tag)
    ]

    return {
        "url": url,
        "ctas": {
            "count": len(ctas),
            "primary": ctas[0] if ctas else None,
            "above_fold_count": sum(1 for cta in ctas if cta["above_fold_heuristic"]),
            "items": ctas[:20],
        },
        "forms": {
            "count": len(forms),
            "total_field_count": total_field_count,
            "items": forms,
            "embedded_providers": embedded_providers,
            "frame_form_count": frame_form_count,
            "form_detected": form_detected,
        },
        "contact": contact,
        "trust_signals": trust,
        "navigation": {
            "has_nav": bool(nav_links),
            "nav_link_count": len(nav_links),
            "footer_link_count": len(footer_links),
        },
        "content": {
            "visible_text_length": len(visible_text),
            "has_substantial_copy": len(visible_text) >= 500,
        },
        "lead_capture": {
            "has_cta": bool(ctas),
            "has_form": form_detected != "none",
            "has_direct_contact": contact["has_phone"] or contact["has_email"],
            # A visitor who isn't ready to call still has an easy first step: a visible
            # email, a contact page link, or a lead form (static or embedded/popup).
            "has_low_pressure_path": (
                contact["has_email"] or contact["has_contact_page_link"] or form_detected != "none"
            ),
        },
    }


def extract_uxui_facts(pages: Iterable[object]) -> JsonDict:
    page_facts = [extract_uxui_facts_for_page(page) for page in pages]
    return {
        "status": "complete" if page_facts else "empty",
        "pages_analyzed": len(page_facts),
        "summary": {
            "total_ctas": sum(page["ctas"]["count"] for page in page_facts),
            "pages_with_primary_cta": sum(1 for page in page_facts if page["ctas"]["primary"]),
            "above_fold_ctas": sum(page["ctas"]["above_fold_count"] for page in page_facts),
            "total_forms": sum(page["forms"]["count"] for page in page_facts),
            "pages_with_forms": sum(1 for page in page_facts if page["forms"]["count"]),
            # Static forms AND detected embedded/popup/iframe forms — the scored fact,
            # so sites whose forms live in popups or lazy iframes get honest credit.
            "pages_with_form_capture": sum(
                1 for page in page_facts if page["forms"]["form_detected"] != "none"
            ),
            "pages_with_phone": sum(1 for page in page_facts if page["contact"]["has_phone"]),
            "pages_with_email": sum(1 for page in page_facts if page["contact"]["has_email"]),
            "pages_with_contact_path": sum(
                1 for page in page_facts if page["lead_capture"]["has_low_pressure_path"]
            ),
            "pages_with_trust_signals": sum(
                1 for page in page_facts if page["trust_signals"]["has_trust_signals"]
            ),
            "total_trust_signals": sum(page["trust_signals"]["count"] for page in page_facts),
        },
        "pages": page_facts,
    }
