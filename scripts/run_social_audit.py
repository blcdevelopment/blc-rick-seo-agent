"""Run a standalone Social audit end-to-end (no DB / no web app needed).

handle -> Apify (collector) -> normalized social facts (extractor) -> Social Score
(scoring.score_social_audit). Reads APIFY_API_TOKEN from .env; free-tier friendly.

Usage:
    python scripts/run_social_audit.py <instagram_handle_or_url>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.shared.config import get_settings  # noqa: E402
from apps.worker.stages.scoring import score_social_audit  # noqa: E402
from apps.worker.stages.social.collector import collect_social_facts  # noqa: E402


def _platform_and_handle(raw: str) -> tuple[str, str]:
    platform = "facebook" if "facebook.com" in raw.lower() else "instagram"
    match = re.search(r"(?:instagram\.com|facebook\.com)/([^/?#]+)", raw, re.IGNORECASE)
    handle = (match.group(1) if match else raw).lstrip("@").strip("/").strip()
    return platform, handle


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python scripts/run_social_audit.py <instagram_or_facebook_link_or_handle>")
        return 2

    platform, handle = _platform_and_handle(args[0].strip())
    settings = get_settings()

    print(f"[social] collecting {platform} @{handle} via Apify ...")
    facts = collect_social_facts(settings, {platform: handle})
    print(f"[social] collection status: {facts.get('status')}", end="")
    if facts.get("reason"):
        print(f" (reason: {facts['reason']})", end="")
    print()

    summary = facts.get("summary") or {}
    if summary:
        print("\nsocial facts summary:")
        print(json.dumps(summary, indent=2, default=str))

    result = score_social_audit(facts, settings)
    print(f"\n=== SOCIAL SCORE: {result['score']}  (status: {result['status']}) ===")
    breakdown = result.get("category")
    if breakdown:
        for rule in breakdown["rules"]:
            print(
                f"  [{rule['result']:>7}] {rule['rule_id']:<28} "
                f"{rule['points_awarded']}/{rule['points_possible']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
