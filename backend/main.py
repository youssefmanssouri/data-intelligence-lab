"""
Data Intelligence Lab - Local Execution Entry Point
Starts the Uvicorn ASGI server bound strictly to loopback (127.0.0.1).
Launch with:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000
    python -m backend.main --host 127.0.0.1 --port 8000
    python -m backend.main --host 127.0.0.1 --port 0 --port-file <path>   (desktop launcher)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
from typing import Optional

import uvicorn

from backend.app import create_app
from backend.config import get_settings, validate_host, validate_port


def write_port_file(path: Path, port: int) -> None:
    """
    Atomically announces ``port`` in ``path`` for the desktop launcher.

    The launcher accepts only ASCII digits, so the value is written to a sibling
    temporary file and renamed into place; a reader never sees a partial write.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(f"Refusing to announce invalid port {port!r}.")
    target = Path(path).resolve()
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "x", encoding="ascii") as handle:
            handle.write(str(port))
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def remove_port_file(path: Path, port: int) -> None:
    """Removes ``path`` only if it still holds this process's announcement."""
    target = Path(path).resolve()
    try:
        content = target.read_text(encoding="ascii")
    except FileNotFoundError:
        return
    if content == str(port):
        target.unlink(missing_ok=True)


class _AnnouncingServer(uvicorn.Server):
    """Uvicorn server that announces its bound port once it accepts connections."""

    def __init__(self, config: uvicorn.Config, port_file: Optional[Path]) -> None:
        super().__init__(config)
        self.port_file = port_file
        self.announced_port: Optional[int] = None

    async def startup(self, sockets: Optional[list[socket.socket]] = None) -> None:
        await super().startup(sockets=sockets)
        if self.port_file is None or not self.started or not sockets:
            return
        port = sockets[0].getsockname()[1]
        write_port_file(self.port_file, port)
        self.announced_port = port


def run_server(
    host: Optional[str] = None,
    port: Optional[int] = None,
    port_file: Optional[Path] = None,
) -> None:
    """
    Starts the local Uvicorn ASGI server with strict loopback binding enforcement.
    Fails closed with a ValueError before Uvicorn starts if the target host is
    not an explicitly authorized loopback address literal.

    With ``port_file``, the actually bound port (useful with ``port=0``) is
    written there once the server accepts connections, and removed on exit.
    """
    settings = get_settings()
    target_host = host if host is not None else settings.host
    target_port = validate_port(port if port is not None else settings.port)

    # Authoritative validation check at launcher boundary before starting Uvicorn
    validated_host = validate_host(target_host)

    if port_file is None:
        uvicorn.run(
            "backend.main:app",
            host=validated_host,
            port=target_port,
            reload=False,
        )
        return

    config = uvicorn.Config(
        "backend.main:app",
        host=validated_host,
        port=target_port,
        reload=False,
    )
    server = _AnnouncingServer(config, Path(port_file))
    sock = config.bind_socket()
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        if server.announced_port is not None:
            remove_port_file(server.port_file, server.announced_port)


# Module-level ASGI application instance for Uvicorn
app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Data Intelligence Lab - Local API Server")
    parser.add_argument("--host", default=None, help="Host to bind to (must be loopback, e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to (default 8000; 0 picks a free port)")
    parser.add_argument(
        "--port-file",
        default=None,
        help="File in which to announce the bound port once the server accepts connections",
    )
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, port_file=Path(args.port_file) if args.port_file else None)
