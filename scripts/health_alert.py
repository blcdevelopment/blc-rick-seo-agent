"""Operational health alerting — run from cron on the host.

Checks audit metrics against thresholds and posts a message to ALERT_WEBHOOK_URL
(Slack / Discord / generic JSON ``{"text": ...}`` webhook) when something looks wrong:
too many failed audits in 24h, or an audit stuck in progress too long. Empty webhook =>
the script just prints the findings (no-op send), mirroring the Sentry/Apify opt-in pattern.

    */15 * * * *  cd /app && python scripts/health_alert.py >> /var/log/blc-alert.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from apps.shared.config import Settings, get_settings
from apps.shared.database import SessionLocal
from apps.shared.metrics import collect_metrics


def evaluate_alerts(metrics: dict, *, failed_threshold: int, stuck_minutes: int) -> list[str]:
    """Pure threshold check over a metrics dict — returns human-readable alert lines."""
    audits = metrics.get("audits", {}) if isinstance(metrics, dict) else {}
    alerts: list[str] = []

    failed = audits.get("failed_24h") or 0
    if failed >= failed_threshold:
        alerts.append(f"{failed} audit(s) failed in the last 24h (threshold {failed_threshold}).")

    oldest = audits.get("oldest_in_progress_minutes")
    if oldest is not None and oldest >= stuck_minutes:
        alerts.append(
            f"An audit has been in progress for {oldest} min (threshold {stuck_minutes}) — "
            f"possible stuck job."
        )
    return alerts


def _send(webhook_url: str, message: str) -> bool:
    try:
        resp = httpx.post(webhook_url, json={"text": message}, timeout=15)
        return resp.status_code < 400
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check audit health and alert on thresholds.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but never POST.")
    args = parser.parse_args(argv)

    settings: Settings = get_settings()
    with SessionLocal() as db:
        metrics = collect_metrics(db, settings)

    alerts = evaluate_alerts(
        metrics,
        failed_threshold=settings.alert_failed_audits_threshold,
        stuck_minutes=settings.alert_stuck_audit_minutes,
    )

    webhook = settings.alert_webhook_url.get_secret_value() if settings.alert_webhook_url else ""
    sent = False
    if alerts and webhook and not args.dry_run:
        sent = _send(webhook, "BLC audit alerts:\n- " + "\n- ".join(alerts))

    print(
        json.dumps(
            {
                "alerts": alerts,
                "sent": sent,
                "webhook_configured": bool(webhook),
                "metrics": metrics.get("audits", {}),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
