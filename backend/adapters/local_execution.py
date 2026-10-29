"""
Data Intelligence Lab - Local Execution Adapter
Orchestrates domain ingestion and local storage persistence conforming to IExecutionAdapter.
Pure Python adapter: does not import FastAPI, Starlette, or HTTP types.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional, Union
import uuid

from backend.adapters.local_storage import (
    LocalDatasetStorage,
    validate_dataset_id,
)
from backend.ports import IExecutionAdapter
from core import ingest_csv
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

    def delete_dataset(self, dataset_id: str) -> bool:
        """Deletes a dataset from storage."""
        valid_id = validate_dataset_id(dataset_id)
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
