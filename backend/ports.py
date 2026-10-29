"""
Data Intelligence Lab - Execution Adapter Port
Defines the architectural interface (Protocol) for execution adapters in the Hexagonal Architecture.
Pure Python boundary: does NOT import FastAPI, Starlette, HTTP status codes, or request/response types.
"""

from __future__ import annotations

from typing import Any, BinaryIO, Dict, Optional, Protocol, Union

from core.types import IngestionLimits, InferencePolicy


class IExecutionAdapter(Protocol):
    """
    Architectural port mediating between delivery mechanisms and the underlying
    analytical engine and storage subsystem.
    """

    def ingest_and_store(
        self,
        file_source: Union[BinaryIO, bytes],
        filename: Optional[str] = None,
        dataset_id: Optional[str] = None,
        limits: Optional[IngestionLimits] = None,
        policy: Optional[InferencePolicy] = None,
    ) -> Dict[str, Any]:
        """
        Ingests a CSV data stream or bytes through the domain engine and persists
        the normalized Parquet and metadata artifacts to storage.

        Args:
            file_source: Binary stream or raw bytes of the CSV file.
            filename: Optional source filename (sanitized by storage adapter).
            dataset_id: Optional specific dataset identifier (generated if omitted).
            limits: Optional ingestion resource limits.
            policy: Optional inference policy.

        Returns:
            The serialized metadata dictionary written to storage.

        Raises:
            IngestionError: If CSV ingestion, validation, or resource limits fail.
            StorageError: If persistence, Parquet conversion, or storage checks fail.
        """
        ...

    def get_dataset_metadata(self, dataset_id: str) -> Dict[str, Any]:
        """
        Retrieves stored metadata for a dataset identifier.

        Args:
            dataset_id: Validated dataset identifier.

        Returns:
            Metadata dictionary loaded from storage.

        Raises:
            InvalidDatasetIdError: If dataset_id fails validation.
            DatasetNotFoundError: If the dataset does not exist.
            DatasetCorruptError: If metadata artifact is corrupted.
            StorageError: If disk access fails.
        """
        ...

    def delete_dataset(self, dataset_id: str) -> bool:
        """
        Deletes a dataset and its persistent artifacts.

        Args:
            dataset_id: Validated dataset identifier.

        Returns:
            True if deleted, False if dataset was not found.

        Raises:
            InvalidDatasetIdError: If dataset_id fails validation.
            StorageError: If deletion encounters an OS error.
        """
        ...

    def check_storage_readiness(self) -> bool:
        """
        Verifies that the underlying storage subsystem is operational and writable.

        Returns:
            True if storage is ready, False otherwise.
        """
        ...
