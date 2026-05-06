from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..services.anomaly_service import AnomalyService


class DiagnosticsIdentityPayload(BaseModel):
    init_data: str = ""
    user_id: Optional[int] = None


class DiagnosticsReportRequest(DiagnosticsIdentityPayload):
    since_hours: int = Field(default=24, ge=1, le=168)
    limit: int = Field(default=20, ge=5, le=100)


class DiagnosticsAutofixRequest(DiagnosticsIdentityPayload):
    dry_run: bool = True
    limit: int = Field(default=20, ge=1, le=100)


def create_diagnostics_router(
    resolve_identity: Callable[[Dict[str, Any]], Any],
    anomaly_service: AnomalyService,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.post("/ai/diagnostics/report")
    async def diagnostics_report(payload: DiagnosticsReportRequest) -> Dict[str, Any]:
        identity = resolve_identity(payload.model_dump())
        report = await anomaly_service.build_report(
            user_id=int(identity.user_id),
            since_hours=int(payload.since_hours),
            limit=int(payload.limit),
        )
        return {"ok": True, "report": report}

    @router.post("/ai/diagnostics/autofix")
    async def diagnostics_autofix(payload: DiagnosticsAutofixRequest) -> Dict[str, Any]:
        identity = resolve_identity(payload.model_dump())
        result = await anomaly_service.auto_fix_missing_media(
            user_id=int(identity.user_id),
            limit=int(payload.limit),
            dry_run=bool(payload.dry_run),
        )
        return {"ok": True, "result": result}

    return router
