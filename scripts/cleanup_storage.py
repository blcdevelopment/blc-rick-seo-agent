"""Delete locally-stored audit artifacts older than the retention window.

Usage:
    python scripts/cleanup_storage.py [--dry-run] [--days N]

There is no in-app scheduler — run this from cron on the host, e.g.:
    0 3 * * *  cd /app && python scripts/cleanup_storage.py >> /var/log/blc-cleanup.log 2>&1

Honors STORAGE_RETENTION_DAYS from the environment / .env unless --days overrides it;
--dry-run reports what would be deleted without removing anything.
"""

from __future__ import annotations

import argparse
import json
import sys

from apps.shared.config import get_settings
from apps.shared.retention import cleanup_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune old local audit artifacts.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted.")
    parser.add_argument(
        "--days", type=int, default=None, help="Override STORAGE_RETENTION_DAYS for this run."
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.days is not None:
        settings = settings.model_copy(update={"storage_retention_days": args.days})

    result = cleanup_storage(settings, dry_run=args.dry_run)
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
