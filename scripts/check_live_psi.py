#!/usr/bin/env python
"""Live PageSpeed Insights -> Core Web Vitals diagnostic.

Calls the real PageSpeed Insights API through the exact pipeline code
(``collect_pagespeed_facts`` + ``report_payload._core_web_vitals``) for one URL and
prints the lab Core Web Vitals plus the CrUX real-user field snapshot, so the live API
response shape can be confirmed without running a full audit.

``GOOGLE_PSI_API_KEY`` is read from the environment / ``.env`` and is never printed.

Run from the repo root (key in ``.env``, or exported first):

    python scripts/check_live_psi.py https://www.example.com/

Exits 0 when PSI returns complete data, 1 otherwise (with the failure reason).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.shared.config import get_settings  # noqa: E402
from apps.worker.stages.psi_client import collect_pagespeed_facts  # noqa: E402
from apps.worker.stages.report_payload import _core_web_vitals  # noqa: E402

DEFAULT_URL = "https://www.builderleadconverter.com/"


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    settings = get_settings()

    key = settings.google_psi_api_key
    if not key or not key.get_secret_value():
        print("No GOOGLE_PSI_API_KEY in env/.env. Set it first (its value is never printed).")
        return 1

    print(f"Calling PageSpeed Insights for {url} (mobile + desktop)...\n")
    psi = collect_pagespeed_facts([url], settings)
    print(f"PSI status: {psi.get('status')} | pages analyzed: {psi.get('pages_analyzed')}")

    if psi.get("status") != "complete":
        strategies = psi.get("strategies", {})
        for name in ("mobile", "desktop"):
            error = strategies.get(name, {}).get("error")
            if error:
                print(f"  {name} error: {error}")
        if psi.get("reason"):
            print(f"  reason: {psi.get('reason')}")
        print(
            "\nPSI did not return complete data (commonly a bad/placeholder key, or the "
            "PageSpeed Insights API is not enabled on the key)."
        )
        return 1

    field = psi.get("strategies", {}).get("mobile", {}).get("field_data") or {}
    origin_present = bool(field.get("origin"))
    page_present = bool(field.get("page"))
    print(f"CrUX field data present?  origin={origin_present}  page={page_present}\n")

    cwv = _core_web_vitals(psi)

    print("--- LAB Core Web Vitals (metric: Mobile / Desktop) ---")
    for row in cwv.lab_rows:
        mobile = f"{row.mobile.value_label} ({row.mobile.rating_label})" if row.mobile else "N/A"
        desktop = (
            f"{row.desktop.value_label} ({row.desktop.rating_label})" if row.desktop else "N/A"
        )
        print(f"  {row.label:28} {mobile:26} {desktop}")

    print(
        f"\n--- FIELD data (CrUX, {cwv.field_form_factor}, {cwv.field_source}) "
        f"assessment={cwv.field_assessment} ---"
    )
    if cwv.field_available:
        for metric in cwv.field_metrics:
            print(f"  {metric.label:28} {metric.value_label:10} [{metric.rating_label}]")
    else:
        print("  (no field data — this origin has too little Chrome traffic, or PSI omitted it)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
