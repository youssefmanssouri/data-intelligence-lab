"""
Data Intelligence Lab - FastAPI Dependencies
Provides dependency injection providers for route handlers.
"""

from __future__ import annotations

from fastapi import Request

from backend.adapters.local_execution import LocalExecutionAdapter
from backend.ports import IExecutionAdapter


def get_execution_adapter(request: Request) -> IExecutionAdapter:
    """
    Retrieves the configured execution adapter from application state.
    Falls back to a default LocalExecutionAdapter if not explicitly set.
    """
    if hasattr(request.app.state, "adapter") and request.app.state.adapter is not None:
        return request.app.state.adapter
    return LocalExecutionAdapter()
