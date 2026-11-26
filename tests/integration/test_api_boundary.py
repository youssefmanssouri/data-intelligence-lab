"""
Integration Test Suite for Milestone 4B-1: Local API Boundary
Validates:
- B1-01: Authoritative loopback-only binding policy across configuration and launcher boundaries.
- B1-02: Complete RFC 7807 problem details sanitization across all public fields without path/traceback leakage.
- B1-03: Bounded multipart intake enforcing limits before and during streaming receive, preventing unbounded spooling.
- B1-04: Clean decoupled headless imports (ports and adapters independent of FastAPI/Starlette).
- All frozen endpoints (/health, /ready, /upload, /datasets/{id} GET and DELETE).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, BinaryIO, Dict, List, Optional, Union
import uuid

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
import pyarrow.parquet as pq
import pytest
import starlette.formparsers as sfp
from starlette.testclient import TestClient

from backend.adapters.exceptions import (
    DatasetNotFoundError,
    InvalidDatasetIdError,
    ParquetConversionError,
    StorageError,
)
from backend.adapters.local_execution import LocalExecutionAdapter
from backend.adapters import exceptions as storage_errors
from backend.app import create_app, resolve_effective_upload_budget
from backend.config import DEFAULT_MAX_UPLOAD_BYTES, MAX_CONFIGURED_UPLOAD_BYTES, Settings, validate_host
from backend.errors import FileSizeLimitHTTPError
from backend.main import run_server
from backend.ports import IExecutionAdapter
from core.types import IngestionLimits, InferencePolicy
from core import types as core_errors


# =====================================================================
# Mock / Fault Injection Execution Adapter
# =====================================================================

class StubExecutionAdapter:
    """Stub adapter implementing IExecutionAdapter for isolated fault injection."""

    def __init__(
        self,
        readiness: bool = True,
        raise_on_upload: Optional[Exception] = None,
        max_upload_bytes: Optional[int] = None,
    ) -> None:
        self._readiness = readiness
        self._raise_on_upload = raise_on_upload
        self.max_upload_bytes = max_upload_bytes
        self.limits = IngestionLimits(max_bytes=max_upload_bytes or 200 * 1024 * 1024)

    def ingest_and_store(
        self,
        file_source: Union[BinaryIO, bytes],
        filename: Optional[str] = None,
        dataset_id: Optional[str] = None,
        limits: Optional[IngestionLimits] = None,
        policy: Optional[InferencePolicy] = None,
    ) -> Dict[str, Any]:
        if self._raise_on_upload:
            raise self._raise_on_upload
        return {
            "dataset_id": dataset_id or "ds_stub_001",
            "filename": filename or "test.csv",
            "created_at": "2026-10-27T12:00:00Z",
            "row_count": 10,
            "column_count": 2,
            "data_quality_score": 1.0,
        }

    def get_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        if dataset_id == "ds_missing":
            raise DatasetNotFoundError(dataset_id)
        if ".." in dataset_id or "/" in dataset_id or "\\" in dataset_id:
            raise InvalidDatasetIdError("Path traversal rejected")
        return {"dataset_id": dataset_id, "row_count": 10}

    def delete_dataset(self, dataset_id: str) -> bool:
        if dataset_id == "ds_missing":
            return False
        if ".." in dataset_id:
            raise InvalidDatasetIdError("Path traversal rejected")
        return True

    def check_storage_readiness(self) -> bool:
        return self._readiness


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def isolated_storage(tmp_path: Path) -> Path:
    """Provides a fresh isolated storage directory per test."""
    storage_dir = tmp_path / "dil_api_storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


@pytest.fixture
def app(isolated_storage: Path):
    """Constructs application configured with isolated temporary storage."""
    return create_app(storage_dir=isolated_storage)


@pytest.fixture
def client(app):
    """Provides a TestClient with server exceptions contained (raise_server_exceptions=False)."""
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1")


# =====================================================================
# 1. B1-01: Authoritative Loopback Host Validation Policy
# =====================================================================

@pytest.mark.parametrize(
    "valid_host,expected_canonical",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.2", "127.0.0.2"),
        ("127.255.255.254", "127.255.255.254"),
        ("::1", "::1"),
        ("[::1]", "::1"),
    ],
)
def test_b1_01_validate_host_accepted_loopback_literals(valid_host: str, expected_canonical: str) -> None:
    """Proves only explicitly classified loopback IP address literals are accepted."""
    result = validate_host(valid_host)
    assert result == expected_canonical


@pytest.mark.parametrize(
    "rejected_host",
    [
        "0.0.0.0",                 # IPv4 wildcard
        "::",                      # IPv6 wildcard
        "0",                       # Integer / shorthand wildcard
        "",                        # Empty string
        "   ",                     # Whitespace only
        " 127.0.0.1 ",             # Surrounding whitespace
        "127.0.0.1:8000",          # Port appended
        "http://127.0.0.1",        # Scheme included
        "localhost",               # Hostname (can resolve to non-loopback or rebinding)
        "local",                   # Arbitrary hostname
        "example.com",             # Public domain
        "192.168.1.1",             # Private LAN (Class C)
        "10.0.0.1",                # Private LAN (Class A)
        "172.16.0.1",              # Private LAN (Class B)
        "169.254.1.1",             # Link-local address
        "8.8.8.8",                 # Public Internet IPv4
        "2001:4860:4860::8888",    # Public Internet IPv6
        "256.256.256.256",         # Out-of-bounds IPv4
        "invalid_host_string",     # Unparseable token
    ],
)
def test_b1_01_validate_host_rejected_non_loopback(rejected_host: str) -> None:
    """Proves wildcards, hostnames, LAN, public IPs, and malformed inputs fail closed."""
    with pytest.raises(ValueError):
        validate_host(rejected_host)


def test_b1_01_settings_enforces_authoritative_host_policy() -> None:
    """Proves Settings constructor enforces the authoritative host policy."""
    with pytest.raises(ValueError):
        Settings(host="0.0.0.0")

    with pytest.raises(ValueError):
        Settings(host="192.168.1.50")

    with pytest.raises(ValueError):
        Settings(host="localhost")

    # Valid loopback succeeds
    s = Settings(host="127.0.0.1")
    assert s.host == "127.0.0.1"


def test_b1_01_app_factory_enforces_authoritative_host_policy() -> None:
    """Proves create_app fails closed if given a non-loopback configuration."""
    bad_settings = object.__new__(Settings)
    bad_settings.host = "0.0.0.0"
    bad_settings.port = 8000
    bad_settings.storage_dir = Path("./tmp")
    bad_settings.max_upload_bytes = 200 * 1024 * 1024
    bad_settings.environment = "development"

    with pytest.raises(ValueError):
        create_app(settings=bad_settings)


def test_b1_01_launcher_fails_closed_before_uvicorn_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves run_server fails closed with ValueError before Uvicorn starts."""
    uvicorn_called = False

    def fake_uvicorn_run(*args: Any, **kwargs: Any) -> None:
        nonlocal uvicorn_called
        uvicorn_called = True

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)

    for bad_host in ("0.0.0.0", "192.168.1.1", "localhost", "::"):
        with pytest.raises(ValueError):
            run_server(host=bad_host)
        assert not uvicorn_called, f"Uvicorn was invoked for invalid host: {bad_host}"


# =====================================================================
# 2. Application Factory & Dependency Injection
# =====================================================================

def test_app_factory_isolated_storage(tmp_path: Path) -> None:
    """Application factory respects custom isolated storage directories."""
    custom_dir = tmp_path / "custom_storage"
    application = create_app(storage_dir=custom_dir)
    assert application.state.adapter.storage.datasets_dir == custom_dir / "datasets"


def test_app_factory_dependency_injection() -> None:
    """Application factory supports dependency injection of IExecutionAdapter."""
    stub = StubExecutionAdapter(readiness=True)
    application = create_app(adapter=stub)
    assert application.state.adapter is stub


# =====================================================================
# 3. Health Probe (Liveness) & Readiness Probe
# =====================================================================

def test_health_returns_200_ok(client: TestClient) -> None:
    """GET /health returns 200 with exact status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_isolated_from_storage_state() -> None:
    """GET /health succeeds even when storage backend readiness fails."""
    unready_adapter = StubExecutionAdapter(readiness=False)
    application = create_app(adapter=unready_adapter)
    test_client = TestClient(application, raise_server_exceptions=False, base_url="http://127.0.0.1")

    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_returns_200_when_storage_writable(client: TestClient) -> None:
    """GET /ready returns 200 with status ready when storage is writable."""
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_returns_503_when_storage_unready() -> None:
    """GET /ready returns 503 problem details when storage readiness check fails."""
    unready_adapter = StubExecutionAdapter(readiness=False)
    application = create_app(adapter=unready_adapter)
    test_client = TestClient(application, raise_server_exceptions=False, base_url="http://127.0.0.1")

    response = test_client.get("/ready")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == 503
    assert body["code"] == "ERR_STORAGE_NOT_READY"
    assert body["title"] == "Service Unavailable"


def test_readiness_with_real_unwritable_storage_dir(tmp_path: Path) -> None:
    """GET /ready returns 503 when the underlying filesystem storage directory cannot be created."""
    blocked_dir = tmp_path / "blocked_storage"
    blocked_dir.mkdir(parents=True)
    datasets_file = blocked_dir / "datasets"
    datasets_file.write_text("blocking_file")

    app_with_broken_storage = create_app(storage_dir=blocked_dir)
    client_broken = TestClient(app_with_broken_storage, raise_server_exceptions=False, base_url="http://127.0.0.1")

    response = client_broken.get("/ready")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ERR_STORAGE_NOT_READY"


# =====================================================================
# 4. B1-03: Bounded Multipart Intake Before Spooling
# =====================================================================

def test_b1_03_settings_upload_limit_enforced_with_default_adapter(isolated_storage: Path) -> None:
    """Settings(max_upload_bytes=500) with default adapter rejects upload > 500 bytes with HTTP 413."""
    app_instance = create_app(
        storage_dir=isolated_storage,
        settings=Settings(max_upload_bytes=500),
    )
    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")

    large_csv = b"col1,col2\n" + b"12345,67890\n" * 50  # ~600 bytes
    files = {"file": ("large.csv", io.BytesIO(large_csv), "text/csv")}
    response = test_client.post("/api/v1/datasets/upload", files=files)

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_FILE_SIZE_LIMIT_EXCEEDED"
    assert body["title"] == "Payload Too Large"
    assert "500 bytes" in body["detail"]


def test_b1_03_environment_max_upload_bytes_enforced(
    monkeypatch: pytest.MonkeyPatch, isolated_storage: Path
) -> None:
    """MAX_UPLOAD_BYTES=500 in environment enforces 500-byte cap via ordinary create_app() path."""
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "500")
    app_instance = create_app(storage_dir=isolated_storage)
    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")

    large_csv = b"col1,col2\n" + b"12345,67890\n" * 50  # ~600 bytes
    files = {"file": ("large.csv", io.BytesIO(large_csv), "text/csv")}
    response = test_client.post("/api/v1/datasets/upload", files=files)

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_FILE_SIZE_LIMIT_EXCEEDED"
    assert body["title"] == "Payload Too Large"


def test_b1_03_limit_precedence_injected_vs_settings(isolated_storage: Path) -> None:
    """
    Proves limit precedence contract:
    1. Smaller configured HTTP limit is NEVER overridden by a larger adapter default.
    2. Explicitly injected adapter with smaller limit is respected.
    """
    # 1. Smaller Settings (500) + default 200MB adapter -> 500 enforced
    adapter_default = LocalExecutionAdapter(storage_dir=isolated_storage)
    app1 = create_app(adapter=adapter_default, settings=Settings(max_upload_bytes=500))
    c1 = TestClient(app1, raise_server_exceptions=False, base_url="http://127.0.0.1")
    res1 = c1.post("/api/v1/datasets/upload", files={"file": ("f.csv", io.BytesIO(b"x\n" + b"1\n" * 300), "text/csv")})
    assert res1.status_code == 413
    assert "500 bytes" in res1.json()["detail"]

    # 2. Injected adapter with smaller limit (300) + default Settings (200MB) -> 300 enforced
    adapter_small = StubExecutionAdapter(max_upload_bytes=300)
    app2 = create_app(adapter=adapter_small, settings=Settings(max_upload_bytes=200 * 1024 * 1024))
    c2 = TestClient(app2, raise_server_exceptions=False, base_url="http://127.0.0.1")
    res2 = c2.post("/api/v1/datasets/upload", files={"file": ("f.csv", io.BytesIO(b"x\n" + b"1\n" * 200), "text/csv")})
    assert res2.status_code == 413
    assert "300 bytes" in res2.json()["detail"]

    # 3. Injected adapter with larger limit (1000) + smaller Settings (500) -> 500 enforced
    adapter_large = StubExecutionAdapter(max_upload_bytes=1000)
    app3 = create_app(adapter=adapter_large, settings=Settings(max_upload_bytes=500))
    c3 = TestClient(app3, raise_server_exceptions=False, base_url="http://127.0.0.1")
    res3 = c3.post("/api/v1/datasets/upload", files={"file": ("f.csv", io.BytesIO(b"x\n" + b"1\n" * 300), "text/csv")})
    assert res3.status_code == 413
    assert "500 bytes" in res3.json()["detail"]


@pytest.mark.parametrize(
    "header_kind",
    ["truthful_oversized", "absent", "invalid", "misleading_small"],
)
def test_b1_03_content_length_variations(header_kind: str) -> None:
    """Proves byte budget is enforced across truthful, absent, invalid, and misleading Content-Length headers."""
    async def _run() -> None:
        adapter = StubExecutionAdapter(max_upload_bytes=400)
        app_instance = create_app(adapter=adapter)

        boundary = "----TestBoundaryContentLengthVariations"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="oversized.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode()

        total_bytes_to_send = 1000
        raw_chunks = [header, b"A" * 300, b"B" * 300, b"C" * 400, f"\r\n--{boundary}--\r\n".encode()]
        chunk_iter = iter(raw_chunks)
        chunks_consumed = 0

        async def recv() -> Dict[str, Any]:
            nonlocal chunks_consumed
            try:
                c = next(chunk_iter)
                chunks_consumed += 1
                return {"type": "http.request", "body": c, "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        response_data: Dict[str, Any] = {}
        async def send(msg: Dict[str, Any]) -> None:
            if msg["type"] == "http.response.start":
                response_data["status"] = msg["status"]
            elif msg["type"] == "http.response.body":
                response_data["body"] = msg.get("body", b"")

        headers_list = [(b"host", b"127.0.0.1:8000"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
        if header_kind == "truthful_oversized":
            headers_list.append((b"content-length", str(total_bytes_to_send).encode()))
        elif header_kind == "invalid":
            headers_list.append((b"content-length", b"invalid_number"))
        elif header_kind == "misleading_small":
            headers_list.append((b"content-length", b"20"))
        # "absent" includes no content-length header

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/datasets/upload",
            "raw_path": b"/api/v1/datasets/upload",
            "query_string": b"",
            "headers": headers_list,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }

        await app_instance(scope, recv, send)

        assert response_data["status"] == 413
        if header_kind == "truthful_oversized":
            # Pre-flight check rejected before any chunk was read
            assert chunks_consumed == 0
        else:
            # Dynamic stream meter caught it early without reading all chunks
            assert chunks_consumed < len(raw_chunks)

    asyncio.run(_run())


def test_b1_03_instrumented_receive_cutoff_and_no_downstream_publication(
    isolated_storage: Path,
) -> None:
    """
    Direct instrumentation test: proves streaming upload exceeding budget
    aborts at receive before crossing chunk reaches multipart parser, later messages
    are not consumed, and no downstream ingestion/publication occurs.
    """
    async def _run() -> None:
        ingest_called = False

        class PublicationSpyAdapter(LocalExecutionAdapter):
            def ingest_and_store(self, file_source: Any, filename: Optional[str] = None) -> Dict[str, Any]:
                nonlocal ingest_called
                ingest_called = True
                return super().ingest_and_store(file_source, filename)

        adapter = PublicationSpyAdapter(
            storage_dir=isolated_storage,
            limits=IngestionLimits(max_bytes=400),
        )
        app_instance = create_app(adapter=adapter, settings=Settings(max_upload_bytes=400))

        boundary = "----TestBoundaryPreSpoolCheck"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="oversized.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode()

        total_data_chunks = 30
        raw_chunks = [header] + [b"X" * 150 for _ in range(total_data_chunks)] + [f"\r\n--{boundary}--\r\n".encode()]
        chunk_iter = iter(raw_chunks)
        chunks_consumed = 0

        async def instrumented_receive() -> Dict[str, Any]:
            nonlocal chunks_consumed
            try:
                chunk = next(chunk_iter)
                chunks_consumed += 1
                return {"type": "http.request", "body": chunk, "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        response_data: Dict[str, Any] = {}
        async def instrumented_send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_data["status"] = message["status"]

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/datasets/upload",
            "raw_path": b"/api/v1/datasets/upload",
            "query_string": b"",
            "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }

        await app_instance(scope, instrumented_receive, instrumented_send)

        # 1. 413 Payload Too Large
        assert response_data["status"] == 413

        # 2. Pre-spooling cutoff: only ~3 chunks consumed, not all 32
        assert chunks_consumed < len(raw_chunks)
        assert chunks_consumed <= 4

        # 3. Ingestion adapter was NEVER called
        assert not ingest_called

        # 4. Storage directory has no published datasets
        dataset_dirs = list((isolated_storage / "datasets").glob("*")) if (isolated_storage / "datasets").exists() else []
        assert len(dataset_dirs) == 0

    asyncio.run(_run())


def test_b1_03_partial_spools_closed_on_rejection_and_cancellation() -> None:
    """Proves partial upload spools close on both 413 rejection and client cancellation."""
    async def _run() -> None:
        boundary = "----TestBoundarySpoolClose"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="oversized.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode()

        # 1. Rejection path
        rejection_spools: List[Any] = []
        orig_spool = sfp.SpooledTemporaryFile
        def tracking_spooled_rejection(*args: Any, **kwargs: Any) -> Any:
            s = orig_spool(*args, **kwargs)
            rejection_spools.append(s)
            return s

        sfp.SpooledTemporaryFile = tracking_spooled_rejection
        try:
            app_rejection = create_app(settings=Settings(max_upload_bytes=400))
            raw_chunks = [header, b"A" * 200, b"B" * 200, b"C" * 200, f"\r\n--{boundary}--\r\n".encode()]
            chunk_iter = iter(raw_chunks)

            async def recv_rejection() -> Dict[str, Any]:
                try:
                    c = next(chunk_iter)
                    return {"type": "http.request", "body": c, "more_body": True}
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            scope_rejection = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/datasets/upload",
                "raw_path": b"/api/v1/datasets/upload",
                "query_string": b"",
                "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8000),
            }
            resp: Dict[str, Any] = {}
            async def send_rejection(msg: Dict[str, Any]) -> None:
                if msg["type"] == "http.response.start":
                    resp["status"] = msg["status"]

            await app_rejection(scope_rejection, recv_rejection, send_rejection)
            assert resp["status"] == 413
            assert len(rejection_spools) > 0
            assert all(s.closed for s in rejection_spools)
        finally:
            sfp.SpooledTemporaryFile = orig_spool

        # 2. Cancellation path
        cancellation_spools: List[Any] = []
        def tracking_spooled_cancellation(*args: Any, **kwargs: Any) -> Any:
            s = orig_spool(*args, **kwargs)
            cancellation_spools.append(s)
            return s

        sfp.SpooledTemporaryFile = tracking_spooled_cancellation
        try:
            app_cancellation = create_app(settings=Settings(max_upload_bytes=1000))
            raw_chunks_c = [header, b"A" * 200, b"B" * 200]
            chunk_iter_c = iter(raw_chunks_c)
            step = 0

            async def recv_cancellation() -> Dict[str, Any]:
                nonlocal step
                step += 1
                if step == 2:
                    raise asyncio.CancelledError("Client disconnected")
                c = next(chunk_iter_c)
                return {"type": "http.request", "body": c, "more_body": True}

            scope_cancellation = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/datasets/upload",
                "raw_path": b"/api/v1/datasets/upload",
                "query_string": b"",
                "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8000),
            }
            async def send_cancellation(msg: Dict[str, Any]) -> None:
                pass

            try:
                await app_cancellation(scope_cancellation, recv_cancellation, send_cancellation)
            except asyncio.CancelledError:
                pass

            assert len(cancellation_spools) > 0
            assert all(s.closed for s in cancellation_spools)
        finally:
            sfp.SpooledTemporaryFile = orig_spool

    asyncio.run(_run())


def test_b1_03_small_upload_and_health_readiness_succeed(isolated_storage: Path) -> None:
    """Valid small upload succeeds under configured budget, and health/readiness remain functional."""
    app_instance = create_app(
        storage_dir=isolated_storage,
        settings=Settings(max_upload_bytes=500),
    )
    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")

    # Health & readiness
    res_health = test_client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json() == {"status": "ok"}

    res_ready = test_client.get("/ready")
    assert res_ready.status_code == 200
    assert res_ready.json() == {"status": "ready"}

    # Small valid CSV (approx 40 bytes) under 500 byte limit
    small_csv = b"col1,col2\n1,2\n3,4\n"
    files = {"file": ("small.csv", io.BytesIO(small_csv), "text/csv")}
    res_upload = test_client.post("/api/v1/datasets/upload", files=files)

    assert res_upload.status_code == 201
    data = res_upload.json()
    dataset_id = data["dataset_id"]
    assert dataset_id.startswith("ds_")
    assert (isolated_storage / "datasets" / dataset_id / "data.parquet").exists()
    assert (isolated_storage / "datasets" / dataset_id / "metadata.json").exists()


# =====================================================================
# 5. Dataset Upload Normal and Edge Cases
# =====================================================================

def test_upload_valid_csv_persists_artifacts(
    client: TestClient, isolated_storage: Path
) -> None:
    """POST /api/v1/datasets/upload ingests and writes Parquet and metadata artifacts."""
    csv_bytes = b"id,name,score\n1,Alpha,95.5\n2,Beta,88.0\n"
    files = {"file": ("grades.csv", io.BytesIO(csv_bytes), "text/csv")}

    response = client.post("/api/v1/datasets/upload", files=files)
    assert response.status_code == 201

    data = response.json()
    dataset_id = data["dataset_id"]
    assert dataset_id.startswith("ds_")
    assert data["filename"] == "grades.csv"
    assert data["row_count"] == 2
    assert data["column_count"] == 3
    assert data["data_quality_score"] is not None

    # Verify physical artifacts on disk
    dataset_dir = isolated_storage / "datasets" / dataset_id
    parquet_path = dataset_dir / "data.parquet"
    metadata_path = dataset_dir / "metadata.json"

    assert parquet_path.exists()
    assert metadata_path.exists()

    # Verify Parquet content
    table = pq.read_table(parquet_path)
    assert table.num_rows == 2
    assert table.num_columns == 3


def test_upload_empty_dataset_returns_400(client: TestClient) -> None:
    """POST /api/v1/datasets/upload with empty CSV returns 400 ERR_EMPTY_DATASET."""
    files = {"file": ("empty.csv", io.BytesIO(b""), "text/csv")}
    response = client.post("/api/v1/datasets/upload", files=files)

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_EMPTY_DATASET"
    assert body["status"] == 400


def test_upload_malformed_csv_returns_422(client: TestClient) -> None:
    """POST /api/v1/datasets/upload with syntax error returns 422 ERR_MALFORMED_CSV."""
    malformed_csv = b'id,val\n1,"unclosed quote\n2,normal\n'
    files = {"file": ("malformed.csv", io.BytesIO(malformed_csv), "text/csv")}
    response = client.post("/api/v1/datasets/upload", files=files)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_MALFORMED_CSV"
    assert body["status"] == 422


def test_upload_mixed_offset_returns_422_parquet_conversion(client: TestClient) -> None:
    """Mixed UTC and named offsets fail in Parquet conversion returning 422 ERR_PARQUET_CONVERSION."""
    mixed_tz_csv = (
        b"event_time\n"
        b"2024-01-01T10:00:00Z\n"
        b"2024-01-01T12:00:00+02:00\n"
    )
    files = {"file": ("mixed_tz.csv", io.BytesIO(mixed_tz_csv), "text/csv")}
    response = client.post("/api/v1/datasets/upload", files=files)

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_PARQUET_CONVERSION"
    assert body["status"] == 422


def test_upload_missing_file_field_validation_error(client: TestClient) -> None:
    """POST /api/v1/datasets/upload without file field returns 422 ERR_REQUEST_VALIDATION."""
    response = client.post("/api/v1/datasets/upload", data={"other": "field"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_REQUEST_VALIDATION"
    assert body["status"] == 422


# =====================================================================
# 6. Metadata Retrieval (GET /api/v1/datasets/{dataset_id})
# =====================================================================

def test_get_dataset_metadata_success(client: TestClient) -> None:
    """GET /api/v1/datasets/{dataset_id} returns 200 with stored metadata."""
    csv_bytes = b"col_a,col_b\nhello,100\nworld,200\n"
    files = {"file": ("data.csv", io.BytesIO(csv_bytes), "text/csv")}
    upload_res = client.post("/api/v1/datasets/upload", files=files)
    assert upload_res.status_code == 201
    dataset_id = upload_res.json()["dataset_id"]

    get_res = client.get(f"/api/v1/datasets/{dataset_id}")
    assert get_res.status_code == 200
    metadata = get_res.json()
    assert metadata["dataset_id"] == dataset_id
    assert metadata["row_count"] == 2
    assert metadata["column_count"] == 2
    assert "columns" in metadata


def test_get_dataset_metadata_missing_404(client: TestClient) -> None:
    """GET /api/v1/datasets/{dataset_id} for nonexistent dataset returns 404 ERR_DATASET_NOT_FOUND."""
    response = client.get("/api/v1/datasets/ds_nonexistent_999")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_DATASET_NOT_FOUND"
    assert body["status"] == 404


def test_get_dataset_metadata_invalid_id_returns_400(client: TestClient) -> None:
    """GET /api/v1/datasets/{dataset_id} with invalid or traversal ID returns 400 ERR_INVALID_DATASET_ID."""
    response_traversal = client.get("/api/v1/datasets/bad..id")
    assert response_traversal.status_code == 400
    assert response_traversal.headers["content-type"] == "application/problem+json"
    assert response_traversal.json()["code"] == "ERR_INVALID_DATASET_ID"


# =====================================================================
# 7. Dataset Deletion (DELETE /api/v1/datasets/{dataset_id})
# =====================================================================

def test_delete_dataset_success(client: TestClient, isolated_storage: Path) -> None:
    """DELETE /api/v1/datasets/{dataset_id} removes artifacts and returns 204 No Content."""
    csv_bytes = b"x,y\n1,2\n"
    files = {"file": ("data.csv", io.BytesIO(csv_bytes), "text/csv")}
    upload_res = client.post("/api/v1/datasets/upload", files=files)
    dataset_id = upload_res.json()["dataset_id"]

    dataset_dir = isolated_storage / "datasets" / dataset_id
    assert dataset_dir.exists()

    del_res = client.delete(f"/api/v1/datasets/{dataset_id}")
    assert del_res.status_code == 204
    assert del_res.content == b""
    assert not dataset_dir.exists()

    # Subsequent GET must return 404
    get_res = client.get(f"/api/v1/datasets/{dataset_id}")
    assert get_res.status_code == 404


def test_delete_dataset_missing_returns_404(client: TestClient) -> None:
    """DELETE /api/v1/datasets/{dataset_id} for nonexistent dataset returns 404 ERR_DATASET_NOT_FOUND."""
    response = client.delete("/api/v1/datasets/ds_missing_000")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ERR_DATASET_NOT_FOUND"


def test_delete_dataset_invalid_id_returns_400(client: TestClient) -> None:
    """DELETE /api/v1/datasets/{dataset_id} with invalid ID returns 400 ERR_INVALID_DATASET_ID."""
    response = client.delete("/api/v1/datasets/invalid..id")
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "ERR_INVALID_DATASET_ID"


# =====================================================================
# 8. B1-02: RFC 7807 Error Sanitization Across All Public Fields
# =====================================================================

def test_b1_02_rfc7807_fields_conformance(client: TestClient) -> None:
    """Problem responses contain type, title, status, detail, instance, and code."""
    response = client.get("/api/v1/datasets/ds_not_found_123")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"

    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Dataset Not Found"
    assert body["status"] == 404
    assert isinstance(body["detail"], str)
    assert body["instance"] == "/api/v1/datasets/ds_not_found_123"
    assert body["code"] == "ERR_DATASET_NOT_FOUND"


ALL_AUDIT_PATH_CASES = [
    "/mnt/AUDIT_PRIVATE_SENTINEL/file.csv",
    "/Volumes/AUDIT_PRIVATE_SENTINEL Disk/file.csv",
    "\\\\AUDIT_PRIVATE_SENTINEL_SERVER\\Confidential Share\\file.csv",
    "backend/AUDIT_PRIVATE_SENTINEL/file.csv",
    "C:\\Users\\O'Brien\\AUDIT_PRIVATE_SENTINEL\\file.csv",
    "/opt/PRIVATE_SENTINEL/backend/private.py",
    "C:\\Users\\User Name\\Secret Folder\\private.py",
    "../private folder/confidential.txt",
]

SENSITIVE_SUBSTRINGS = [
    "AUDIT_PRIVATE_SENTINEL",
    "Confidential Share",
    "O'Brien",
    "PRIVATE_SENTINEL",
    "Secret Folder",
    "private folder",
    "confidential.txt",
    "/mnt/",
    "/Volumes/",
    "\\\\AUDIT_PRIVATE_SENTINEL_SERVER",
]


@pytest.mark.parametrize("path_case", ALL_AUDIT_PATH_CASES)
def test_b1_02_storage_failures_serialized_response_all_audit_paths(path_case: str) -> None:
    """
    Proves that storage failures across all audit path cases:
    1. Return HTTP 500 application/problem+json.
    2. Contain exactly the six RFC 7807 fields: type, title, status, detail, instance, code.
    3. Return stable code ERR_STORAGE_FAILED and controlled message 'Storage operation failed.'.
    4. Never leak the sentinel or any sensitive path fragment into the serialized response.
    """
    class LeakyStorageAdapter(StubExecutionAdapter):
        def get_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
            raise StorageError(f"Internal storage error accessing {path_case}")

    app_instance = create_app(adapter=LeakyStorageAdapter())
    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")

    response = test_client.get("/api/v1/datasets/ds_audit_test")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"

    body = response.json()
    assert set(body.keys()) == {"type", "title", "status", "detail", "instance", "code"}
    assert body["status"] == 500
    assert body["code"] == "ERR_STORAGE_FAILED"
    assert body["detail"] == "Storage operation failed."
    assert body["instance"] == "/api/v1/datasets/ds_audit_test"

    response_text = response.text
    assert path_case not in response_text, f"Leaked path {path_case} in response"
    for sensitive in SENSITIVE_SUBSTRINGS:
        assert sensitive not in response_text, f"Leaked sensitive token {sensitive} in response: {response_text}"


@pytest.mark.parametrize("path_case", ALL_AUDIT_PATH_CASES)
def test_b1_02_http_exceptions_serialized_response_all_audit_paths(path_case: str) -> None:
    """
    Proves that HTTP exceptions with unverified internal details:
    1. Return HTTP 404 application/problem+json with stable code ERR_NOT_FOUND.
    2. Contain exactly the six RFC 7807 fields.
    3. Use canonical status title/detail rather than publishing arbitrary path details.
    4. Never leak the sentinel or any sensitive path fragment into the serialized response.
    """
    app_instance = create_app()

    @app_instance.get("/test-http-audit-leak")
    def leak_endpoint() -> None:
        exc = HTTPException(
            status_code=404,
            detail=f"Resource missing at {path_case}",
        )
        exc.code = "ERR_NOT_FOUND"
        raise exc

    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")
    response = test_client.get("/test-http-audit-leak")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"

    body = response.json()
    assert set(body.keys()) == {"type", "title", "status", "detail", "instance", "code"}
    assert body["status"] == 404
    assert body["code"] == "ERR_NOT_FOUND"
    assert body["title"] == "Not Found"
    assert body["detail"] == "Not Found"

    response_text = response.text
    assert path_case not in response_text, f"Leaked path {path_case} in response"
    for sensitive in SENSITIVE_SUBSTRINGS:
        assert sensitive not in response_text, f"Leaked sensitive token {sensitive} in response: {response_text}"


def test_b1_02_safe_http_exception_detail_published() -> None:
    """
    Proves that explicitly safe HTTP exceptions publish their controlled message.
    """
    app_instance = create_app()

    @app_instance.get("/test-safe-http-detail")
    def safe_endpoint() -> None:
        raise FileSizeLimitHTTPError(max_bytes=500, actual_bytes=600)

    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")
    response = test_client.get("/test-safe-http-detail")

    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_FILE_SIZE_LIMIT_EXCEEDED"
    assert body["detail"] == "Dataset size (600 bytes) exceeds configured limit (500 bytes)."
    assert set(body.keys()) == {"type", "title", "status", "detail", "instance", "code"}


def test_b1_02_validation_error_does_not_leak_raw_sensitive_input() -> None:
    """Proves validation errors do not echo raw sensitive input objects or paths."""
    app_instance = create_app()
    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")

    sentinel_secret = "C:\\Users\\O'Brien\\AUDIT_PRIVATE_SENTINEL\\file.csv"
    response = test_client.post(
        "/api/v1/datasets/upload",
        data={"file": sentinel_secret},
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "ERR_REQUEST_VALIDATION"
    assert set(body.keys()) == {"type", "title", "status", "detail", "instance", "code"}

    response_text = response.text
    for sensitive in ("AUDIT_PRIVATE_SENTINEL", "O'Brien", "C:\\"):
        assert sensitive not in response_text, f"Leaked sensitive token {sensitive} in response: {response_text}"


def test_b1_02_unhandled_and_chained_exceptions_generic_500() -> None:
    """Proves unhandled and chained exceptions return generic 500 without leaking cause, traces, or paths."""
    app_instance = create_app()

    @app_instance.get("/crash-chained")
    def crash_endpoint() -> None:
        try:
            raise FileNotFoundError("C:\\Users\\O'Brien\\AUDIT_PRIVATE_SENTINEL\\system\\config.sys not found")
        except FileNotFoundError as err:
            raise RuntimeError("Database error with /mnt/AUDIT_PRIVATE_SENTINEL/token.key") from err

    test_client = TestClient(app_instance, raise_server_exceptions=False, base_url="http://127.0.0.1")
    response = test_client.get("/crash-chained")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert set(body.keys()) == {"type", "title", "status", "detail", "instance", "code"}
    assert body["status"] == 500
    assert body["code"] == "ERR_INTERNAL_SERVER_ERROR"
    assert body["title"] == "Internal Server Error"
    assert body["detail"] == "An unexpected internal server error occurred."

    response_text = response.text
    for forbidden in (
        "AUDIT_PRIVATE_SENTINEL",
        "O'Brien",
        "config.sys",
        "token.key",
        "C:\\",
        "/mnt/",
        "FileNotFoundError",
        "RuntimeError",
        "Traceback",
    ):
        assert forbidden not in response_text, f"Leaked token {forbidden} in response: {response_text}"


# =====================================================================
# 9. B1-04: Decoupled Headless Imports
# =====================================================================

def test_b1_04_headless_adapters_do_not_import_delivery_framework() -> None:
    """
    Proves importing headless adapters and ports in a fresh Python process
    does not import FastAPI or Starlette.
    """
    code = (
        "import sys\n"
        "import backend.ports\n"
        "import backend.adapters.local_storage\n"
        "import backend.adapters.local_execution\n"
        "assert 'fastapi' not in sys.modules, 'fastapi was imported!'\n"
        "assert 'starlette' not in sys.modules, 'starlette was imported!'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Import verification failed: {result.stderr}"


def test_b1_04_headless_imports_succeed_when_fastapi_blocked() -> None:
    """
    Proves headless modules import and execute correctly even when FastAPI is blocked in sys.modules.
    """
    code = (
        "import sys\n"
        "sys.modules['fastapi'] = None\n"
        "sys.modules['starlette'] = None\n"
        "import backend.ports\n"
        "import backend.adapters.local_storage\n"
        "import backend.adapters.local_execution\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Headless execution with blocked delivery framework failed: {result.stderr}"


# =====================================================================
# 10. Regression gates for the independently reproduced acceptance blockers
# =====================================================================

_AUDIT_SECRET = "AUDIT_PRIVATE_SENTINEL"


def _assert_controlled_problem(response, status, title, detail, code):
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert set(response.json()) == {"type", "title", "status", "detail", "instance", "code"}
    assert response.json() == {
        "type": "about:blank", "status": status, "title": title,
        "detail": detail, "instance": "/api/v1/datasets/ds_probe", "code": code,
    }
    assert _AUDIT_SECRET not in response.text


class _DeceptiveDetail(str):
    def __hash__(self):
        return hash("Not Found")

    def __eq__(self, other):
        return other == "Not Found"


class _DiagnosticSizeError(FileSizeLimitHTTPError):
    def __init__(self):
        super().__init__(max_bytes=500, actual_bytes=600)
        self.message = _AUDIT_SECRET
        self.detail = _AUDIT_SECRET
        self.code = _AUDIT_SECRET


@pytest.mark.parametrize("case", [
    "safe_flag", "http_code", "storage_code", "parquet", "encoding",
    "mutated_size", "subclass_size", "deceptive_string", "validation",
    "invalid_size_fields", "wrapped_storage", "wrapped_size",
])
def test_b1_02_controlled_messages_ignore_exception_text_and_attributes(case):
    path = "/data/" + _AUDIT_SECRET + "/private.csv"
    status, title, detail, code = 404, "Not Found", "Not Found", "ERR_NOT_FOUND"
    if case in ("safe_flag", "http_code", "deceptive_string"):
        exc = HTTPException(404, detail=_DeceptiveDetail(path) if case == "deceptive_string" else path)
        exc.is_safe_public = True if case == "safe_flag" else False
        exc.code = _AUDIT_SECRET
    elif case in ("storage_code", "wrapped_storage"):
        exc = StorageError(path, code=_AUDIT_SECRET)
        status, title, detail, code = 500, "Storage Operation Failed", "Storage operation failed.", "ERR_STORAGE_FAILED"
        if case == "wrapped_storage":
            outer = HTTPException(400, detail=path)
            outer.__cause__ = exc
            exc = outer
    elif case == "parquet":
        exc = ParquetConversionError(path)
        status, title, detail, code = 422, "Parquet Conversion Failed", "Dataset values could not be represented safely in Parquet.", "ERR_PARQUET_CONVERSION"
    elif case == "encoding":
        exc = core_errors.EncodingDetectionError(path)
        status, title, detail, code = 422, "Encoding Detection Failed", "The dataset encoding could not be determined or decoded.", "ERR_ENCODING_DETECTION"
    elif case == "validation":
        exc = RequestValidationError([{
            "loc": ("body", _AUDIT_SECRET), "type": _AUDIT_SECRET,
            "msg": path, "input": _AUDIT_SECRET, "ctx": {"error": ValueError(path)},
        }])
        status, title, detail, code = 422, "Request Validation Error", "Request validation failed.", "ERR_REQUEST_VALIDATION"
    else:
        exc = _DiagnosticSizeError() if case == "subclass_size" else FileSizeLimitHTTPError(500, 600)
        exc.message = path
        exc.detail = path
        exc.code = _AUDIT_SECRET
        status, title, detail, code = 413, "Payload Too Large", "Dataset size (600 bytes) exceeds configured limit (500 bytes).", "ERR_FILE_SIZE_LIMIT_EXCEEDED"
        if case == "invalid_size_fields":
            exc.actual_bytes = _DeceptiveDetail(_AUDIT_SECRET)
            exc.max_bytes = True
            detail = "Dataset size exceeds the configured limit."
        if case == "wrapped_size":
            outer = HTTPException(400, detail=path)
            outer.__cause__ = exc
            exc = outer

    class FaultAdapter(StubExecutionAdapter):
        def get_dataset_metadata(self, dataset_id):
            raise exc

    with TestClient(create_app(adapter=FaultAdapter()), raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/datasets/ds_probe")
    _assert_controlled_problem(response, status, title, detail, code)


@pytest.mark.parametrize("status", [0, 700, True, "404", _AUDIT_SECRET, None, 200, 399, 600, 404.0])
def test_b1_02_invalid_http_status_is_safe_internal_error(status):
    class FaultAdapter(StubExecutionAdapter):
        def get_dataset_metadata(self, dataset_id):
            exc = HTTPException(status, detail=_AUDIT_SECRET)
            exc.code = _AUDIT_SECRET
            exc.is_safe_public = True
            raise exc

    with TestClient(create_app(adapter=FaultAdapter()), raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/datasets/ds_probe")
    _assert_controlled_problem(
        response, 500, "Internal Server Error",
        "An unexpected internal server error occurred.", "ERR_INTERNAL_SERVER_ERROR",
    )


@pytest.mark.parametrize("exc,status,title,detail,code", [
    (core_errors.EmptyDatasetError(), 400, "Empty Dataset", "The provided dataset is empty.", "ERR_EMPTY_DATASET"),
    (storage_errors.InvalidDatasetIdError(_AUDIT_SECRET), 400, "Invalid Dataset ID", "Dataset identifier must be a non-empty string without path traversal or reserved characters.", "ERR_INVALID_DATASET_ID"),
    (storage_errors.DatasetNotFoundError(_AUDIT_SECRET), 404, "Dataset Not Found", "The requested dataset was not found.", "ERR_DATASET_NOT_FOUND"),
    (storage_errors.DatasetAlreadyExistsError(_AUDIT_SECRET), 409, "Dataset Already Exists", "A dataset with this identifier already exists in storage.", "ERR_DATASET_ALREADY_EXISTS"),
    (core_errors.ProcessingTimeoutError(31, 30), 504, "Processing Timeout", "Data ingestion exceeded the configured processing time limit.", "ERR_PROCESSING_TIMEOUT"),
    (core_errors.MalformedCSVError(message=_AUDIT_SECRET), 422, "Malformed CSV", "The dataset contains malformed CSV.", "ERR_MALFORMED_CSV"),
    (core_errors.ParserFieldSizeLimitError(2, message=_AUDIT_SECRET), 422, "Field Size Exceeded", "A field exceeds the CSV parser size limit.", "ERR_PARSER_FIELD_SIZE_LIMIT"),
    (core_errors.CellSizeLimitExceededError(2, message=_AUDIT_SECRET), 422, "Cell Size Exceeded", "A cell exceeds the configured size limit.", "ERR_CELL_SIZE_LIMIT_EXCEEDED"),
    (core_errors.RowCountLimitExceededError(11, 10), 422, "Row Count Exceeded", "Dataset row count exceeds the configured limit.", "ERR_ROW_COUNT_LIMIT_EXCEEDED"),
    (core_errors.ColumnCountLimitExceededError(11, 10), 422, "Column Count Exceeded", "Dataset column count exceeds the configured limit.", "ERR_COLUMN_COUNT_LIMIT_EXCEEDED"),
    (core_errors.DelimiterDetectionError(_AUDIT_SECRET), 422, "Delimiter Detection Failed", "The dataset delimiter could not be determined.", "ERR_DELIMITER_DETECTION"),
    (core_errors.HeaderValidationError(_AUDIT_SECRET), 422, "Header Validation Failed", "The dataset headers are invalid.", "ERR_HEADER_VALIDATION"),
    (core_errors.NumericRangeError(_AUDIT_SECRET), 422, "Numeric Range Error", "A numeric calculation exceeded the supported range.", "ERR_NUMERIC_RANGE"),
    (storage_errors.DatasetCorruptError(_AUDIT_SECRET, _AUDIT_SECRET), 500, "Dataset Corrupted", "Dataset storage artifacts are corrupted or unreadable.", "ERR_DATASET_CORRUPT"),
    (core_errors.SourceReadError(_AUDIT_SECRET), 500, "Source Read Error", "Failed to read data source.", "ERR_SOURCE_READ"),
    (core_errors.IngestionError(_AUDIT_SECRET), 422, "Ingestion Failed", "Data ingestion failed.", "ERR_INGESTION_FAILED"),
])
def test_b1_02_every_domain_category_ignores_mutated_diagnostics(exc, status, title, detail, code):
    exc.code = _AUDIT_SECRET
    exc.message = _AUDIT_SECRET
    exc.is_safe_public = True

    class FaultAdapter(StubExecutionAdapter):
        def get_dataset_metadata(self, dataset_id):
            raise exc

    with TestClient(create_app(adapter=FaultAdapter()), raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/datasets/ds_probe")
    _assert_controlled_problem(response, status, title, detail, code)


@pytest.mark.parametrize("value,error", [
    (True, TypeError), (False, TypeError), (500.9, TypeError), (500.0, TypeError),
    ("500", TypeError), (float("nan"), TypeError), (float("inf"), TypeError),
    (0, ValueError), (-1, ValueError), (MAX_CONFIGURED_UPLOAD_BYTES + 1, ValueError),
])
def test_b1_03_settings_reject_invalid_caps_without_coercion(value, error):
    with pytest.raises(error):
        Settings(max_upload_bytes=value)


@pytest.mark.parametrize("source", ["settings", "adapter_alias", "adapter_limits"])
@pytest.mark.parametrize("value", [True, 500.9, "500", 0, -1, MAX_CONFIGURED_UPLOAD_BYTES + 1])
def test_b1_03_factory_validates_every_present_cap(source, value):
    settings = Settings(max_upload_bytes=500)
    adapter = StubExecutionAdapter(max_upload_bytes=1000)
    if source == "settings":
        settings.max_upload_bytes = value
    elif source == "adapter_alias":
        adapter.max_upload_bytes = value
    else:
        adapter.limits = SimpleNamespace(max_bytes=value)
    with pytest.raises((TypeError, ValueError)):
        create_app(settings=settings, adapter=adapter)
    if source == "settings":
        with pytest.raises((TypeError, ValueError)):
            create_app(settings=settings)


@pytest.mark.parametrize("value", ["", "0", "-1", "500.9", "True", " 500", "+500", "NaN", "inf", "５００", str(MAX_CONFIGURED_UPLOAD_BYTES + 1)])
def test_b1_03_environment_rejects_invalid_caps(monkeypatch, value):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", value)
    with pytest.raises(ValueError):
        create_app()
    with pytest.raises(ValueError):
        create_app(adapter=StubExecutionAdapter())


def test_b1_03_missing_and_boundary_configuration(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    assert Settings().max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES
    assert resolve_effective_upload_budget(None, None) == DEFAULT_MAX_UPLOAD_BYTES
    for cap in (1, MAX_CONFIGURED_UPLOAD_BYTES):
        for adapter in (None, SimpleNamespace()):
            app_instance = create_app(settings=Settings(max_upload_bytes=cap), adapter=adapter)
            assert resolve_effective_upload_budget(app_instance.state.settings, app_instance.state.adapter) == cap
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(MAX_CONFIGURED_UPLOAD_BYTES))
    assert Settings().max_upload_bytes == MAX_CONFIGURED_UPLOAD_BYTES


def _audit_multipart_parts():
    boundary = "AuditBoundary"
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="f.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode()
    return boundary, header, f"\r\n--{boundary}--\r\n".encode()


async def _audit_asgi_upload(application, chunks, *, content_length=None, cancel_after=None):
    boundary, _, _ = _audit_multipart_parts()
    received, sent = [], []
    async def receive():
        if cancel_after is not None and len(received) == cancel_after:
            raise asyncio.CancelledError()
        if len(received) == len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[len(received)]
        received.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": len(received) < len(chunks)}
    async def send(message):
        sent.append(message)
    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", f"multipart/form-data; boundary={boundary}".encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/datasets/upload",
        "raw_path": b"/api/v1/datasets/upload", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
    }
    cancelled = False
    try:
        await application(scope, receive, send)
    except asyncio.CancelledError:
        cancelled = True
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) <= 1
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return received, starts, json.loads(body) if body else None, cancelled


@pytest.mark.parametrize("content_length", [None, 1, "invalid", 133])
def test_b1_03_conflicting_caps_reject_exact_133_byte_request(tmp_path, monkeypatch, content_length):
    adapter = LocalExecutionAdapter(storage_dir=tmp_path / "s", limits=IngestionLimits(max_bytes=100))
    adapter.max_upload_bytes = 1000
    application = create_app(settings=Settings(max_upload_bytes=500), adapter=adapter)
    assert resolve_effective_upload_budget(application.state.settings, adapter) == 100
    _, header, end = _audit_multipart_parts()
    body = header + b"v\n1\n2\n" + end
    assert len(body) == 133
    forwarded, ingested = [], []
    original_write = sfp.multipart.MultipartParser.write
    def write(parser, data):
        forwarded.append(data)
        return original_write(parser, data)
    def ingest(*args, **kwargs):
        ingested.append(True)
        pytest.fail("Over-budget intake reached ingestion")
    monkeypatch.setattr(sfp.multipart.MultipartParser, "write", write)
    monkeypatch.setattr(adapter, "ingest_and_store", ingest)
    received, starts, problem, cancelled = asyncio.run(
        _audit_asgi_upload(application, [body], content_length=content_length)
    )
    assert not cancelled
    assert len(received) == (0 if content_length == 133 else 1)
    assert not forwarded and not ingested
    assert len(starts) == 1 and starts[0]["status"] == 413
    assert dict(starts[0]["headers"])[b"content-type"] == b"application/problem+json"
    assert problem["code"] == "ERR_FILE_SIZE_LIMIT_EXCEEDED"
    assert "100 bytes" in problem["detail"]
    assert not list(tmp_path.rglob("metadata.json"))


@pytest.mark.parametrize("mode", ["rejection", "cancellation", "malformed"])
def test_b1_03_real_disk_spool_deleted_after_writes(tmp_path, monkeypatch, mode):
    _, header, end = _audit_multipart_parts()
    cap = 1536 * 1024 + 500 if mode == "rejection" else 4 * 1024 * 1024
    adapter = LocalExecutionAdapter(storage_dir=tmp_path / "s")
    application = create_app(settings=Settings(max_upload_bytes=cap), adapter=adapter)
    spools, forwarded, ingested = [], [], []
    original_spool = sfp.SpooledTemporaryFile
    original_write = sfp.multipart.MultipartParser.write

    def track_spool(*args, **kwargs):
        kwargs["dir"] = str(tmp_path)
        spool = original_spool(*args, **kwargs)
        record = {"file": spool, "bytes": 0, "name": None, "rolled": False}
        spools.append(record)
        write, close = spool.write, spool.close
        def track_write(data):
            count = write(data)
            record["bytes"] += count
            return count
        def track_close():
            if not spool.closed:
                record["rolled"] = spool._rolled
                record["name"] = spool.name
            close()
        spool.write, spool.close = track_write, track_close
        return spool

    def track_parser(parser, data):
        forwarded.append(data)
        return original_write(parser, data)

    def ingest(*args, **kwargs):
        ingested.append(True)
        pytest.fail("Failed intake reached ingestion")

    monkeypatch.setattr(sfp, "SpooledTemporaryFile", track_spool)
    monkeypatch.setattr(sfp.multipart.MultipartParser, "write", track_parser)
    monkeypatch.setattr(adapter, "ingest_and_store", ingest)
    chunks = [header] + [b"A" * (256 * 1024)] * 10 + [end]
    if mode == "malformed":
        chunks = chunks[:7] + [b"\r\n--AuditBoundary\r\nBad Header\r\n\r\n"]
    received, starts, problem, cancelled = asyncio.run(
        _audit_asgi_upload(application, chunks, cancel_after=7 if mode == "cancellation" else None)
    )
    assert len(spools) == 1
    spool = spools[0]
    assert spool["bytes"] == 1536 * 1024
    assert spool["rolled"] and spool["file"].closed
    if isinstance(spool["name"], str):
        assert not Path(spool["name"]).exists()
    else:
        # POSIX anonymous temporary files have a descriptor instead of a pathname.
        with pytest.raises(OSError):
            os.fstat(spool["name"])
    assert not ingested and not list(tmp_path.rglob("metadata.json"))
    if mode == "cancellation":
        assert cancelled and not starts
        assert len(received) == 7
    else:
        assert not cancelled and len(starts) == 1
        assert starts[0]["status"] == (413 if mode == "rejection" else 400)
        assert dict(starts[0]["headers"])[b"content-type"] == b"application/problem+json"
        assert problem["code"] == ("ERR_FILE_SIZE_LIMIT_EXCEEDED" if mode == "rejection" else "ERR_HTTP_ERROR")
    if mode == "rejection":
        assert len(received) == 8 < len(chunks)
        assert sum(map(len, forwarded)) <= cap
        assert len(forwarded) == len(received) - 1


def test_b1_03_per_request_resolution_and_exact_budget(tmp_path):
    _, header, end = _audit_multipart_parts()
    body = header + b"v\n1\n2\n" + end
    app_instance = create_app(storage_dir=tmp_path / "s", settings=Settings(max_upload_bytes=500))
    app_instance.state.settings.max_upload_bytes = 132
    _, starts, problem, _ = asyncio.run(_audit_asgi_upload(app_instance, [body]))
    assert starts[0]["status"] == 413
    assert "132 bytes" in problem["detail"]
    app_instance.state.settings.max_upload_bytes = 133
    _, starts, metadata, _ = asyncio.run(_audit_asgi_upload(app_instance, [body]))
    assert starts[0]["status"] == 201
    assert (tmp_path / "s" / "datasets" / metadata["dataset_id"] / "data.parquet").exists()


def test_b1_03_r1_runtime_mutation_to_none_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    B1-03-R1 regression:
    Construct app with Settings cap 500 and injected adapter cap 1000.
    1. Confirm a 772-byte multipart request is rejected while cap is 500 (HTTP 413).
    2. Mutate settings.max_upload_bytes = None.
    3. Submit the same 772-byte multipart request.
    4. Assert request does not return 201 (returns 500).
    5. Assert parser receives no body content after invalid required cap is detected.
    6. Assert ingestion is not invoked.
    7. Assert no dataset is published.
    8. Assert response follows existing safe problem-response contract.
    9. Confirm invalid setting is rejected consistently rather than falling back to 1000-byte cap.
    """
    storage_dir = tmp_path / "storage"
    settings = Settings(max_upload_bytes=500)
    adapter = LocalExecutionAdapter(storage_dir=storage_dir, limits=IngestionLimits(max_bytes=1000))
    app_instance = create_app(storage_dir=storage_dir, settings=settings, adapter=adapter)

    # Instrument parser and adapter
    forwarded, ingested = [], []
    original_write = sfp.multipart.MultipartParser.write

    def write(parser, data):
        forwarded.append(data)
        return original_write(parser, data)

    def ingest(*args, **kwargs):
        ingested.append(True)
        pytest.fail("Ingestion should not be invoked on rejected request")

    monkeypatch.setattr(sfp.multipart.MultipartParser, "write", write)
    monkeypatch.setattr(adapter, "ingest_and_store", ingest)

    # Construct exact 772-byte multipart payload
    boundary, header, end = _audit_multipart_parts()
    pad_len = 772 - len(header) - len(end)
    body = header + b"v\n" + b"x" * (pad_len - 2) + end
    assert len(body) == 772

    # Step 1: Confirm request is rejected while cap is 500
    received1, starts1, problem1, _ = asyncio.run(_audit_asgi_upload(app_instance, [body]))
    assert starts1[0]["status"] == 413
    assert problem1["code"] == "ERR_FILE_SIZE_LIMIT_EXCEEDED"
    assert not (storage_dir / "datasets").exists() or list((storage_dir / "datasets").iterdir()) == []

    # Step 2: Mutate settings.max_upload_bytes = None
    forwarded.clear()
    ingested.clear()
    settings.max_upload_bytes = None

    # Step 3: Submit the same 772-byte request
    received2, starts2, problem2, cancelled2 = asyncio.run(_audit_asgi_upload(app_instance, [body]))

    # Step 4: Assert the request does not return 201 (returns 500)
    assert starts2[0]["status"] != 201
    assert starts2[0]["status"] == 500
    assert not cancelled2

    # Step 5: Assert parser receives no body content
    assert len(received2) == 0
    assert len(forwarded) == 0

    # Step 6: Assert ingestion is not invoked
    assert len(ingested) == 0

    # Step 7: Assert no dataset is published
    assert not (storage_dir / "datasets").exists() or list((storage_dir / "datasets").iterdir()) == []

    # Step 8: Assert response is safe and follows problem-response contract
    headers_dict = dict(starts2[0]["headers"])
    assert headers_dict[b"content-type"] == b"application/problem+json"
    assert problem2["type"] == "about:blank"
    assert problem2["title"] == "Internal Server Error"
    assert problem2["status"] == 500
    assert problem2["detail"] == "An unexpected internal server error occurred."
    assert problem2["instance"] == "/api/v1/datasets/upload"
    assert problem2["code"] == "ERR_INTERNAL_SERVER_ERROR"

    # Step 9: Confirm reconstructing app raises TypeError consistently
    with pytest.raises(TypeError):
        create_app(settings=settings, adapter=adapter)
    with pytest.raises(TypeError):
        resolve_effective_upload_budget(settings, adapter)


@pytest.mark.parametrize("invalid_val,expected_error", [
    (True, TypeError),
    (500.9, TypeError),
    ("500", TypeError),
    (0, ValueError),
    (MAX_CONFIGURED_UPLOAD_BYTES + 1, ValueError),
])
def test_b1_03_r1_runtime_mutation_to_invalid_types_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_val: object, expected_error: type
) -> None:
    """
    B1-03-R1 regression:
    Test runtime mutations to True, 500.9, '500', 0, and > sys.maxsize.
    Confirm none can bypass required-cap validation and all fail closed safely.
    """
    storage_dir = tmp_path / "storage"
    settings = Settings(max_upload_bytes=500)
    adapter = LocalExecutionAdapter(storage_dir=storage_dir, limits=IngestionLimits(max_bytes=1000))
    app_instance = create_app(storage_dir=storage_dir, settings=settings, adapter=adapter)

    forwarded, ingested = [], []
    monkeypatch.setattr(sfp.multipart.MultipartParser, "write", lambda p, d: forwarded.append(d))
    monkeypatch.setattr(adapter, "ingest_and_store", lambda *a, **k: ingested.append(True))

    boundary, header, end = _audit_multipart_parts()
    pad_len = 772 - len(header) - len(end)
    body = header + b"v\n" + b"x" * (pad_len - 2) + end

    # Mutate to invalid value
    settings.max_upload_bytes = invalid_val

    # Verify direct resolution fails
    with pytest.raises(expected_error):
        resolve_effective_upload_budget(settings, adapter)

    # Verify request fails closed safely over ASGI
    received, starts, problem, _ = asyncio.run(_audit_asgi_upload(app_instance, [body]))
    assert starts[0]["status"] != 201
    assert starts[0]["status"] == 500
    assert len(received) == 0
    assert len(forwarded) == 0
    assert len(ingested) == 0
    assert not (storage_dir / "datasets").exists() or list((storage_dir / "datasets").iterdir()) == []
    assert problem["code"] == "ERR_INTERNAL_SERVER_ERROR"
    assert problem["detail"] == "An unexpected internal server error occurred."

