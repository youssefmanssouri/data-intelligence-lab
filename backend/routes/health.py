"""
Data Intelligence Lab - Health and Readiness Routes
Provides liveness (/health) and storage readiness (/ready) probes.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from backend.errors import build_problem_response
from backend.dependencies import get_execution_adapter
from backend.ports import IExecutionAdapter

router = APIRouter(tags=["health"])


@router.get("/health", status_code=status.HTTP_200_OK)
def health_check() -> Dict[str, str]:
    """
    Liveness probe.
    Confirms that the HTTP server process is running and responsive.
    Strictly isolated: does NOT perform disk I/O, storage checks, or network operations.
    """
    return {"status": "ok"}


@router.get("/ready")
def readiness_check(
    adapter: IExecutionAdapter = Depends(get_execution_adapter),
) -> JSONResponse:
    """
    Readiness probe.
    Confirms that the underlying storage subsystem is operational and writable.
    Returns 200 OK if ready, 503 Service Unavailable if storage cannot be written.
    """
    if adapter.check_storage_readiness():
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready"},
        )
    return build_problem_response(
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
        title="Service Unavailable",
        detail="Storage subsystem is not ready or writable.",
        instance="/ready",
        code="ERR_STORAGE_NOT_READY",
    )

