"""
Data Intelligence Lab - FastAPI Application Factory
Constructs the ASGI application instance, registers RFC 7807 problem details handlers,
enforces loopback-only binding, and installs the bounded multipart upload intake middleware.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.adapters.local_execution import LocalExecutionAdapter
from backend.config import (
    DEFAULT_MAX_UPLOAD_BYTES, Settings, get_settings, validate_host, validate_upload_cap,
)
from backend.errors import (
    FileSizeLimitHTTPError, _INTERNAL, _log_exception, _serialize_problem,
    register_error_handlers,
)
from backend.ports import IExecutionAdapter
from backend.request_guard import LocalRequestGuard
from backend.routes.datasets import router as datasets_router
from backend.routes.health import router as health_router
from core.types import IngestionLimits


def resolve_effective_upload_budget(
    settings: Optional[Settings],
    adapter: Optional[Any],
) -> int:
    """
    Validate the required settings cap and any present adapter caps,
    returning the minimum applicable cap: min(required_settings_cap, all_present_adapter_caps).
    Settings cap is strictly required (never optional). Adapter caps are optional
    when absent/None, but validated if present.
    If both settings and adapter are None, use DEFAULT_MAX_UPLOAD_BYTES.
    """
    if settings is None and adapter is None:
        return DEFAULT_MAX_UPLOAD_BYTES

    if settings is None:
        raise TypeError("Application settings are required to resolve upload budget.")

    raw_settings_cap = getattr(settings, "max_upload_bytes", None)
    settings_cap = validate_upload_cap(raw_settings_cap)

    adapter_caps: list[int] = []
    if adapter is not None:
        raw_adapter_cap = getattr(adapter, "max_upload_bytes", None)
        if raw_adapter_cap is not None:
            adapter_caps.append(validate_upload_cap(raw_adapter_cap))

        limits_obj = getattr(adapter, "limits", None)
        if limits_obj is not None:
            raw_limits_cap = getattr(limits_obj, "max_bytes", None)
            if raw_limits_cap is not None:
                adapter_caps.append(validate_upload_cap(raw_limits_cap))

    return min([settings_cap, *adapter_caps])


class BoundedUploadMiddleware:
    """
    ASGI middleware that enforces a strict incoming byte budget on upload endpoints
    before and during multipart streaming intake.
    Prevents unbounded file spooling to disk.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Apply specifically to multipart upload endpoints
        if scope.get("method") == "POST" and path.rstrip("/").endswith("/upload"):
            app_instance = scope.get("app")
            settings = getattr(getattr(app_instance, "state", None), "settings", None)
            adapter = getattr(getattr(app_instance, "state", None), "adapter", None)
            try:
                max_bytes = resolve_effective_upload_budget(settings, adapter)
            except (TypeError, ValueError) as exc:
                _log_exception(exc)
                response = _serialize_problem(_INTERNAL, path)
                await response(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            raw_cl = headers.get(b"content-length")
            content_length: Optional[int] = None
            if raw_cl:
                try:
                    content_length = int(raw_cl.decode("ascii"))
                except (ValueError, UnicodeDecodeError):
                    content_length = None

            bytes_received = 0

            async def bounded_receive() -> Message:
                nonlocal bytes_received
                # 1. Pre-flight check: untrusted Content-Length header check
                # Reject before reading body chunks if header already declares an oversized payload.
                if content_length is not None and content_length > max_bytes:
                    raise FileSizeLimitHTTPError(
                        max_bytes=max_bytes,
                        actual_bytes=content_length,
                    )

                # 2. Dynamic stream meter: enforce budget chunk-by-chunk for chunked/streaming
                # or missing/inaccurate Content-Length headers.
                msg = await receive()
                if msg["type"] == "http.request":
                    chunk = msg.get("body", b"")
                    bytes_received += len(chunk)
                    if bytes_received > max_bytes:
                        raise FileSizeLimitHTTPError(
                            max_bytes=max_bytes,
                            actual_bytes=bytes_received,
                        )
                return msg

            await self.app(scope, bounded_receive, send)
            return

        await self.app(scope, receive, send)


def create_app(
    storage_dir: Optional[Union[str, Path]] = None,
    adapter: Optional[IExecutionAdapter] = None,
    settings: Optional[Settings] = None,
) -> FastAPI:
    """
    Constructs and configures a FastAPI application instance.

    Args:
        storage_dir: Optional custom storage directory (useful for isolated tests).
        adapter: Optional custom execution adapter instance.
        settings: Optional configuration settings instance.

    Returns:
        Configured FastAPI application instance.
    """
    app_settings = settings or get_settings()

    # Authoritative loopback validation policy enforcement
    validate_host(app_settings.host)
    validate_upload_cap(app_settings.max_upload_bytes)

    app = FastAPI(
        title="Data Intelligence Lab",
        version="0.1.0",
        description="Local-first data intelligence and statistical analysis engine.",
    )

    # Configure application state for dependency injection
    resolved_storage_dir = storage_dir if storage_dir is not None else app_settings.storage_dir
    if adapter is not None:
        app.state.adapter = adapter
    else:
        app.state.adapter = LocalExecutionAdapter(
            storage_dir=resolved_storage_dir,
            limits=IngestionLimits(max_bytes=app_settings.max_upload_bytes),
        )
    app.state.settings = app_settings
    storage = getattr(app.state.adapter, "storage", None)
    if hasattr(storage, "sweep_stale_staging"):
        storage.sweep_stale_staging()
    resolve_effective_upload_budget(app_settings, app.state.adapter)

    # Register RFC 7807 Problem Details error boundary
    register_error_handlers(app)

    # Register bounded upload intake middleware
    app.add_middleware(BoundedUploadMiddleware)

    # Outermost: refuse non-loopback Host, cross-site and foreign-origin requests
    # before routing or body intake (see backend/request_guard.py).
    app.add_middleware(LocalRequestGuard)

    # Register API route handlers
    app.include_router(health_router)
    app.include_router(datasets_router)

    return app
