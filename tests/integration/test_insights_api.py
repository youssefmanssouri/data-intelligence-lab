"""Integration tests for GET /api/v1/datasets/{id}/insights."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    path = tmp_path / "storage"
    path.mkdir()
    return path


@pytest.fixture
def client(storage_dir: Path) -> TestClient:
    return TestClient(create_app(storage_dir=storage_dir), raise_server_exceptions=False, base_url="http://127.0.0.1")


def sales_csv() -> bytes:
    lines = ["order_id,region,amount,note"]
    for i in range(40):
        region = "North" if i % 2 else "South"
        amount = 5000 if i == 39 else (90 if region == "North" else 30) + i % 4
        note = "" if i % 3 else "CONFIDENTIAL_NOTE_7731"
        lines.append(f"{i + 1},{region},{amount},{note}")
    return ("\n".join(lines) + "\n").encode()


def upload(client: TestClient, body: bytes) -> str:
    res = client.post("/api/v1/datasets/upload", files={"file": ("sales.csv", io.BytesIO(body), "text/csv")})
    assert res.status_code == 201, res.text
    return res.json()["dataset_id"]


def test_insights_endpoint_returns_deterministic_plain_language_findings(client: TestClient) -> None:
    dataset_id = upload(client, sales_csv())
    first = client.get(f"/api/v1/datasets/{dataset_id}/insights")
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("application/json")
    body = first.json()
    assert body["dataset_id"] == dataset_id
    assert body["method_version"] == "1.0"
    assert body["rows_examined"] == 40 and body["sampled"] is False and body["partial"] is False
    found = {item["code"] for item in body["items"]}
    assert {"IDENTIFIER_COLUMN", "MISSING_VALUES", "OUTLIERS", "GROUP_DIFFERENCE"} <= found
    for item in body["items"]:
        assert set(item) == {"code", "severity", "title", "detail", "columns", "evidence"}
        assert item["title"] and item["detail"]

    # A fresh process (no cache) produces byte-identical output.
    assert client.get(f"/api/v1/datasets/{dataset_id}/insights").json() == body


def test_insights_are_identical_across_app_instances(storage_dir: Path) -> None:
    a = TestClient(create_app(storage_dir=storage_dir), base_url="http://127.0.0.1")
    dataset_id = upload(a, sales_csv())
    first = a.get(f"/api/v1/datasets/{dataset_id}/insights").json()
    b = TestClient(create_app(storage_dir=storage_dir), base_url="http://127.0.0.1")
    assert b.get(f"/api/v1/datasets/{dataset_id}/insights").json() == first


def test_insights_are_not_persisted_to_metadata(client: TestClient, storage_dir: Path) -> None:
    dataset_id = upload(client, sales_csv())
    assert client.get(f"/api/v1/datasets/{dataset_id}/insights").status_code == 200
    meta_text = (storage_dir / "datasets" / dataset_id / "metadata.json").read_text(encoding="utf-8")
    assert "insights" not in json.loads(meta_text)
    assert "CONFIDENTIAL_NOTE_7731" not in meta_text
    assert sorted(p.name for p in (storage_dir / "datasets" / dataset_id).iterdir()) == ["data.parquet", "metadata.json"]


def test_insights_missing_and_invalid_ids(client: TestClient) -> None:
    missing = client.get("/api/v1/datasets/ds_nonexistent_999/insights")
    assert missing.status_code == 404
    assert missing.json()["code"] == "ERR_DATASET_NOT_FOUND"
    invalid = client.get("/api/v1/datasets/bad..id/insights")
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "ERR_INVALID_DATASET_ID"


def test_insights_cache_is_dropped_on_delete(client: TestClient) -> None:
    dataset_id = upload(client, sales_csv())
    assert client.get(f"/api/v1/datasets/{dataset_id}/insights").status_code == 200
    assert client.delete(f"/api/v1/datasets/{dataset_id}").status_code == 204
    assert client.get(f"/api/v1/datasets/{dataset_id}/insights").status_code == 404


def test_large_dataset_is_sampled(client: TestClient) -> None:
    rows = ["k,v"] + [f"{i % 7},{i}" for i in range(25_000)]
    dataset_id = upload(client, ("\n".join(rows) + "\n").encode())
    body = client.get(f"/api/v1/datasets/{dataset_id}/insights").json()
    assert body["sampled"] is True
    assert body["rows_examined"] == 20_000
