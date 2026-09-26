from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

JsonDict = dict[str, Any]
NUMERIC_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?%?")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Grounding validates factual *claims* about the site. These keys are not claims, so the
# numbers inside them must not be stripped:
#   - evidence_refs: machine identifiers / fact paths (e.g. "seo.pages[0]....") - the
#     bracketed index is not a measurement.
#   - action_items: prescriptive advice that legitimately carries target numbers (e.g.
#     "70-160 characters", "1-5 fields") - guidance, not a claim about the site.
#   - location_urls / location_label: "where to start" addresses (URLs can contain
#     digits like ".../96-percent-failure/") - locations, not measurements.
UNGROUNDED_KEYS = frozenset({"evidence_refs", "action_items", "location_urls", "location_label"})
# Timeframe/duration phrases (e.g. "30 days", "1-3 months", "3 to 12 months") are
# rhetorical recommendation language, not measured site facts, so the numbers in them
# must not trigger sentence stripping. They are masked out before grounding the rest.
TIMEFRAME_RE = re.compile(
    r"\d[\d,]*\s*(?:[-–]|to)?\s*\d*\s*"
    r"(?:second|minute|hour|day|week|month|quarter|year)s?\b",
    re.IGNORECASE,
)


def validate_commentary_grounding(
    commentary: JsonDict,
    *,
    fact_sources: JsonDict,
) -> tuple[JsonDict, JsonDict]:
    # Imported lazily to avoid a module-load cycle: commentary imports the shared social
    # grounding helpers (collect_social_known_numbers / social_text_has_ungrounded) from here.
    from apps.worker.stages.commentary import validate_commentary_content

    sanitized = deepcopy(commentary)
    content = sanitized.get("content")
    if not isinstance(content, dict):
        return sanitized, {
            "status": "failed",
            "unsupported_claims": [],
            "reason": "commentary content missing",
        }

    known_values = _known_numeric_values(fact_sources)
    unsupported: list[JsonDict] = []
    sanitized["content"] = _sanitize_value(
        content,
        path="content",
        known_values=known_values,
        unsupported=unsupported,
    )
    validate_commentary_content(sanitized["content"])

    return sanitized, {
        "status": "complete",
        "numeric_claims_checked": sum(item["claim_count"] for item in unsupported)
        + _supported_claim_count(content, known_values),
        "unsupported_claims": unsupported,
        "unsupported_claim_count": sum(item["claim_count"] for item in unsupported),
        "action": _summary_action(unsupported),
    }


def _summary_action(unsupported: list[JsonDict]) -> str:
    if not unsupported:
        return "none"
    if any(item.get("outcome") == "stripped" for item in unsupported):
        return "stripped_unsupported_numeric_sentences"
    return "reverted_unsupported_to_baseline"


def _sanitize_value(
    value: Any,
    *,
    path: str,
    known_values: set[float],
    unsupported: list[JsonDict],
) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                child
                if key in UNGROUNDED_KEYS
                else _sanitize_value(
                    child,
                    path=f"{path}.{key}",
                    known_values=known_values,
                    unsupported=unsupported,
                )
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_value(
                child,
                path=f"{path}[{index}]",
                known_values=known_values,
                unsupported=unsupported,
            )
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        return _sanitize_text(value, path=path, known_values=known_values, unsupported=unsupported)
    return value


def _sanitize_text(
    text: str,
    *,
    path: str,
    known_values: set[float],
    unsupported: list[JsonDict],
) -> str:
    sentences = SENTENCE_RE.split(text)
    kept: list[str] = []
    removed: list[JsonDict] = []
    for sentence in sentences:
        # Mask timeframe phrases first so legitimate prose ("within 30 days",
        # "1-3 months") is not stripped as an unsupported numeric claim.
        claims = _numeric_claims(TIMEFRAME_RE.sub(" ", sentence))
        unsupported_values = [claim for claim in claims if claim not in known_values]
        if unsupported_values:
            removed.append(
                {
                    "text": sentence.strip(),
                    "values": unsupported_values,
                }
            )
        else:
            kept.append(sentence)

    cleaned = " ".join(sentence.strip() for sentence in kept if sentence.strip()).strip()

    if not removed:
        return cleaned

    if cleaned:
        outcome, result_text = "stripped", cleaned
    else:
        # The whole field is unsupported. Revert to the original text rather than leaking a
        # placeholder string into the report; the deterministic baseline is grounded by
        # construction, and the Phase 2 polish layer passes an explicit baseline. The revert
        # is recorded below so the validation log honestly reflects what was flagged and that
        # the baseline text was kept rather than removed.
        outcome, result_text = "reverted_to_baseline", text.strip()

    unsupported.append(
        {
            "path": path,
            "claim_count": sum(len(item["values"]) for item in removed),
            "removed_sentences": removed,
            "outcome": outcome,
        }
    )
    return result_text


# --- Shared numeric grounding ------------------------------------------------------------
# ONE parameterized implementation backs BOTH grounding paths (P2-24 / SMWA-76): the website
# path (every number, full precision, sentence-level stripping) and the social commentary path
# (only "risky" percentages / large counts, integer-or-2dp precision, field-level revert). The
# paths differ only in their regex + token normalization, supplied below; the recursive walk
# and the claim extraction are shared, so there is a single tested implementation to maintain.

# Social only flags a likely-fabricated claim: a percentage, or a 3+-digit count/year. Small
# numbers (cadence like "2-3 posts/week", "30 days") are advice, not claims about the brand.
_SOCIAL_RISKY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%|\b\d[\d,]{2,}(?:\.\d+)?\b")
_SOCIAL_ANY_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _website_token(value: Any) -> float | None:
    """Normalize a website number/token to a comparable float (``None`` => not a number)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return round(float(value), 4)
    raw = str(value).replace(",", "").removesuffix("%")
    try:
        return round(float(raw), 4)
    except ValueError:
        return None


def _social_token(value: Any) -> str | None:
    """Normalize a social number/token to an int-or-2dp string (``None`` => skip)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        digits = re.sub(r"[^\d.]", "", str(value)).rstrip(".")
        if not digits:
            return None
        try:
            number = float(digits)
        except ValueError:
            return digits
    return str(int(number)) if number.is_integer() else str(round(number, 2))


class NumericGrounding:
    """Collect known numbers from fact payloads and flag ungrounded numeric claims in text.

    Parameterized by a collection regex (numbers to harvest from fact strings), a claim regex
    (numbers in prose that count as factual claims), and a token normalizer, so the website and
    social paths share the walk/extract logic while keeping their distinct policies.
    """

    def __init__(
        self,
        *,
        collect_re: re.Pattern[str],
        claim_re: re.Pattern[str],
        to_token: Callable[[Any], Any],
    ) -> None:
        self._collect_re = collect_re
        self._claim_re = claim_re
        self._to_token = to_token

    def known(self, *payloads: Any) -> set:
        found: set = set()
        for payload in payloads:
            self._walk(payload, found)
        return found

    def _walk(self, value: Any, found: set) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int | float):
            token = self._to_token(value)
            if token is not None:
                found.add(token)
        elif isinstance(value, str):
            for match in self._collect_re.findall(value):
                token = self._to_token(match)
                if token is not None:
                    found.add(token)
        elif isinstance(value, dict):
            for child in value.values():
                self._walk(child, found)
        elif isinstance(value, list | tuple | set):
            for child in value:
                self._walk(child, found)

    def claims(self, text: str) -> list:
        tokens: list = []
        for match in self._claim_re.findall(text):
            token = self._to_token(match)
            if token is not None:
                tokens.append(token)
        return tokens

    def has_ungrounded(self, text: str, known: set) -> bool:
        return any(token not in known for token in self.claims(text))


_WEBSITE = NumericGrounding(collect_re=NUMERIC_RE, claim_re=NUMERIC_RE, to_token=_website_token)
_SOCIAL = NumericGrounding(
    collect_re=_SOCIAL_ANY_RE, claim_re=_SOCIAL_RISKY_RE, to_token=_social_token
)


def _known_numeric_values(payload: Any) -> set[float]:
    return _WEBSITE.known(payload)


def _numeric_claims(text: str) -> list[float]:
    return _WEBSITE.claims(text)


def collect_social_known_numbers(*payloads: Any) -> set[str]:
    """Known numeric tokens for social grounding (followers, percentages, counts, etc.)."""
    return _SOCIAL.known(*payloads)


def social_text_has_ungrounded(text: str, known: set[str]) -> bool:
    """True if ``text`` asserts a risky number (percentage / large count) not in ``known``."""
    return _SOCIAL.has_ungrounded(text, known)


def _supported_claim_count(payload: Any, known_values: set[float]) -> int:
    if isinstance(payload, str):
        return sum(1 for claim in _numeric_claims(payload) if claim in known_values)
    if isinstance(payload, dict):
        return sum(
            _supported_claim_count(value, known_values)
            for key, value in payload.items()
            if key not in UNGROUNDED_KEYS
        )
    if isinstance(payload, list):
        return sum(_supported_claim_count(value, known_values) for value in payload)
    return 0
