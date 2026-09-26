import json
from pathlib import Path

from apps.worker.stages.extractor_seo import extract_seo_facts
from apps.worker.stages.extractor_uxui import extract_uxui_facts, extract_uxui_facts_for_page

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _load_expected(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _page(name: str, html: str) -> dict:
    return {
        "url": f"https://{name}.example/",
        "final_url": f"https://{name}.example/",
        "status_code": 200,
        "html": html,
    }


def test_extractors_match_strong_site_fixture() -> None:
    html = _load_fixture("strong_site.html")
    expected = _load_expected("strong_site_expected.json")

    seo = extract_seo_facts([_page("strongbuilder", html)])
    uxui = extract_uxui_facts([_page("strongbuilder", html)])

    for key, value in expected["seo"]["summary"].items():
        assert seo["summary"][key] == value
    assert seo["pages"][0]["headings"]["counts"]["h1"] == expected["seo"]["page"]["h1_count"]
    assert seo["pages"][0]["headings"]["counts"]["h2"] == expected["seo"]["page"]["h2_count"]
    assert seo["pages"][0]["links"]["internal_count"] == expected["seo"]["page"]["internal_links"]
    assert (
        seo["pages"][0]["links"]["empty_or_non_http_count"]
        == expected["seo"]["page"]["empty_or_non_http_links"]
    )

    for key, value in expected["uxui"]["summary"].items():
        assert uxui["summary"][key] == value
    assert (
        uxui["pages"][0]["ctas"]["primary"]["text"] == expected["uxui"]["page"]["primary_cta_text"]
    )
    assert uxui["pages"][0]["forms"]["total_field_count"] == expected["uxui"]["page"]["form_fields"]
    assert uxui["pages"][0]["navigation"]["has_nav"] is expected["uxui"]["page"]["has_nav"]


def test_extractors_match_weak_site_fixture() -> None:
    html = _load_fixture("weak_site.html")
    expected = _load_expected("weak_site_expected.json")

    seo = extract_seo_facts([_page("weakbuilder", html)])
    uxui = extract_uxui_facts([_page("weakbuilder", html)])

    for key, value in expected["seo"]["summary"].items():
        assert seo["summary"][key] == value
    assert seo["pages"][0]["headings"]["counts"]["h1"] == expected["seo"]["page"]["h1_count"]
    assert seo["pages"][0]["links"]["internal_count"] == expected["seo"]["page"]["internal_links"]
    assert (
        seo["pages"][0]["links"]["empty_or_non_http_count"]
        == expected["seo"]["page"]["empty_or_non_http_links"]
    )
    assert seo["pages"][0]["images"]["missing_alt"] == expected["seo"]["page"]["missing_image_alts"]

    for key, value in expected["uxui"]["summary"].items():
        assert uxui["summary"][key] == value
    assert uxui["pages"][0]["lead_capture"]["has_form"] is expected["uxui"]["page"]["has_form"]
    assert (
        uxui["pages"][0]["lead_capture"]["has_direct_contact"]
        is expected["uxui"]["page"]["has_direct_contact"]
    )
    assert uxui["pages"][0]["navigation"]["has_nav"] is expected["uxui"]["page"]["has_nav"]


def test_uxui_required_field_count_ignores_aria_required_false() -> None:
    html = """
    <form>
      <input type="text" name="name" required>
      <input type="email" name="email" aria-required="true">
      <input type="text" name="company" aria-required="false">
      <button type="submit">Send</button>
    </form>
    """
    facts = extract_uxui_facts_for_page(_page("forms", html))
    form = facts["forms"]["items"][0]

    assert form["field_count"] == 3
    # name (required) + email (aria-required="true"); company (aria-required="false") excluded.
    assert form["required_field_count"] == 2


def test_schema_detects_business_identity_and_malformed_json_ld() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"GeneralContractor","name":"Acme"}
    </script>
    <script type="application/ld+json">{ this is not valid json }</script>
    </head><body></body></html>
    """
    facts = extract_seo_facts([_page("schema", html)])
    schema = facts["pages"][0]["schema"]
    assert schema["json_ld_blocks"] == 2
    assert schema["json_ld_invalid_blocks"] == 1
    assert schema["json_ld_valid"] is False
    assert schema["uses_schema_org_context"] is True
    assert schema["detected"]["organization"] is True
    assert schema["detected"]["local_business"] is True  # GeneralContractor -> "contractor"
    assert facts["summary"]["pages_with_organization_schema"] == 1
    assert facts["summary"]["pages_with_invalid_json_ld"] == 1


def test_schema_detects_breadcrumb_and_faq() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}
    </script>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"FAQPage"}
    </script>
    </head><body></body></html>
    """
    facts = extract_seo_facts([_page("bc", html)])
    schema = facts["pages"][0]["schema"]
    assert schema["detected"]["breadcrumb"] is True
    assert schema["detected"]["faq"] is True
    assert schema["json_ld_valid"] is True
    assert facts["summary"]["has_breadcrumb_schema"] is True


def test_security_detects_https_and_mixed_content() -> None:
    html = """
    <html><head>
      <link rel="stylesheet" href="http://cdn.example/a.css">
    </head><body>
      <img src="http://cdn.example/img.jpg" alt="x">
      <script src="https://cdn.example/ok.js"></script>
    </body></html>
    """
    facts = extract_seo_facts([_page("secure", html)])  # _page url is https://
    sec = facts["pages"][0]["security"]
    assert sec["https"] is True
    assert sec["mixed_content_count"] == 2  # http stylesheet + http img; the https script is fine
    assert facts["summary"]["all_pages_https"] is True
    assert facts["summary"]["pages_with_mixed_content"] == 1
    assert facts["summary"]["total_mixed_content"] == 2


def test_security_http_page_has_no_mixed_content() -> None:
    http_page = {
        "url": "http://insecure.example/",
        "final_url": "http://insecure.example/",
        "status_code": 200,
        "html": '<html><body><img src="http://x/a.jpg"></body></html>',
    }
    facts = extract_seo_facts([http_page])
    sec = facts["pages"][0]["security"]
    # On a plain-http page the whole page is insecure; we don't double-count "mixed content".
    assert sec["https"] is False
    assert sec["mixed_content_count"] == 0
    assert facts["summary"]["all_pages_https"] is False


def test_aeo_detects_clean_hierarchy_question_headings_and_lists() -> None:
    html = """
    <html><body><main>
      <h1>Custom Home Builder</h1>
      <h2>Our Services</h2>
      <ul><li>New homes</li><li>Remodels</li><li>Additions</li></ul>
      <h2>Frequently Asked Questions</h2>
      <h3>How much does a custom home cost?</h3>
      <p>It depends on finishes.</p>
      <h3>Do you offer free estimates?</h3>
      <p>Yes, after a consultation.</p>
    </main></body></html>
    """
    facts = extract_seo_facts([_page("aeo", html)])
    aeo = facts["pages"][0]["aeo"]
    assert aeo["heading_hierarchy_ok"] is True
    assert aeo["question_heading_count"] == 2  # the two "?" H3s; "Our Services" is not a question
    assert aeo["content_list_count"] == 1
    assert aeo["has_extractable_structure"] is True
    assert facts["summary"]["all_pages_heading_hierarchy_ok"] is True
    assert facts["summary"]["total_question_headings"] == 2
    assert facts["summary"]["has_extractable_structure"] is True


def test_aeo_flags_skipped_heading_level_and_missing_h1() -> None:
    skipped = "<html><body><h1>Title</h1><h3>Jumped past H2</h3></body></html>"
    assert (
        extract_seo_facts([_page("skip", skipped)])["pages"][0]["aeo"]["heading_hierarchy_ok"]
        is False
    )

    no_h1 = "<html><body><h2>Welcome</h2><p>No top-level heading.</p></body></html>"
    assert (
        extract_seo_facts([_page("noh1", no_h1)])["pages"][0]["aeo"]["heading_hierarchy_ok"]
        is False
    )


def test_aeo_excludes_nav_and_tiny_lists_from_extractable_structure() -> None:
    html = """
    <html><body>
      <nav><ul><li>Home</li><li>Services</li><li>Contact</li></ul></nav>
      <main>
        <h1>Services</h1>
        <ul><li>Only</li><li>Two</li></ul>
      </main>
    </body></html>
    """
    aeo = extract_seo_facts([_page("nav", html)])["pages"][0]["aeo"]
    # The 3-item nav menu is chrome; the 2-item main list is below the content threshold.
    assert aeo["content_list_count"] == 0
    assert aeo["has_extractable_structure"] is False


def test_local_detects_complete_nap_schema_address_and_map_link() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"GeneralContractor","name":"BLC Homes",
       "telephone":"+1-512-555-0188",
       "address":{"@type":"PostalAddress","streetAddress":"100 Congress Ave",
                  "addressLocality":"Austin"},
       "areaServed":"Greater Austin"}
    </script>
    </head><body>
      <address>BLC Homes, 100 Congress Ave, Austin, TX</address>
      <a href="https://www.google.com/maps/place/BLC+Homes">Find us on Google Maps</a>
    </body></html>
    """
    facts = extract_seo_facts([_page("local", html)])
    local = facts["pages"][0]["local"]
    assert local["nap_schema_complete"] is True
    assert local["schema_area_served"] is True
    assert local["has_address_element"] is True
    assert local["has_map_or_gbp_link"] is True
    assert facts["summary"]["has_complete_nap_schema"] is True
    assert facts["summary"]["has_service_area_markup"] is True
    assert facts["summary"]["has_map_or_gbp_link"] is True
    assert facts["summary"]["has_visible_address"] is True


def test_local_incomplete_nap_schema_is_not_complete() -> None:
    # Name + phone but NO address -> NAP is incomplete; no map link or <address> either.
    html = """
    <html><head>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"LocalBusiness","name":"BLC Homes",
       "telephone":"+1-512-555-0188"}
    </script>
    </head><body><p>No address block here.</p></body></html>
    """
    facts = extract_seo_facts([_page("partial", html)])
    local = facts["pages"][0]["local"]
    assert local["has_business_schema"] is True
    assert local["schema_address"] is False
    assert local["nap_schema_complete"] is False
    assert facts["summary"]["has_complete_nap_schema"] is False
    assert facts["summary"]["has_visible_address"] is False
    assert facts["summary"]["has_map_or_gbp_link"] is False


def test_a11y_flags_lang_landmark_zoom_label_and_empty_controls() -> None:
    html = """
    <html>
      <head><meta name="viewport" content="width=device-width, user-scalable=no"></head>
      <body>
        <form>
          <input type="text" name="email">
          <input type="hidden" name="csrf">
        </form>
        <a href="/x"><span aria-hidden="true">&#9733;</span></a>
        <button type="button"></button>
      </body>
    </html>
    """
    a11y = extract_seo_facts([_page("bad", html)])["pages"][0]["a11y"]
    assert a11y["has_lang"] is False
    assert a11y["has_main_landmark"] is False
    assert a11y["viewport_blocks_zoom"] is True
    assert a11y["form_control_count"] == 1  # the hidden input is excluded
    assert a11y["unlabeled_form_controls"] == 1
    assert a11y["empty_links"] == 1  # the only text is an aria-hidden star -> no accessible name
    assert a11y["empty_buttons"] == 1


def test_a11y_accepts_accessible_names_and_excludes_decorative() -> None:
    html = """
    <html lang="en">
      <head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
      <body>
        <main>
          <form>
            <label>Email <input type="email" name="email"></label>
            <input type="text" name="phone" aria-label="Phone number">
          </form>
          <a href="/home" aria-label="Home"><svg></svg></a>
          <a href="/profile"><img src="/i.png" alt="Your profile"></a>
          <button aria-label="Close"></button>
          <input type="submit" value="">
          <p id="lbl">Search the site</p>
          <input type="text" name="q" aria-labelledby="lbl">
        </main>
      </body>
    </html>
    """
    a11y = extract_seo_facts([_page("good", html)])["pages"][0]["a11y"]
    assert a11y["has_lang"] is True
    assert a11y["has_main_landmark"] is True
    assert a11y["viewport_blocks_zoom"] is False
    assert a11y["unlabeled_form_controls"] == 0  # wrapping label, aria-label, aria-labelledby
    assert a11y["empty_links"] == 0  # aria-label and inner img alt both count as names
    assert a11y["empty_buttons"] == 0  # aria-label button + empty-value submit (UA default text)


def test_a11y_gates_inapplicable_rules_and_narrows_duplicate_ids() -> None:
    # No forms/buttons/links/id-references at all -> those summary facts are None (rule skips).
    plain = "<html lang='en'><body><main><p>Just prose.</p></main></body></html>"
    summary = extract_seo_facts([_page("plain", plain)])["summary"]
    assert summary["unlabeled_form_controls"] is None
    assert summary["empty_buttons"] is None
    assert summary["empty_links"] is None
    assert summary["duplicate_referenced_ids"] is None
    assert summary["viewport_allows_zoom"] is None  # no viewport meta

    # Generic duplicate ids do NOT count (WCAG 2.2 dropped 4.1.1); only a referenced one does.
    dup = """
    <html lang="en"><body><main>
      <span id="dupe">A</span><span id="dupe">B</span>
      <span id="solo">Label</span>
      <label for="dupe">Pick</label>
      <input type="text" id="field" aria-labelledby="solo">
    </main></body></html>
    """
    a11y = extract_seo_facts([_page("dup", dup)])["pages"][0]["a11y"]
    assert (
        a11y["duplicate_referenced_ids"] == 1
    )  # 'dupe' is duplicated AND referenced by label[for]
    assert extract_seo_facts([_page("dup", dup)])["summary"]["duplicate_referenced_ids"] == 1


def test_extractors_handle_malformed_html_fixture() -> None:
    html = _load_fixture("malformed_site.html")
    expected = _load_expected("malformed_site_expected.json")

    seo = extract_seo_facts([_page("malformedbuilder", html)])
    uxui = extract_uxui_facts([_page("malformedbuilder", html)])

    assert seo["status"] == expected["seo"]["status"]
    assert seo["pages_analyzed"] == expected["seo"]["pages_analyzed"]
    assert uxui["status"] == expected["uxui"]["status"]
    assert uxui["pages_analyzed"] == expected["uxui"]["pages_analyzed"]


def test_popup_embed_form_gets_credit_without_static_form() -> None:
    # Sites built on popup/lazy-iframe form stacks (e.g. LeadConnector) have NO <form>
    # in the page HTML — the provider signature must still credit lead capture, and the
    # homepage field-count fact must be None (rule skips) instead of a false "0 fields".
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    html = (
        "<html><body>"
        '<a href="#" class="btn">Get a quote</a>'
        '<script src="https://link.msgsndr.com/js/form_embed.js"></script>'
        '<iframe src="https://api.leadconnectorhq.com/widget/form/ABC123" loading="lazy">'
        "</iframe>"
        "</body></html>"
    )
    facts = extract_uxui_facts(
        [SimpleNamespace(url="https://x.test/", final_url="https://x.test/", html=html)]
    )
    page = facts["pages"][0]
    assert page["forms"]["count"] == 0
    assert page["forms"]["form_detected"] == "provider_embed"
    assert "leadconnector" in page["forms"]["embedded_providers"]
    assert page["forms"]["total_field_count"] is None
    assert page["lead_capture"]["has_form"] is True
    assert page["lead_capture"]["has_low_pressure_path"] is True
    assert facts["summary"]["pages_with_form_capture"] == 1


def test_zero_field_form_shell_around_embed_is_uncountable() -> None:
    # The live BLC homepage has an empty <form> shell wrapping a lazy LeadConnector embed,
    # so the static parse found a form with 0 inputs and printed "0 homepage form fields".
    # A zero-input static form beside a provider embed means the real fields live in the
    # embed and can't be counted here -> None (rule skips), not a false 0.
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    html = (
        "<html><body>"
        '<form id="lead-form"></form>'
        '<script src="https://link.msgsndr.com/js/form_embed.js"></script>'
        '<iframe src="https://api.leadconnectorhq.com/widget/form/ABC123" loading="lazy">'
        "</iframe>"
        "</body></html>"
    )
    facts = extract_uxui_facts(
        [SimpleNamespace(url="https://x.test/", final_url="https://x.test/", html=html)]
    )
    page = facts["pages"][0]
    assert page["forms"]["form_detected"] == "static_form"
    assert page["forms"]["total_field_count"] is None
    assert page["lead_capture"]["has_form"] is True
    assert facts["summary"]["pages_with_form_capture"] == 1


def test_zero_field_form_shell_around_measured_iframe_uses_frame_count() -> None:
    # Same empty <form> shell, but this time the crawler's frame pass DID measure the
    # embedded form's fields -> use that honest count, not the shell's 0.
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    page_obj = SimpleNamespace(
        url="https://x.test/",
        final_url="https://x.test/",
        html='<html><body><form id="lead-form"></form></body></html>',
        frame_form_count=1,
        frame_form_field_count=3,
    )
    facts = extract_uxui_facts([page_obj])
    page = facts["pages"][0]
    assert page["forms"]["form_detected"] == "static_form"
    assert page["forms"]["total_field_count"] == 3


def test_static_form_with_real_fields_keeps_its_count() -> None:
    # Regression guard: a genuine static form with inputs must NOT be treated as
    # uncountable just because an unrelated embed also appears on the page.
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    html = (
        "<html><body>"
        '<form><input name="name"><input name="email"><input name="phone"></form>'
        '<script src="https://link.msgsndr.com/js/form_embed.js"></script>'
        "</body></html>"
    )
    facts = extract_uxui_facts(
        [SimpleNamespace(url="https://x.test/", final_url="https://x.test/", html=html)]
    )
    page = facts["pages"][0]
    assert page["forms"]["form_detected"] == "static_form"
    assert page["forms"]["total_field_count"] == 3


def test_runtime_iframe_form_counts_from_frame_pass() -> None:
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    page_obj = SimpleNamespace(
        url="https://x.test/",
        final_url="https://x.test/",
        html="<html><body><p>No visible form.</p></body></html>",
        frame_form_count=1,
        frame_form_field_count=4,
    )
    facts = extract_uxui_facts([page_obj])
    page = facts["pages"][0]
    assert page["forms"]["form_detected"] == "runtime_iframe_form"
    assert page["forms"]["total_field_count"] == 4
    assert facts["summary"]["pages_with_form_capture"] == 1


def test_page_with_no_countable_form_does_not_assert_a_field_count() -> None:
    # A page with no measurable form must NOT assert "0 homepage form fields": a count of 0
    # never means "a usable form with zero fields", it means we could not measure a form
    # (popup/JS/embed or none). total_field_count is None so the size rule SKIPS; whether a
    # form exists at all is judged separately by forms.present (here: absent).
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts

    facts = extract_uxui_facts(
        [
            SimpleNamespace(
                url="https://x.test/",
                final_url="https://x.test/",
                html="<html><body><p>Just text.</p></body></html>",
            )
        ]
    )
    page = facts["pages"][0]
    assert page["forms"]["form_detected"] == "none"
    assert page["forms"]["total_field_count"] is None
    assert facts["summary"]["pages_with_form_capture"] == 0


def test_js_popup_form_homepage_never_prints_zero_fields() -> None:
    # The live BLC case: the homepage form is a click-triggered popup, so the served/rendered
    # HTML has no <form>, no <iframe>, no inputs, and no recognized provider signature. The
    # audit must not print the false "0 homepage form fields" — the size fact is None (rule
    # skips) rather than a scored 0.
    from pathlib import Path
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts
    from apps.worker.stages.scoring import load_rubric, score_category

    html = (
        "<html><body>"
        "<h1>Custom Home Builder</h1>"
        '<a href="#" class="btn" data-open="quote">Get a quote</a>'  # popup trigger, no form in DOM
        '<a href="tel:+15551234567">(555) 123-4567</a>'
        "</body></html>"
    )
    uxui = extract_uxui_facts(
        [SimpleNamespace(url="https://x.test/", final_url="https://x.test/", html=html)]
    )
    assert uxui["pages"][0]["forms"]["total_field_count"] is None

    # Wrap as the real fact bundle ({"uxui": ...}) so the rubric's uxui.* fact paths resolve;
    # the rule must skip because total_field_count is None, not because the whole path is missing.
    out = score_category({"uxui": uxui}, load_rubric(Path("rubrics/uxui.yaml")))
    field_rule = next(r for r in out["rules"] if r["rule_id"] == "uxui.homepage_form.field_count")
    assert field_rule["result"] == "skipped"  # no false "0 fields" finding
    assert field_rule["evidence"]["value"] is None  # skipped because None, not a missing path


def test_popup_embed_passes_form_rule_and_skips_field_count() -> None:
    # Rule-level proof against the real uxui rubric: an embed-only page passes
    # uxui.forms.present and the homepage field-count rule SKIPS (rescales).
    from pathlib import Path
    from types import SimpleNamespace

    from apps.worker.stages.extractor_uxui import extract_uxui_facts
    from apps.worker.stages.scoring import load_rubric, score_category

    html = (
        "<html><body>"
        '<iframe src="https://api.leadconnectorhq.com/widget/form/ABC123"></iframe>'
        "</body></html>"
    )
    uxui_facts = extract_uxui_facts(
        [SimpleNamespace(url="https://x.test/", final_url="https://x.test/", html=html)]
    )
    rubric = load_rubric(Path("rubrics/uxui.yaml"))
    category = score_category({"uxui": uxui_facts}, rubric)
    by_id = {rule["rule_id"]: rule for rule in category["rules"]}
    assert by_id["uxui.forms.present"]["result"] == "pass"
    assert by_id["uxui.homepage_form.field_count"]["result"] == "skipped"
