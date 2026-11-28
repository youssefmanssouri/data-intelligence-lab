"""
Data Intelligence Lab - Local Execution Adapter
Orchestrates domain ingestion and local storage persistence conforming to IExecutionAdapter.
Pure Python adapter: does not import FastAPI, Starlette, or HTTP types.
"""

from __future__ import annotations

from collections import OrderedDict
import io
from pathlib import Path
import threading
from typing import Any, BinaryIO, Dict, Optional, Union
import uuid

import pyarrow as pa

from backend.adapters.local_storage import (
    LocalDatasetStorage,
    validate_dataset_id,
)
from backend.ports import IExecutionAdapter
from core import ingest_csv
from core.insights import MAX_COLUMNS_SCANNED, generate_insights, sample_indices
from core.types import IngestionLimits, InferencePolicy


class LocalExecutionAdapter:
    """
    Local workstation execution adapter.
    Coordinates pure-Python core CSV ingestion and local filesystem Parquet/JSON storage.
    """

    def __init__(
        self,
        storage_dir: Optional[Union[str, Path]] = None,
        storage: Optional[LocalDatasetStorage] = None,
        limits: Optional[IngestionLimits] = None,
    ) -> None:
        if storage is not None:
            self.storage = storage
        else:
            self.storage = LocalDatasetStorage(storage_dir=storage_dir)
        self.limits = limits or IngestionLimits()
        # Insights are derived from immutable datasets, so a small per-process cache is safe;
        # entries are dropped when a dataset is deleted.
        self._insights_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._insights_lock = threading.Lock()

    def generate_dataset_id(self) -> str:
        """Generates a collision-resistant dataset identifier conforming to storage rules."""
        return f"ds_{uuid.uuid4().hex[:16]}"

    def ingest_and_store(
        self,
        file_source: Union[BinaryIO, bytes],
        filename: Optional[str] = None,
        dataset_id: Optional[str] = None,
        limits: Optional[IngestionLimits] = None,
        policy: Optional[InferencePolicy] = None,
    ) -> Dict[str, Any]:
        """
        Orchestrates ingestion of CSV data through core.ingest_csv() and atomic persistence
        to LocalDatasetStorage.
        """
        # Validate or generate dataset ID using authoritative storage validator
        if dataset_id is not None:
            valid_id = validate_dataset_id(dataset_id)
        else:
            valid_id = self.generate_dataset_id()

        # Ingest CSV via pure domain core
        resolved_limits = limits if limits is not None else self.limits
        ingestion_result = ingest_csv(
            file_source,
            limits=resolved_limits,
            inference_policy=policy,
        )

        # Persist normalized Parquet and metadata artifacts via frozen 4A storage
        metadata = self.storage.save_dataset(
            dataset_id=valid_id,
            ingestion_result=ingestion_result,
            filename=filename,
        )

        return metadata

    def get_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """Loads and returns dataset metadata dictionary."""
        valid_id = validate_dataset_id(dataset_id)
        return self.storage.load_metadata(valid_id)

    def get_dataset_insights(self, dataset_id: str) -> Dict[str, Any]:
        """
        Computes deterministic, plain-language insights for a stored dataset.

        Read from data.parquet on demand and never written to metadata.json, which must not
        contain cell values (insights may quote a category label). Works for datasets
        saved by any earlier version.
        """
        valid_id = validate_dataset_id(dataset_id)
        with self._insights_lock:
            cached = self._insights_cache.get(valid_id)
            if cached is not None:
                self._insights_cache.move_to_end(valid_id)
                return cached

        metadata = self.storage.load_metadata(valid_id)
        table = self.storage.read_parquet_table(valid_id)
        columns = metadata.get("columns") or []
        row_count = table.num_rows
        indices = sample_indices(row_count)
        if len(indices) < row_count:
            table = table.take(pa.array(indices, type=pa.int64()))
        present = set(table.column_names)
        scanned = [c for c in columns if c.get("name") in present][:MAX_COLUMNS_SCANNED]
        values = {c["name"]: table.column(c["name"]).to_pylist() for c in scanned}
        quality = metadata.get("quality") or {}
        result = generate_insights(
            columns,
            values,
            row_count=int(metadata.get("row_count") or row_count),
            duplicate_rows=int(quality.get("duplicate_row_count") or 0),
            rows_examined=len(indices),
        )
        result["dataset_id"] = valid_id

        with self._insights_lock:
            self._insights_cache[valid_id] = result
            self._insights_cache.move_to_end(valid_id)
            while len(self._insights_cache) > 16:
                self._insights_cache.popitem(last=False)
        return result

    def delete_dataset(self, dataset_id: str) -> bool:
        """Deletes a dataset from storage."""
        valid_id = validate_dataset_id(dataset_id)
        with self._insights_lock:
            self._insights_cache.pop(valid_id, None)
        return self.storage.delete_dataset(valid_id)

    def check_storage_readiness(self) -> bool:
        """Verifies that the datasets storage directory exists and is writable."""
        try:
            self.storage.datasets_dir.mkdir(parents=True, exist_ok=True)
            test_probe = self.storage.datasets_dir / f".readiness_probe_{uuid.uuid4().hex}"
            test_probe.touch()
            test_probe.unlink(missing_ok=True)
            return True
        except Exception:
            return False
