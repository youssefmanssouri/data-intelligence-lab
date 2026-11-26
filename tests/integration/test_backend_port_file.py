"""
The desktop launcher starts the backend as
``python -m backend.main --host 127.0.0.1 --port 0 --port-file <path>`` and polls
that file for the bound port. These tests hold the backend to that contract.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

import pytest

from backend.main import remove_port_file, write_port_file

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_write_port_file_is_digits_only_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "backend.port"
    write_port_file(target, 50123)
    assert target.read_text(encoding="ascii") == "50123"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backend.port"]


@pytest.mark.parametrize("port", [0, -1, 65536, True, "8000", 1.0])
def test_write_port_file_rejects_invalid_ports(tmp_path: Path, port: object) -> None:
    with pytest.raises(ValueError):
        write_port_file(tmp_path / "p", port)  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


def test_remove_port_file_keeps_a_newer_announcement(tmp_path: Path) -> None:
    target = tmp_path / "backend.port"
    target.write_text("40000", encoding="ascii")
    remove_port_file(target, 41000)
    assert target.read_text(encoding="ascii") == "40000"
    remove_port_file(target, 40000)
    assert not target.exists()
    remove_port_file(target, 40000)  # already gone: no error


def test_backend_announces_dynamic_port_and_serves_health(tmp_path: Path) -> None:
    port_file = tmp_path / "backend.port"
    env = dict(os.environ)
    env["STORAGE_DIR"] = str(tmp_path / "storage")
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.main", "--host", "127.0.0.1", "--port", "0", "--port-file", str(port_file)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 60
        content = ""
        while time.monotonic() < deadline and proc.poll() is None:
            if port_file.is_file():
                content = port_file.read_text(encoding="ascii")
                break
            time.sleep(0.1)
        assert content.isdigit(), f"no port announced (exit={proc.poll()})"
        port = int(content)
        assert 1 <= port <= 65535

        # Announced only once the server accepts connections: no retry needed.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
            assert resp.status == 200

        if sys.platform != "win32":
            proc.send_signal(signal.SIGINT)
            # Uvicorn shuts down gracefully, then re-raises the captured signal.
            assert proc.wait(timeout=20) in (0, -signal.SIGINT)
            assert not port_file.exists(), "port file must be removed on graceful exit"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stderr:
            proc.stderr.close()
