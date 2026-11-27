"""
Data Intelligence Lab - Dataset Management Routes
Handles CSV dataset upload, metadata retrieval, and dataset deletion.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from backend.adapters.exceptions import DatasetNotFoundError
from backend.dependencies import get_execution_adapter
from backend.ports import IExecutionAdapter

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    file: UploadFile = File(...),
    adapter: IExecutionAdapter = Depends(get_execution_adapter),
) -> Dict[str, Any]:
    """
    Uploads and ingests a CSV dataset.
    Streams the uploaded binary file directly into the execution adapter without
    eagerly buffering the entire file into memory via await file.read().
    Returns 201 Created with a summary of the ingested dataset metadata.
    """
    try:
        # Pass the underlying binary stream (SpooledTemporaryFile) directly
        metadata = adapter.ingest_and_store(
            file_source=file.file,
            filename=file.filename,
        )
        # Return summary payload conforming to API specifications
        return {
            "dataset_id": metadata["dataset_id"],
            "filename": metadata.get("filename"),
            "created_at": metadata.get("created_at"),
            "row_count": metadata.get("row_count"),
            "column_count": metadata.get("column_count"),
            "data_quality_score": metadata.get("data_quality_score"),
        }
    finally:
        await file.close()


@router.get("/{dataset_id}", status_code=status.HTTP_200_OK)
def get_dataset(
    dataset_id: str,
    adapter: IExecutionAdapter = Depends(get_execution_adapter),
) -> Dict[str, Any]:
    """
    Retrieves stored metadata and statistical profile for a dataset identifier.
    Returns 200 OK with the full metadata dictionary.
    """
    return adapter.get_dataset_metadata(dataset_id)


@router.get("/{dataset_id}/insights", status_code=status.HTTP_200_OK)
def get_dataset_insights(
    dataset_id: str,
    adapter: IExecutionAdapter = Depends(get_execution_adapter),
) -> Dict[str, Any]:
    """
    Returns rule-based, deterministic insights for a dataset, written in plain language.
    Computed locally from the stored Parquet data; nothing leaves the machine.
    """
    return adapter.get_dataset_insights(dataset_id)


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dataset(
    dataset_id: str,
    adapter: IExecutionAdapter = Depends(get_execution_adapter),
) -> Response:
    """
    Deletes a dataset and its persistent artifacts from storage.
    Returns 204 No Content on success, or 404 Not Found if the dataset does not exist.
    """
    deleted = adapter.delete_dataset(dataset_id)
    if not deleted:
        raise DatasetNotFoundError(dataset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
