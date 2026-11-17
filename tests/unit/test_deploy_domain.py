"""The public domain is spelled in three places that must agree: the API base baked into the UI
build, the API's CORS origins and the deploy's public health check (DEPLOYMENT.md §9, the address
change of November 2026). A mismatch only shows after a deploy, so it is pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MAIN_DOMAIN = "visllm.builderleadconverter.com"
# The first address. It still serves the same UI for links already shared, and its pages call the
# API on the main domain, so the API must allow it as an origin.
FIRST_DOMAIN = "seo.builderleadconverter.com"


def _as_deployed(value: str) -> str:
    # Compose resolves ${RICK_DOMAIN:-<default>} to the default on the box, where RICK_DOMAIN is
    # unset (it exists only for rehearsals on another hostname).
    return re.sub(r"\$\{RICK_DOMAIN:-([^}]+)\}", r"\1", value)


def test_the_ui_build_the_api_and_the_deploy_check_use_the_same_domain() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]

    api_base = services["frontend"]["build"]["args"]["NEXT_PUBLIC_API_BASE_URL"]
    assert _as_deployed(api_base) == f"https://{MAIN_DOMAIN}/api"

    origins = _as_deployed(services["api"]["environment"]["API_CORS_ORIGINS"]).split(",")
    assert origins == [f"https://{MAIN_DOMAIN}", f"https://{FIRST_DOMAIN}"]

    deploy_script = (ROOT / "deploy/deploy.sh").read_text()
    assert f"BLC_RICK_PUBLIC_HEALTH_URL:-https://{MAIN_DOMAIN}/api/health" in deploy_script
