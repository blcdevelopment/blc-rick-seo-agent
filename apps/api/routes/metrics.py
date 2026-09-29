"""Operational metrics endpoint (gated like /audits). Returns aggregate audit + storage
stats as JSON for an internal dashboard / uptime check — no Prometheus stack required."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.auth import require_user
from apps.api.deps import get_db_session
from apps.shared.config import get_settings
from apps.shared.metrics import collect_metrics

router = APIRouter(tags=["metrics"], dependencies=[Depends(require_user)])
DbSession = Annotated[Session, Depends(get_db_session)]


@router.get("/metrics")
def get_metrics(db: DbSession) -> dict:
    return collect_metrics(db, get_settings())
