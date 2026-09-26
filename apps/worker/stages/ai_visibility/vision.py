"""Vision extraction — turn Semrush AI Visibility dashboard screenshots into typed facts.

Semrush has no API for the AI Visibility Toolkit, so we screenshot the rendered dashboard and read
the numbers with an OpenAI vision model using the SDK's structured-output parse (the same
``client.responses.parse(..., text_format=Model)`` pattern the social commentary polish uses,
extended with image inputs). The model returns an :class:`AiVisibilityExtraction`; a response with
no parsed output (a drifted shape) raises here — there is no app-level retry.

This is a *fact source* for a report section — it never touches scoring, so it does not affect
reproducibility. It runs whenever AI Visibility is enabled: automatically at the end of every
website/combined audit, and on demand via the refresh task — so each run is one paid vision call.
On any error it raises; the caller (the scraper) turns that into a ``None`` fetch so the collector
skips gracefully.
"""

from __future__ import annotations

import base64
from pathlib import Path

from apps.shared.config import Settings
from apps.worker.stages.ai_visibility.schema import AiVisibilityExtraction

_SYSTEM_PROMPT = (
    "You extract structured data from screenshots of the Semrush 'AI Visibility' dashboard. "
    "Read ONLY what is visibly printed in the images. Do not guess, infer, or invent any number "
    "or label. If a value is not clearly visible, leave it null / omit that row. Numbers like "
    "'116.9K' in the 'AI Volume' column are display strings — copy them verbatim as text. "
    "Percentages are numeric (e.g. 78.6 for '78.6%'). The visibility_score is the big gauge value "
    "out of 100 (e.g. 19), and visibility_band is its qualitative label ('Low'/'Medium'/'High')."
)

_USER_PROMPT = (
    "Extract the AI Visibility data for the domain '{domain}' from the attached Semrush dashboard "
    "screenshot(s):\n"
    "- visibility_score (0-100) and visibility_band\n"
    "- headline metrics: mentions, citations, cited_pages, share_of_voice_pct\n"
    "- per_platform: the 'Distribution by LLM' rows (platform name, mentions, share_pct)\n"
    "- topics: 'Your Performing Topics' rows (topic, visibility, your_mentions, ai_volume text)\n"
    "- competitors: any compared brands (label, visibility_score, mentions)\n"
    "- by_country: the 'Mentions by Country' rows (country, mentions, share_pct)\n"
    "Return null for anything not visible. Never fabricate."
)


def _image_data_url(path: str | Path) -> str:
    data = Path(path).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def extract_ai_visibility_from_images(
    image_paths: list[str | Path],
    *,
    domain: str,
    settings: Settings,
) -> AiVisibilityExtraction:
    """Read AI-visibility facts off one or more dashboard screenshots via an OpenAI vision model.

    Raises if OpenAI is not configured, the call fails, or the response has no parsed output — the
    caller (the scraper) converts any exception into a graceful ``None`` fetch.
    """
    from openai import OpenAI

    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not configured; cannot run vision extraction.")
    if not image_paths:
        raise ValueError("No screenshots supplied to vision extraction.")

    model = settings.ai_visibility_vision_model or settings.openai_model
    content: list[dict] = [
        {"type": "input_text", "text": _USER_PROMPT.format(domain=domain)},
    ]
    for path in image_paths:
        # detail="high" makes the model TILE the image and read each tile at full resolution, so the
        # lower panels of a very tall full-page dashboard screenshot stay legible (auto/low would
        # downscale the whole tall image and blur small numbers in the topics / by-country tables).
        content.append(
            {"type": "input_image", "image_url": _image_data_url(path), "detail": "high"}
        )

    client = OpenAI(api_key=api_key, timeout=settings.openai_timeout_seconds)
    response = client.responses.parse(
        model=model,
        instructions=_SYSTEM_PROMPT,
        input=[{"role": "user", "content": content}],
        text_format=AiVisibilityExtraction,
        max_output_tokens=settings.openai_max_tokens,
        # Extraction should be as literal as possible — a fixed low temperature, not the commentary
        # default, to minimise misreads.
        temperature=0.0,
    )
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, AiVisibilityExtraction):
        return parsed
    raise ValueError("OpenAI vision response did not include parsed AI-visibility output.")
