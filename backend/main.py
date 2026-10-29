"""
Data Intelligence Lab - Local Execution Entry Point
Starts the Uvicorn ASGI server bound strictly to loopback (127.0.0.1).
Launch with:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000
    python -m backend.main --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
from typing import Optional

import uvicorn

from backend.app import create_app
from backend.config import get_settings, validate_host


def run_server(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """
    Starts the local Uvicorn ASGI server with strict loopback binding enforcement.
    Fails closed with a ValueError before Uvicorn starts if the target host is
    not an explicitly authorized loopback address literal.
    """
    settings = get_settings()
    target_host = host if host is not None else settings.host
    target_port = port if port is not None else settings.port

    # Authoritative validation check at launcher boundary before starting Uvicorn
    validated_host = validate_host(target_host)

    uvicorn.run(
        "backend.main:app",
        host=validated_host,
        port=target_port,
        reload=False,
    )


# Module-level ASGI application instance for Uvicorn
app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data Intelligence Lab - Local API Server")
    parser.add_argument("--host", default=None, help="Host to bind to (must be loopback, e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to (default 8000)")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port)
