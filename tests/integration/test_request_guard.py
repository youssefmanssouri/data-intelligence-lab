"""
The backend must refuse requests that a web page could make to the loopback port:
DNS-rebound Host names, cross-site browser requests and foreign origins
(backend/request_guard.py). The frontend proxy's own shape must still pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.request_guard import is_allowed_request, is_loopback_authority

CSV = b"a,b\n1,2\n3,4\n"


def h(**pairs: str) -> list[tuple[bytes, bytes]]:
    return [(k.replace("_", "-").encode(), v.encode()) for k, v in pairs.items()]


@pytest.mark.parametrize("value", ["127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:65535", "[::1]", "[::1]:3000"])
def test_loopback_authorities_accepted(value: str) -> None:
    assert is_loopback_authority(value)


@pytest.mark.parametrize("value", [
    "evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil.example:8000",
    "127.0.0.2:8000", "0.0.0.0:8000", "[::2]", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:080",
    "127.0.0.1:80:80", "user@127.0.0.1", "", "127.0.0.1 :80",
])
def test_non_loopback_or_malformed_authorities_refused(value: str) -> None:
    assert not is_loopback_authority(value)


def test_request_rules() -> None:
    assert is_allowed_request(h(host="127.0.0.1:8000"))  # CLI / server-side client
    assert is_allowed_request(h(host="127.0.0.1:8000", sec_fetch_site="same-origin", origin="http://127.0.0.1:3000"))
    assert is_allowed_request(h(host="localhost:8000", sec_fetch_site="none"))
    assert not is_allowed_request([])  # no Host
    assert not is_allowed_request(h(host="127.0.0.1:8000") + h(host="evil.example"))  # duplicate Host
    for site in ("cross-site", "same-site", ""):
        assert not is_allowed_request(h(host="127.0.0.1:8000", sec_fetch_site=site))
    for origin in ("null", "https://127.0.0.1:3000", "http://evil.example", "http://127.0.0.1:3000/x",
                   "http://user@127.0.0.1:3000"):
        assert not is_allowed_request(h(host="127.0.0.1:8000", origin=origin))
    assert not is_allowed_request(h(host="127.0.0.1:8000", origin="http://127.0.0.1:1") + h(origin="http://127.0.0.1:2"))


@pytest.fixture
def app_dir(tmp_path: Path):
    return create_app(storage_dir=tmp_path / "storage"), tmp_path / "storage"


def _datasets(storage: Path) -> list[str]:
    d = storage / "datasets"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_dns_rebound_host_is_refused_before_routing(app_dir) -> None:
    app, _ = app_dir
    with TestClient(app, base_url="http://evil.example:8000", raise_server_exceptions=False) as client:
        for path in ("/health", "/ready", "/api/v1/datasets/ds_0123456789abcdef"):
            res = client.get(path)
            assert res.status_code == 403, path
            assert res.headers["content-type"].startswith("application/problem+json")
            assert res.json()["code"] == "ERR_FORBIDDEN_REQUEST"


def test_cross_site_upload_stores_nothing(app_dir) -> None:
    app, storage = app_dir
    with TestClient(app, base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
        res = client.post("/api/v1/datasets/upload", files={"file": ("x.csv", CSV, "text/csv")},
                          headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
        assert res.status_code == 403
        res = client.post("/api/v1/datasets/upload", files={"file": ("x.csv", CSV, "text/csv")},
                          headers={"Origin": "null"})
        assert res.status_code == 403
    assert _datasets(storage) == []


def test_proxy_and_cli_shapes_still_work(app_dir) -> None:
    app, storage = app_dir
    with TestClient(app, base_url="http://127.0.0.1:8000", raise_server_exceptions=False) as client:
        # What the frontend proxy forwards for the UI: backend Host, the browser's same-origin hints.
        res = client.post("/api/v1/datasets/upload", files={"file": ("x.csv", CSV, "text/csv")},
                          headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:3000"})
        assert res.status_code == 201, res.text
        dataset_id = res.json()["dataset_id"]
        # A command-line client: no browser headers at all.
        assert client.get(f"/api/v1/datasets/{dataset_id}").status_code == 200
        assert client.get("/health").status_code == 200
    assert _datasets(storage) == [dataset_id]
