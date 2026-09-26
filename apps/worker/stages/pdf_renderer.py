from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.config import Settings
from apps.worker.stages.report_branding import apply_brand_overrides, load_brand_config
from apps.worker.stages.report_payload import (
    REPORT_PAYLOAD_VERSION,
    ReportPayload,
    compose_report_payload,
)

JsonDict = dict[str, Any]
PDF_RENDERER_VERSION = "phase1-weasyprint-v1"


class PdfRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pdf_path: str
    report_metadata: JsonDict
    page_count: int = Field(ge=1)
    size_bytes: int = Field(ge=1)


def render_audit_pdf(job: Any, result: Any, settings: Settings) -> PdfRenderResult:
    payload = compose_report_payload(job, result)
    output_path = _output_path(settings.local_report_storage_dir, str(job.id))
    return render_report_pdf(
        payload,
        settings=settings,
        output_path=output_path,
        brand_overrides=getattr(job, "brand_overrides", None),
    )


def render_report_pdf(
    payload: ReportPayload,
    *,
    settings: Settings,
    output_path: Path,
    brand_overrides: JsonDict | None = None,
) -> PdfRenderResult:
    _ensure_font_cache()
    from weasyprint import HTML

    rendered_at = datetime.now(UTC)
    template_path = settings.report_template_path
    css_path = settings.report_css_path
    brand = load_brand_config(settings.brand_config_path)
    brand_context = brand.template_context(config_path=settings.brand_config_path)
    # Per-client white-label overrides (apps/shared/models.AuditJob.brand_overrides).
    brand_context = apply_brand_overrides(brand_context, brand_overrides)
    html = _render_html(
        payload=payload,
        template_path=template_path,
        css_path=css_path,
        brand_context=brand_context,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = HTML(string=html, base_url=str(template_path.parent.resolve())).render()
    document.write_pdf(target=str(output_path))

    size_bytes = output_path.stat().st_size
    report_metadata = {
        "status": "complete",
        "renderer": "weasyprint",
        "renderer_version": PDF_RENDERER_VERSION,
        "report_payload_version": REPORT_PAYLOAD_VERSION,
        "generated_at": rendered_at.isoformat(),
        "pdf_path": str(output_path),
        "pdf_size_bytes": size_bytes,
        "page_count": len(document.pages),
        "template_path": str(template_path),
        "css_path": str(css_path),
        "brand_config_path": str(settings.brand_config_path),
        "brand_logo_used": brand_context["logo_exists"],
        "brand_logo_path": brand_context["logo_path"],
        "storage": {
            "type": "local_filesystem",
            "directory": str(output_path.parent),
        },
    }
    return PdfRenderResult(
        pdf_path=str(output_path),
        report_metadata=report_metadata,
        page_count=len(document.pages),
        size_bytes=size_bytes,
    )


def render_social_pdf(job: Any, result: Any, settings: Settings) -> PdfRenderResult:
    """Render the standalone Social audit report (separate template; PDF only)."""
    from apps.worker.stages.social.report import compose_social_report_payload

    payload = compose_social_report_payload(job, result)
    output_path = _output_path(settings.local_report_storage_dir, str(job.id))
    _ensure_font_cache()
    from weasyprint import HTML

    rendered_at = datetime.now(UTC)
    template_path = settings.report_social_template_path
    if not template_path.exists():
        raise FileNotFoundError(f"Social report template does not exist: {template_path}")

    brand = load_brand_config(settings.brand_config_path)
    brand_context = apply_brand_overrides(
        brand.template_context(config_path=settings.brand_config_path),
        getattr(job, "brand_overrides", None),
    )
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)
    html = template.render(payload=payload, brand=brand_context)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = HTML(string=html, base_url=str(template_path.parent.resolve())).render()
    document.write_pdf(target=str(output_path))

    size_bytes = output_path.stat().st_size
    return PdfRenderResult(
        pdf_path=str(output_path),
        report_metadata={
            "status": "complete",
            "renderer": "weasyprint",
            "renderer_version": PDF_RENDERER_VERSION,
            "report_kind": "social",
            "report_payload_version": payload["version"],
            "generated_at": rendered_at.isoformat(),
            "pdf_path": str(output_path),
            "pdf_size_bytes": size_bytes,
            "page_count": len(document.pages),
            "template_path": str(template_path),
            "brand_config_path": str(settings.brand_config_path),
            "brand_logo_used": brand_context["logo_exists"],
            "storage": {"type": "local_filesystem", "directory": str(output_path.parent)},
        },
        page_count=len(document.pages),
        size_bytes=size_bytes,
    )


def _render_html(
    *,
    payload: ReportPayload,
    template_path: Path,
    css_path: Path,
    brand_context: JsonDict,
) -> str:
    if not template_path.exists():
        raise FileNotFoundError(f"Report template does not exist: {template_path}")
    if not css_path.exists():
        raise FileNotFoundError(f"Report CSS does not exist: {css_path}")

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)
    return template.render(
        payload=payload.model_dump(mode="json"),
        brand=brand_context,
        report_css=Markup(_render_css(css_path=css_path, brand_context=brand_context)),
    )


def _render_css(*, css_path: Path, brand_context: JsonDict) -> str:
    css_env = Environment(
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = css_env.from_string(css_path.read_text(encoding="utf-8"))
    return template.render(brand=brand_context)


def _output_path(storage_dir: Path, audit_id: str) -> Path:
    return (storage_dir / f"{audit_id}.pdf").resolve()


def _ensure_font_cache() -> None:
    if os.environ.get("XDG_CACHE_HOME"):
        cache_home = Path(os.environ["XDG_CACHE_HOME"])
    else:
        cache_home = Path(tempfile.gettempdir()) / "blc-font-cache"
        os.environ["XDG_CACHE_HOME"] = str(cache_home)
    cache_home.mkdir(parents=True, exist_ok=True)
