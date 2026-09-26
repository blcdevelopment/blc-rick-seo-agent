from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.shared.config import Settings

JsonDict = dict[str, Any]
EvaluatorName = Literal["boolean", "presence", "range", "exact_match", "threshold", "linear_scale"]
NormalizationMode = Literal["sum_of_weights", "rescale_to_max"]
RuleResult = Literal["pass", "partial", "fail", "skipped"]
_MISSING = object()
_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[(\d+)\]")


class RubricRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    weight: float = Field(gt=0)
    fact_path: str
    evaluator: EvaluatorName
    params: JsonDict = Field(default_factory=dict)
    skip_if_missing: bool = False
    # Content-plan metadata (consumed by content_plan.build_content_plan). Optional so a
    # rubric without these fields still validates; the content planner derives safe
    # defaults when they are absent.
    impact: Literal["high", "medium", "low"] = "medium"
    tier: Literal["quick_win", "mid_term", "long_term"] = "quick_win"
    finding_label: str | None = None
    remediation: str | None = None
    surface_as_finding: bool = True
    # Display-only measurement unit for the scored fact ("%", " days", "/month"), consumed by
    # the social report's quantified metric lines. Never affects scoring. Optional — when
    # absent the report falls back to its fact-path heuristic (which also covers breakdowns
    # stored before units were declared).
    unit: str | None = None
    # Presentation-level merge: when BOTH this rule and the `merged_into` rule surface as
    # findings, this one folds into that primary as a covered-by note (content_plan). Scores
    # are untouched. It lives in the rubric — next to the rule ids it names — so a rule
    # rename/split updates the pair in the same file; a dangling target fails rubric load.
    merged_into: str | None = None


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    # "social" is allowed for the STANDALONE social audit rubric. It does NOT enter the
    # website CompositeRubric (whose weights stay exactly {seo, uxui}); the Social Score
    # is computed on its own via score_social_audit().
    category: Literal["seo", "uxui", "social"]
    max_score: int = Field(default=100, gt=0)
    normalization: NormalizationMode = "rescale_to_max"
    rules: list[RubricRule] = Field(min_length=1)

    @field_validator("version", "category")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _merged_into_targets_exist(self) -> Rubric:
        # A bad merge target silently DROPS a finding from every client report (the rule is
        # diverted into a covered-by group that is never rendered), so merge metadata is
        # validated hard at load time: targets must exist, no self-merges, and targets must
        # be terminal (no chains) — content_plan folds exactly one level.
        rules_by_id = {rule.id: rule for rule in self.rules}
        for rule in self.rules:
            if not rule.merged_into:
                continue
            if rule.merged_into == rule.id:
                raise ValueError(f"rule {rule.id!r} cannot merge into itself")
            target = rules_by_id.get(rule.merged_into)
            if target is None:
                raise ValueError(
                    f"rule {rule.id!r} merges into unknown rule id {rule.merged_into!r}"
                )
            if target.merged_into:
                raise ValueError(
                    f"rule {rule.id!r} merges into {rule.merged_into!r}, which itself merges "
                    f"into {target.merged_into!r} — merge targets must be terminal (no chains)"
                )
        return self


class CompositeRubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    max_score: int = Field(default=100, gt=0)
    weights: dict[Literal["seo", "uxui"], float]

    @model_validator(mode="after")
    def validate_weights(self) -> CompositeRubric:
        expected = {"seo", "uxui"}
        actual = set(self.weights)
        if actual != expected:
            raise ValueError(f"composite weights must include exactly {sorted(expected)}")

        total = sum(self.weights.values())
        if abs(total - 1.0) > 0.0001:
            raise ValueError("composite weights must sum to 1.0")

        for category, weight in self.weights.items():
            if weight < 0:
                raise ValueError(f"composite weight for {category} must be non-negative")
        return self


class OverallRubric(BaseModel):
    """Weights for the combined-audit Overall Lead-Gen Readiness score (rubrics/overall.yaml).

    Blends the website Lead-Gen composite with the standalone Social Score. The website composite
    ({seo, uxui}) is UNTOUCHED — it is used here only as one of two pre-computed inputs."""

    model_config = ConfigDict(extra="forbid")

    version: str
    max_score: int = Field(default=100, gt=0)
    website_weight: float = Field(ge=0, le=1)
    social_weight: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_weights(self) -> OverallRubric:
        total = self.website_weight + self.social_weight
        if abs(total - 1.0) > 0.0001:
            raise ValueError("overall weights (website_weight + social_weight) must sum to 1.0")
        return self


class Evaluation(BaseModel):
    result: RuleResult
    ratio: float = Field(ge=0, le=1)
    reason: str | None = None


def load_rubric(path: Path) -> Rubric:
    payload = _read_yaml(path)
    return Rubric.model_validate(payload)


def load_composite_rubric(path: Path) -> CompositeRubric:
    payload = _read_yaml(path)
    return CompositeRubric.model_validate(payload)


def score_audit(
    seo_facts: JsonDict,
    uxui_facts: JsonDict,
    psi_facts: JsonDict,
    settings: Settings,
    external_seo_facts: JsonDict | None = None,
) -> JsonDict:
    seo_rubric = load_rubric(settings.rubric_seo_path)
    uxui_rubric = load_rubric(settings.rubric_uxui_path)
    composite_rubric = load_composite_rubric(settings.rubric_composite_path)
    fact_bundle = {
        "seo": seo_facts,
        "uxui": uxui_facts,
        "psi": psi_facts,
        "external_seo": _trusted_external_seo_facts(external_seo_facts),
    }

    seo_breakdown = score_category(fact_bundle, seo_rubric)
    uxui_breakdown = score_category(fact_bundle, uxui_rubric)
    lead_gen = compose_lead_generation_score(
        seo_breakdown["score"],
        uxui_breakdown["score"],
        composite_rubric,
    )

    rubric_version = f"{seo_rubric.version}+{uxui_rubric.version}+{composite_rubric.version}"
    return {
        "status": "complete",
        "rubric_version": rubric_version,
        "scores": {
            "seo": seo_breakdown["score"],
            "uxui": uxui_breakdown["score"],
            "lead_gen": lead_gen["score"],
        },
        "categories": {
            "seo": seo_breakdown,
            "uxui": uxui_breakdown,
        },
        "composite": lead_gen,
    }


def score_social_audit(social_facts: JsonDict, settings: Settings) -> JsonDict:
    """Standalone Social Score for the Phase-2 social audit.

    Scores ``rubrics/social.yaml`` against the social facts bundle (from
    ``stages.social.extractor``). This is independent of the website audit — it never
    touches the website composite, so website scores are unaffected. Returns no score
    when collection was skipped/failed (status not complete/partial)."""
    rubric = load_rubric(settings.rubric_social_path)
    status = social_facts.get("status") if isinstance(social_facts, dict) else None
    if status not in {"complete", "partial"}:
        return {
            "status": status or "skipped",
            "rubric_version": rubric.version,
            "score": None,
            "category": None,
        }

    breakdown = score_category({"social": social_facts}, rubric)
    return {
        "status": "complete",
        "rubric_version": rubric.version,
        "score": breakdown["score"],
        "category": breakdown,
    }


def _trusted_external_seo_facts(external_seo_facts: JsonDict | None) -> JsonDict:
    if not isinstance(external_seo_facts, dict):
        return {}

    trusted = dict(external_seo_facts)
    for source_key in ("technical_crawl", "screaming_frog", "gsc", "url_inspection"):
        source = trusted.get(source_key)
        if not isinstance(source, dict):
            continue
        if source.get("status") == "complete":
            continue
        sanitized_source = dict(source)
        sanitized_source.pop("summary", None)
        trusted[source_key] = sanitized_source
    return trusted


def score_category(facts: JsonDict, rubric: Rubric) -> JsonDict:
    rule_results = [_score_rule(facts, rule) for rule in rubric.rules]
    evaluated_rules = [rule for rule in rule_results if rule["result"] != "skipped"]
    evaluated_weight = sum(rule["weight"] for rule in evaluated_rules)
    skipped_weight = sum(rule["weight"] for rule in rule_results if rule["result"] == "skipped")
    awarded_points = sum(rule["points_awarded"] for rule in evaluated_rules)

    # When EVERY rule skipped (no scorable facts) the score is 0 only because there was nothing
    # to score — which reads identically to a genuine 0. Surface ``data_sufficient`` so consumers
    # can tell "no data" apart from "scored zero". (Effectively unreachable for the website audit,
    # whose core rules never skip; defensive, and keeps the numeric score unchanged.)
    data_sufficient = bool(evaluated_rules) and evaluated_weight > 0
    if not data_sufficient:
        score = 0
    elif rubric.normalization == "sum_of_weights":
        score = _round_score(awarded_points, rubric.max_score)
    else:
        normalized_points = (awarded_points / evaluated_weight) * rubric.max_score
        score = _round_score(normalized_points, rubric.max_score)

    return {
        "status": "complete",
        "category": rubric.category,
        "rubric_version": rubric.version,
        "score": score,
        "max_score": rubric.max_score,
        "normalization": rubric.normalization,
        "data_sufficient": data_sufficient,
        "weights": {
            "configured": round(sum(rule.weight for rule in rubric.rules), 4),
            "evaluated": round(evaluated_weight, 4),
            "skipped": round(skipped_weight, 4),
        },
        "rules": rule_results,
    }


def compose_lead_generation_score(
    seo_score: int,
    uxui_score: int,
    rubric: CompositeRubric,
) -> JsonDict:
    weighted = (seo_score * rubric.weights["seo"]) + (uxui_score * rubric.weights["uxui"])
    score = _round_score(weighted, rubric.max_score)
    return {
        "status": "complete",
        "rubric_version": rubric.version,
        "score": score,
        "max_score": rubric.max_score,
        "weights": rubric.weights,
        "inputs": {
            "seo": seo_score,
            "uxui": uxui_score,
        },
    }


def load_overall_rubric(path: Path) -> OverallRubric:
    payload = _read_yaml(path)
    return OverallRubric.model_validate(payload)


def _readiness_band(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score >= 75:
        return "strong"
    if score >= 50:
        return "fair"
    return "weak"


def compose_overall_readiness_score(
    *,
    website_lead_gen: int | None,
    social_score: int | None,
    settings: Settings,
) -> JsonDict:
    """Combine the website Lead-Gen composite with the Social Score into one 0-100 Overall
    Lead-Gen Readiness number (combined audit only). When the social audit produced no score,
    the readiness rescales to the website Lead-Gen score alone (social weight drops out).
    Deterministic; half-up rounding to match the rest of the engine. The website composite is
    untouched — it is consumed here only as a pre-computed input."""
    rubric = load_overall_rubric(settings.rubric_overall_path)
    if website_lead_gen is None:
        return {
            "status": "skipped",
            "rubric_version": rubric.version,
            "score": None,
            "band": "unknown",
            "max_score": rubric.max_score,
            "weights": {"website": rubric.website_weight, "social": rubric.social_weight},
            "inputs": {"website_lead_gen": website_lead_gen, "social": social_score},
        }

    if social_score is None:
        score = _round_score(website_lead_gen, rubric.max_score)
        status = "website_only"
        weights = {"website": 1.0, "social": 0.0}
    else:
        weighted = website_lead_gen * rubric.website_weight + social_score * rubric.social_weight
        score = _round_score(weighted, rubric.max_score)
        status = "complete"
        weights = {"website": rubric.website_weight, "social": rubric.social_weight}

    return {
        "status": status,
        "rubric_version": rubric.version,
        "score": score,
        "band": _readiness_band(score),
        "max_score": rubric.max_score,
        "weights": weights,
        "inputs": {"website_lead_gen": website_lead_gen, "social": social_score},
    }


def _read_yaml(path: Path) -> JsonDict:
    with path.open(encoding="utf-8") as file:
        payload = yaml.safe_load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"Rubric file {path} must contain a YAML object.")
    return payload


def _score_rule(facts: JsonDict, rule: RubricRule) -> JsonDict:
    value = resolve_fact_path(facts, rule.fact_path)
    if value is _MISSING or (value is None and rule.skip_if_missing):
        if rule.skip_if_missing:
            evaluation = Evaluation(result="skipped", ratio=0, reason="fact path missing")
        else:
            evaluation = Evaluation(result="fail", ratio=0, reason="fact path missing")
    else:
        evaluation = _evaluate(rule, value)

    points = 0.0 if evaluation.result == "skipped" else round(rule.weight * evaluation.ratio, 4)
    return {
        "rule_id": rule.id,
        "description": rule.description,
        "weight": rule.weight,
        "evaluator": rule.evaluator,
        "fact_path": rule.fact_path,
        "impact": rule.impact,
        "tier": rule.tier,
        "finding_label": rule.finding_label,
        "remediation": rule.remediation,
        "surface_as_finding": rule.surface_as_finding,
        "unit": rule.unit,
        "merged_into": rule.merged_into,
        "result": evaluation.result,
        "points_awarded": points,
        "points_possible": 0.0 if evaluation.result == "skipped" else rule.weight,
        "evidence": {
            "value": None if value is _MISSING else value,
            "params": rule.params,
            "reason": evaluation.reason,
        },
    }


def _evaluate(rule: RubricRule, value: Any) -> Evaluation:
    evaluator = rule.evaluator
    params = rule.params
    if evaluator == "boolean":
        return _boolean(value)
    if evaluator == "presence":
        return _presence(value)
    if evaluator == "range":
        return _range(value, params)
    if evaluator == "exact_match":
        return _exact_match(value, params)
    if evaluator == "threshold":
        return _threshold(value, params)
    if evaluator == "linear_scale":
        return _linear_scale(value, params)
    raise ValueError(f"Unsupported evaluator: {evaluator}")


def _boolean(value: Any) -> Evaluation:
    if value is True:
        return Evaluation(result="pass", ratio=1)
    return Evaluation(result="fail", ratio=0)


def _presence(value: Any) -> Evaluation:
    if value is None:
        return Evaluation(result="fail", ratio=0)
    if isinstance(value, str) and not value.strip():
        return Evaluation(result="fail", ratio=0)
    if isinstance(value, (list, dict, set, tuple)) and not value:
        return Evaluation(result="fail", ratio=0)
    return Evaluation(result="pass", ratio=1)


def _range(value: Any, params: JsonDict) -> Evaluation:
    number = _as_number(value)
    if number is None:
        return Evaluation(result="fail", ratio=0, reason="value is not numeric")

    full_credit = params.get("full_credit")
    if _within_range(number, full_credit):
        return Evaluation(result="pass", ratio=1)

    partial_ranges = params.get("partial_credit") or []
    if any(_within_range(number, item) for item in partial_ranges):
        return Evaluation(result="partial", ratio=0.5)

    return Evaluation(result="fail", ratio=0)


def _exact_match(value: Any, params: JsonDict) -> Evaluation:
    full_credit = params.get("full_credit")
    if value == full_credit:
        return Evaluation(result="pass", ratio=1)

    partial_credit = params.get("partial_credit") or []
    if value in partial_credit:
        return Evaluation(result="partial", ratio=0.5)

    return Evaluation(result="fail", ratio=0)


def _threshold(value: Any, params: JsonDict) -> Evaluation:
    number = _as_number(value)
    if number is None:
        return Evaluation(result="fail", ratio=0, reason="value is not numeric")

    min_value = params.get("min")
    max_value = params.get("max")
    partial_min = params.get("partial_min")
    partial_max = params.get("partial_max")

    if _meets_bounds(number, min_value, max_value):
        return Evaluation(result="pass", ratio=1)
    if _meets_bounds(number, partial_min, partial_max):
        return Evaluation(result="partial", ratio=0.5)
    return Evaluation(result="fail", ratio=0)


def _linear_scale(value: Any, params: JsonDict) -> Evaluation:
    number = _as_number(value)
    if number is None:
        return Evaluation(result="fail", ratio=0, reason="value is not numeric")

    input_range = params.get("input_range") or [0, 100]
    if not isinstance(input_range, list | tuple) or len(input_range) != 2:
        return Evaluation(result="fail", ratio=0, reason="invalid input_range")

    start = _as_number(input_range[0])
    end = _as_number(input_range[1])
    if start is None or end is None or end == start:
        return Evaluation(result="fail", ratio=0, reason="invalid input_range")

    ratio = (number - start) / (end - start)
    clamped = min(max(ratio, 0.0), 1.0)
    if clamped >= 1:
        result: RuleResult = "pass"
    elif clamped > 0:
        result = "partial"
    else:
        result = "fail"
    return Evaluation(result=result, ratio=clamped)


def _within_range(number: float, range_value: Any) -> bool:
    if not isinstance(range_value, list | tuple) or len(range_value) != 2:
        return False
    start = _as_number(range_value[0])
    end = _as_number(range_value[1])
    if start is None or end is None:
        return False
    return start <= number <= end


def _meets_bounds(number: float, min_value: Any, max_value: Any) -> bool:
    if min_value is None and max_value is None:
        return False
    minimum = _as_number(min_value)
    maximum = _as_number(max_value)
    if minimum is not None and number < minimum:
        return False
    return not (maximum is not None and number > maximum)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def round_score(value: float, max_score: int) -> int:
    """Half-up round to an int, then clamp to ``[0, max_score]`` — the codebase-wide rounding
    convention (CLAUDE.md §10). Public so other stages (e.g. benchmarking) can reuse it instead of
    re-implementing the clamp or importing a private name."""
    return min(max(int(value + 0.5), 0), max_score)


# Back-compat alias for this module's many internal callers.
_round_score = round_score


def resolve_fact_path(facts: Any, path: str) -> Any:
    current = facts
    for raw_part in path.split("."):
        if not raw_part:
            return _MISSING
        for token, index in _PATH_TOKEN_RE.findall(raw_part):
            if token:
                if not isinstance(current, dict) or token not in current:
                    return _MISSING
                current = current[token]
            elif index:
                if not isinstance(current, list):
                    return _MISSING
                position = int(index)
                if position >= len(current):
                    return _MISSING
                current = current[position]
    return current
