"""`.env.template` is the committed example config: it must document every Settings field."""

import re
from pathlib import Path

from apps.shared.config import Settings

TEMPLATE = Path(__file__).resolve().parents[2] / ".env.template"


def test_every_setting_is_documented_in_env_template() -> None:
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", TEMPLATE.read_text(), flags=re.M))
    missing = sorted(
        name.upper() for name in Settings.model_fields if name.upper() not in documented
    )
    assert not missing, f"Settings fields missing from .env.template: {missing}"
