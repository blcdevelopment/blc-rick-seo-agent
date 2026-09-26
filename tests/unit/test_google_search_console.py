import json

from apps.worker.stages.google_search_console import (
    _aio_factor,
    _brand_token,
    _branded_split,
    _opportunity_estimate,
    _ranking_opportunities,
    _topic_clusters,
    match_search_console_property,
)

_STRIKING = [
    {
        "query": "custom home builder austin",
        "impressions": 3000,
        "clicks": 57,
        "ctr": 0.019,
        "position": 9,
    },
    {
        "query": "kitchen remodel cost austin",
        "impressions": 1200,
        "clicks": 48,
        "ctr": 0.04,
        "position": 6,
    },
    {
        "query": "home additions austin",
        "impressions": 800,
        "clicks": 24,
        "ctr": 0.03,
        "position": 7,
    },
    {"query": "low impression", "impressions": 10, "clicks": 1, "ctr": 0.1, "position": 5},
    {"query": "page one already", "impressions": 500, "clicks": 90, "ctr": 0.18, "position": 2},
]


def test_opportunity_estimate_math_and_grounding_shape() -> None:
    striking = _ranking_opportunities(_STRIKING)
    # only impressions>=50 AND position 4-20 qualify (drops the 10-impression and the pos-2 rows)
    assert len(striking) == 3
    opp = _opportunity_estimate(striking, window_days=91, site_total_clicks=3000)

    assert opp["striking_query_count"] == 3
    assert opp["modeled_query_count"] == 3
    # Window totals are converted to true monthly rates: 5000 impressions over 91 days
    # = 5000 / (91 / 30.44) = 1673/month (half-up).
    assert opp["per_month"] is True and opp["window_days"] == 91
    assert opp["total_striking_impressions"] == 1673
    # low end models to position 5, high end to position 3 -> low <= high, both positive
    assert 0 < opp["opportunity_clicks_low"] <= opp["opportunity_clicks_high"]
    # The headline IS the conservative scenario, and scenarios are ordered.
    assert opp["opportunity_clicks_low"] == opp["scenarios"]["conservative"]["clicks_low"]
    assert (
        opp["scenarios"]["conservative"]["clicks_high"]
        <= opp["scenarios"]["expected"]["clicks_high"]
        <= opp["scenarios"]["optimistic"]["clicks_high"]
    )
    assert opp["estimated_leads_low"] <= opp["estimated_leads_high"]
    assert opp["lead_rate_low_pct"] == 5 and opp["lead_rate_high_pct"] == 10
    assert opp["ctr_curve_version"] == "blended-conservative-v1"
    # Every surfaced number must be an int (clean grounding token) — no floats/derived precision.
    for key in (
        "total_striking_impressions",
        "current_clicks",
        "site_monthly_clicks",
        "opportunity_clicks_low",
        "opportunity_clicks_high",
        "estimated_leads_low",
        "estimated_leads_high",
    ):
        assert isinstance(opp[key], int)
    # deterministic
    assert json.dumps(opp, sort_keys=True) == json.dumps(
        _opportunity_estimate(striking, window_days=91, site_total_clicks=3000), sort_keys=True
    )


def test_aio_discount_is_position_aware() -> None:
    # Grounded in the Ahrefs Dec-2025 AI-Overview study: top ranks suffer more CTR suppression,
    # so the upside factor is monotonically increasing in position (rank 1 keeps the least).
    factors = [_aio_factor(p) for p in range(1, 11)]
    assert factors == sorted(factors)  # rank 1 <= rank 2 <= ... <= rank 10
    assert all(0 < f <= 1 for f in factors)
    assert _aio_factor(1) < _aio_factor(10)  # a real spread, not flat
    assert _aio_factor(11) == _aio_factor(10)  # positions past 10 clamp to the pos-10 value


def test_opportunity_estimate_exposes_position_aware_aio_discount() -> None:
    opp = _opportunity_estimate(
        _ranking_opportunities(_STRIKING), window_days=91, site_total_clicks=3000
    )
    # The optimistic target (position 3) is discounted MORE than the conservative one (position 5).
    assert opp["aio_discount_max_pct"] > opp["aio_discount_min_pct"]
    assert opp["aio_model_version"] == "position-aware-v1"
    # Displayed discount is derived from the SAME _aio_factor applied to the clicks (no drift).
    assert opp["aio_discount_min_pct"] == int((1 - _aio_factor(5)) * 100 + 0.5)
    assert opp["aio_discount_max_pct"] == int((1 - _aio_factor(3)) * 100 + 0.5)
    # low-end estimate never exceeds the high-end even with different per-target discounts
    assert opp["opportunity_clicks_low"] <= opp["opportunity_clicks_high"]


def test_opportunity_estimate_empty_is_safe() -> None:
    assert _opportunity_estimate([], window_days=91, site_total_clicks=0) == {}


def test_opportunity_estimate_returns_empty_when_no_headroom() -> None:
    # A near-miss query that already out-performs the target CTR has no modeled upside, so the
    # estimate is suppressed rather than rendering a "0 to 0 more visits" message.
    over_performing = [
        {"query": "q", "impressions": 1000, "clicks": 500, "ctr": 0.5, "position": 4},
    ]
    assert _opportunity_estimate(over_performing, window_days=91, site_total_clicks=500) == {}


def test_opportunity_estimate_caps_at_multiple_of_current_clicks() -> None:
    # A huge modeled upside on a low-traffic site must not produce a many-times-current
    # step-function projection: every scenario is capped at 3x current monthly clicks,
    # and only the top-25 striking queries are modeled at all.
    rows = [
        {"query": f"q{i}", "impressions": 100000, "clicks": 10, "ctr": 0.0001, "position": 15}
        for i in range(30)
    ]
    opp = _opportunity_estimate(rows, window_days=91, site_total_clicks=300)
    assert opp["modeled_query_count"] == 25
    assert opp["striking_query_count"] == 30
    assert opp["capture_capped"] is True
    cap = 3 * opp["site_monthly_clicks"]
    for name in ("conservative", "expected", "optimistic"):
        assert opp["scenarios"][name]["clicks_high"] <= cap
        assert opp["scenarios"][name]["clicks_low"] <= cap


def test_opportunity_suppressed_when_monthly_conservative_rounds_to_zero() -> None:
    # The guard must test the number the reader sees (conservative, monthly) — the raw
    # window-total upside is ~6x larger at 91 days / 50% capture, and a tiny query set
    # that passes on window scale used to print "0-0 visits per month".
    tiny = [{"query": "q", "impressions": 50, "clicks": 4, "ctr": 0.087, "position": 15}]
    assert _opportunity_estimate(tiny, window_days=91, site_total_clicks=100) == {}


def test_zero_click_site_is_not_capped_to_a_floor() -> None:
    # A site with no click baseline has nothing to cap against: the old max(cap, 1)
    # floor pinned every scenario at "1-1 visits" while the disclosure claimed
    # "3x current clicks" (= 0). No cap applies; conservative capture + the AI-Overview
    # discount stay the only brakes.
    rows = [
        {"query": f"q{i}", "impressions": 100000, "clicks": 0, "ctr": 0.0, "position": 15}
        for i in range(10)
    ]
    opp = _opportunity_estimate(rows, window_days=91, site_total_clicks=0)
    assert opp["site_monthly_clicks"] == 0
    assert opp["cap_applied"] is False
    assert opp["capture_capped"] is False
    assert opp["scenarios"]["conservative"]["clicks_high"] > 1


def test_cluster_labels_trim_stopword_edges() -> None:
    # "near me"-suffixed query profiles used to tie-break into subject-less labels like
    # "repair near me"; trimming stopword edges keeps the noun ("roof repair").
    rows = [
        {"query": "roof repair near me", "impressions": 900, "clicks": 10},
        {"query": "emergency roof repair near me", "impressions": 400, "clicks": 5},
    ]
    clusters = _topic_clusters(rows, "")
    labels = [c["cluster"] for c in clusters]
    assert "roof repair" in labels
    for label in labels:
        words = label.split()
        assert words[0] not in {"near", "me", "the", "for"}
        assert words[-1] not in {"near", "me", "the", "for"}


def test_cluster_labels_keep_unicode_words_whole() -> None:
    # The old [^a-z0-9] tokenizer fragmented accented words into garbage phrase labels
    # ("a m xico"); \W-based splitting keeps them whole. The label is the cleanest phrase
    # ("plomería méxico"), so both accented words survive intact — that's the point here.
    rows = [{"query": "plomería méxico df", "impressions": 500, "clicks": 5}]
    clusters = _topic_clusters(rows, "")
    assert clusters
    label = clusters[0]["cluster"]
    assert "plomería" in label.split()
    assert "méxico" in label.split()
    assert "m" not in label.split() and "xico" not in label.split()  # no fragmentation


def test_branded_split_matches_spaced_brand_queries() -> None:
    rows = [
        {"query": "builder lead converter", "impressions": 500, "clicks": 200},
        {"query": "builderleadconverter reviews", "impressions": 100, "clicks": 30},
        {"query": "kitchen remodel", "impressions": 900, "clicks": 20},
    ]
    branded = _branded_split(rows, "https://www.builderleadconverter.com/")
    assert branded["brand_token"] == "builderleadconverter"
    assert branded["branded_query_count"] == 2  # spaced + exact both match
    assert branded["branded_impressions"] == 600
    assert branded["branded_impression_share_pct"] == 40
    assert _brand_token("https://www.example.co/") == "example"


def test_topic_clusters_group_deterministically() -> None:
    clusters = _topic_clusters(_STRIKING, _brand_token("https://x.example/"))
    assert clusters  # non-empty
    assert all(c["query_count"] > 0 and c["impressions"] > 0 for c in clusters)
    # sorted by impressions desc, deterministic
    impressions = [c["impressions"] for c in clusters]
    assert impressions == sorted(impressions, reverse=True)
    assert _topic_clusters([], "") == []


def test_match_search_console_property_prefers_longest_url_prefix() -> None:
    properties = [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://example.com/", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://example.com/blog/", "permissionLevel": "siteFullUser"},
    ]

    matched = match_search_console_property("https://example.com/blog/post", properties)

    assert matched == properties[2]


def test_match_search_console_property_falls_back_to_domain_property() -> None:
    properties = [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]

    matched = match_search_console_property("https://www.example.com/services", properties)

    assert matched == properties[0]


def test_match_search_console_property_returns_none_without_verified_match() -> None:
    properties = [{"siteUrl": "sc-domain:other.com", "permissionLevel": "siteOwner"}]

    assert match_search_console_property("https://example.com/", properties) is None


def test_topic_clusters_prefer_readable_phrases() -> None:
    # Labels must read like topics ("per square foot"), not disjoint fragments — the old
    # unigram seeding surfaced "square" and "foot" as two separate topics.
    rows = [
        {"query": "cost per square foot to build", "impressions": 1000, "position": 6},
        {"query": "average cost per square foot", "impressions": 800, "position": 7},
        {"query": "price per square foot to build a house", "impressions": 600, "position": 8},
    ]
    clusters = _topic_clusters(rows, "")
    labels = [cluster["cluster"] for cluster in clusters]
    assert any(" " in label for label in labels)
    assert "square" not in labels
    assert "foot" not in labels
    # Deterministic across runs.
    assert labels == [cluster["cluster"] for cluster in _topic_clusters(rows, "")]


def test_topic_clusters_phrase_wins_when_token_out_weighs_it() -> None:
    # The live report still showed single tokens ("square", "foot", "builder") because a
    # unigram collects at least the impressions of every phrase containing it, so in real
    # data (never a tie) the heaviest token won every seed and subsumed its own phrases.
    # Phrase-first seeding must beat a strictly-heavier token. "square" here totals more
    # impressions than "square foot", yet the label must still read as a phrase.
    rows = [
        {"query": "square foot cost", "impressions": 5000, "position": 6},
        {"query": "square footage estimate", "impressions": 4000, "position": 7},
        {"query": "square foot to build", "impressions": 3000, "position": 8},
        {"query": "cost per square foot to build a house", "impressions": 2000, "position": 6},
    ]
    labels = [c["cluster"] for c in _topic_clusters(rows, "")]
    assert labels, "expected at least one cluster"
    assert "square" not in labels and "foot" not in labels and "footage" not in labels
    # Every label is a readable multi-word phrase, none a bare single token.
    assert all(" " in label for label in labels)
    # No filler-only edges leak in ("... to", "per ...").
    for label in labels:
        words = label.split()
        assert words[0] not in {"per", "to", "a", "of"}
        assert words[-1] not in {"per", "to", "a", "of"}


def test_topic_clusters_do_not_drop_token_sharing_queries() -> None:
    # Phrase-first seeding (an earlier attempt) read well but silently dropped broad queries
    # that shared a topic token yet contained no exact seed phrase, deflating every theme.
    # Token grouping must keep them: "square footage estimate" and "construction loan
    # calculator" share a token with a themed query and must land in a cluster, not vanish.
    rows = [
        {"query": "cost per square foot to build a house", "impressions": 2000, "position": 6},
        {"query": "square footage estimate", "impressions": 900, "position": 9},  # 'footage'
        {"query": "square footage calculator", "impressions": 700, "position": 8},
        {"query": "construction cost per square foot", "impressions": 1700, "position": 5},
        {"query": "construction loan calculator", "impressions": 1500, "position": 12},  # broad
    ]
    clusters = _topic_clusters(rows, "")
    covered = sum(c["impressions"] for c in clusters)
    total = sum(r["impressions"] for r in rows)
    # Every impression is represented — no theme is deflated by dropped queries.
    assert covered == total
    labels = [c["cluster"] for c in clusters]
    assert all(" " in label for label in labels)  # still readable phrases, not tokens
    assert clusters == _topic_clusters(rows, "")  # deterministic


def test_topic_clusters_avoid_awkward_mid_filler_labels() -> None:
    # A "square foot to build shed" query used to be able to seed the awkward label
    # "foot to build"; preferring shorter, heavier phrases keeps labels clean.
    rows = [{"query": "square foot to build shed", "impressions": 100, "position": 6}]
    labels = [c["cluster"] for c in _topic_clusters(rows, "")]
    assert "foot to build" not in labels
    for label in labels:
        assert " to " not in f" {label} "


def test_topic_clusters_folded_fragment_query_is_not_dropped() -> None:
    # Regression (code review): a query whose only content token is a FRAGMENT folded into a
    # heavier seed's label must land in that seed's cluster, not vanish. Before the ownership
    # bucketing fix, "bravo" (folded into the "alpha bravo" label of the heavier "alpha" seed)
    # matched no bare seed token and its 10 impressions were silently dropped, deflating the
    # theme's total — the exact coverage loss the token-grouping rewrite is meant to prevent.
    rows = [
        {"query": "alpha bravo", "impressions": 1000, "position": 5},
        {"query": "alpha extra", "impressions": 100, "position": 5},
        {"query": "bravo", "impressions": 10, "position": 5},  # only content token is 'bravo'
    ]
    clusters = _topic_clusters(rows, "")
    covered = sum(c["impressions"] for c in clusters)
    total = sum(r["impressions"] for r in rows)
    assert covered == total  # no impressions dropped
    assert clusters == _topic_clusters(rows, "")  # deterministic


def test_topic_clusters_query_stays_in_its_own_token_theme() -> None:
    # Regression (code review): owned-word bucketing must NOT steal a query that genuinely
    # belongs to a lighter theme into a heavier one just because it shares a folded label
    # fragment. "foot doctor" is a doctor query and contains the 'doctor' seed token; it must
    # land in the doctor theme even though 'foot' is a folded fragment of the heavier
    # "square foot" seed. (Owned matching is only a fallback when no grouping token matches.)
    rows = [
        {"query": "square foot", "impressions": 1000, "position": 5},
        {"query": "square meter", "impressions": 900, "position": 5},
        {"query": "doctor consultation", "impressions": 800, "position": 5},
        {"query": "doctor visit", "impressions": 700, "position": 5},
        {"query": "foot doctor", "impressions": 300, "position": 5},  # doctor query, not square
    ]
    clusters = _topic_clusters(rows, "")
    doctor = next(c for c in clusters if "doctor" in c["cluster"])
    # doctor theme holds all three doctor queries (consultation + visit + foot doctor).
    assert doctor["impressions"] == 1800
    assert doctor["query_count"] == 3
    # the square theme did NOT absorb the 300-impression 'foot doctor' query.
    square = next(c for c in clusters if "square" in c["cluster"])
    assert square["impressions"] == 1900
    covered = sum(c["impressions"] for c in clusters)
    assert covered == sum(r["impressions"] for r in rows)  # no impressions lost
    assert clusters == _topic_clusters(rows, "")  # deterministic


def test_registrable_brand_label_handles_cctld_and_platform_hosts() -> None:
    # ONE brand deriver serves the GSC branded split and the Places business query; ccTLD
    # generics and multi-tenant platform hosts must both step past the non-brand label —
    # _brand_token("acme.com.au") used to return "com" and mark generic queries branded.
    from apps.worker.stages.google_search_console import _brand_token, registrable_brand_label

    assert registrable_brand_label("https://www.builderleadconverter.com/") == (
        "builderleadconverter"
    )
    assert registrable_brand_label("https://www.acme.com.au/") == "acme"
    assert registrable_brand_label("https://smithbuilders.wixsite.com/home") == "smithbuilders"
    assert registrable_brand_label("https://shop.acme.com/") == "acme"
    assert _brand_token("https://www.acme.com.au/") == "acme"
    assert _brand_token("https://acme-builders.example/") == "acmebuilders"


def test_granted_scopes_stored_from_token_payload() -> None:
    # The connection row must record the REAL grant: with connected-YouTube enabled the
    # consent may include the Analytics scope (or a user may untick one) — hardcoding
    # GSC_SCOPES would misstate what the stored token can actually do.
    from apps.worker.stages.google_search_console import GSC_SCOPES, _granted_scopes

    assert _granted_scopes(
        {"scope": "https://www.googleapis.com/auth/webmasters.readonly extra.scope"}
    ) == ["https://www.googleapis.com/auth/webmasters.readonly", "extra.scope"]
    assert _granted_scopes({}) == list(GSC_SCOPES)
    assert _granted_scopes({"scope": "   "}) == list(GSC_SCOPES)


def test_registrable_brand_label_walks_past_stacked_generic_labels() -> None:
    # smith.blogspot.co.uk stacks a platform label on a ccTLD family: a single step left would
    # stop at "blogspot" and bill a Places query for the PLATFORM's name. The walk must continue
    # until a real brand label is found.
    from apps.worker.stages.technical_crawl_common import registrable_brand_label

    assert registrable_brand_label("https://smith.blogspot.co.uk/") == "smith"
    assert registrable_brand_label("smith.co.uk") == "smith"
    assert registrable_brand_label("smithbuilders.wixsite.com") == "smithbuilders"
    assert registrable_brand_label("www.acme.com") == "acme"
    # The platform's own apex IS the platform brand — no over-walking.
    assert registrable_brand_label("blogspot.com") == "blogspot"


def test_registrable_brand_label_path_tenant_hosts() -> None:
    # On a path-tenant host the brand is the tenant path slug, never the platform's own label
    # ("google" would misclassify every 'google' query as branded and send a billed Places
    # lookup for the platform itself); a bare path-tenant host has no derivable brand.
    from apps.worker.stages.technical_crawl_common import registrable_brand_label

    assert registrable_brand_label("https://sites.google.com/view/smithbuilders") == (
        "smithbuilders"
    )
    assert registrable_brand_label("https://sites.google.com/site/smithbuilders/home") == (
        "smithbuilders"
    )
    assert registrable_brand_label("https://sites.google.com/") == ""


def test_registrable_brand_label_strips_path_tenant_prefixes() -> None:
    # The signed-in (/u/<n>/) and Workspace (/a/<domain>/) prefixes carry their own argument
    # segment: without dropping both, the "brand" comes back as "u"/"a" — a junk token that
    # still funds a billed Places lookup.
    from apps.worker.stages.technical_crawl_common import registrable_brand_label

    assert registrable_brand_label("https://sites.google.com/u/0/view/smithbuilders") == (
        "smithbuilders"
    )
    assert registrable_brand_label("https://sites.google.com/a/acme.com/site/smith") == "smith"
