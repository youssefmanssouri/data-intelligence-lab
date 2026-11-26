"""Controlled RFC 7807 responses for the HTTP delivery boundary.

Exception messages, codes, validation diagnostics, and safety flags are never
public response data. Diagnostics are confined to the configured local logger.
"""

from __future__ import annotations

import http
import logging
import sys
from typing import NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.adapters.exceptions import (
    DatasetAlreadyExistsError, DatasetCorruptError, DatasetNotFoundError,
    InvalidDatasetIdError, ParquetConversionError, StorageError,
)
from core.types import (
    CellSizeLimitExceededError, ColumnCountLimitExceededError,
    DelimiterDetectionError, EmptyDatasetError, EncodingDetectionError,
    FileSizeLimitExceededError, HeaderValidationError, IngestionError,
    MalformedCSVError, NumericRangeError, ParserFieldSizeLimitError,
    ProcessingTimeoutError, RowCountLimitExceededError, SourceReadError,
)

logger = logging.getLogger("backend.errors")
PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


class _Problem(NamedTuple):
    status: int
    title: str
    detail: str
    code: str


_INTERNAL = _Problem(
    500, "Internal Server Error", "An unexpected internal server error occurred.",
    "ERR_INTERNAL_SERVER_ERROR",
)
_VALIDATION = _Problem(
    422, "Request Validation Error", "Request validation failed.",
    "ERR_REQUEST_VALIDATION",
)
_READINESS = _Problem(
    503, "Service Unavailable", "Storage subsystem is not ready or writable.",
    "ERR_STORAGE_NOT_READY",
)
_FORBIDDEN_REQUEST = _Problem(
    403, "Forbidden", "Only local, same-origin requests are accepted.",
    "ERR_FORBIDDEN_REQUEST",
)

# Categories are application-owned types; their mutable attributes supply no text.
_DOMAIN_PROBLEMS = {
    FileSizeLimitExceededError: _Problem(413, "Payload Too Large", "Dataset size exceeds the configured limit.", "ERR_FILE_SIZE_LIMIT_EXCEEDED"),
    EmptyDatasetError: _Problem(400, "Empty Dataset", "The provided dataset is empty.", "ERR_EMPTY_DATASET"),
    InvalidDatasetIdError: _Problem(400, "Invalid Dataset ID", "Dataset identifier must be a non-empty string without path traversal or reserved characters.", "ERR_INVALID_DATASET_ID"),
    DatasetNotFoundError: _Problem(404, "Dataset Not Found", "The requested dataset was not found.", "ERR_DATASET_NOT_FOUND"),
    DatasetAlreadyExistsError: _Problem(409, "Dataset Already Exists", "A dataset with this identifier already exists in storage.", "ERR_DATASET_ALREADY_EXISTS"),
    ProcessingTimeoutError: _Problem(504, "Processing Timeout", "Data ingestion exceeded the configured processing time limit.", "ERR_PROCESSING_TIMEOUT"),
    MalformedCSVError: _Problem(422, "Malformed CSV", "The dataset contains malformed CSV.", "ERR_MALFORMED_CSV"),
    ParserFieldSizeLimitError: _Problem(422, "Field Size Exceeded", "A field exceeds the CSV parser size limit.", "ERR_PARSER_FIELD_SIZE_LIMIT"),
    CellSizeLimitExceededError: _Problem(422, "Cell Size Exceeded", "A cell exceeds the configured size limit.", "ERR_CELL_SIZE_LIMIT_EXCEEDED"),
    RowCountLimitExceededError: _Problem(422, "Row Count Exceeded", "Dataset row count exceeds the configured limit.", "ERR_ROW_COUNT_LIMIT_EXCEEDED"),
    ColumnCountLimitExceededError: _Problem(422, "Column Count Exceeded", "Dataset column count exceeds the configured limit.", "ERR_COLUMN_COUNT_LIMIT_EXCEEDED"),
    EncodingDetectionError: _Problem(422, "Encoding Detection Failed", "The dataset encoding could not be determined or decoded.", "ERR_ENCODING_DETECTION"),
    DelimiterDetectionError: _Problem(422, "Delimiter Detection Failed", "The dataset delimiter could not be determined.", "ERR_DELIMITER_DETECTION"),
    HeaderValidationError: _Problem(422, "Header Validation Failed", "The dataset headers are invalid.", "ERR_HEADER_VALIDATION"),
    NumericRangeError: _Problem(422, "Numeric Range Error", "A numeric calculation exceeded the supported range.", "ERR_NUMERIC_RANGE"),
    ParquetConversionError: _Problem(422, "Parquet Conversion Failed", "Dataset values could not be represented safely in Parquet.", "ERR_PARQUET_CONVERSION"),
    DatasetCorruptError: _Problem(500, "Dataset Corrupted", "Dataset storage artifacts are corrupted or unreadable.", "ERR_DATASET_CORRUPT"),
    SourceReadError: _Problem(500, "Source Read Error", "Failed to read data source.", "ERR_SOURCE_READ"),
    StorageError: _Problem(500, "Storage Operation Failed", "Storage operation failed.", "ERR_STORAGE_FAILED"),
    IngestionError: _Problem(422, "Ingestion Failed", "Data ingestion failed.", "ERR_INGESTION_FAILED"),
}
_CODE_PROBLEMS = {
    problem.code: problem
    for problem in (*_DOMAIN_PROBLEMS.values(), _INTERNAL, _VALIDATION, _READINESS, _FORBIDDEN_REQUEST)
}
_HTTP_CODES = {404: "ERR_NOT_FOUND"}


class FileSizeLimitHTTPError(StarletteHTTPException, FileSizeLimitExceededError):
    """Preserve receive-time size failures through FastAPI's multipart boundary."""

    def __init__(self, max_bytes: int, actual_bytes: int) -> None:
        StarletteHTTPException.__init__(self, status_code=413)
        FileSizeLimitExceededError.__init__(
            self, max_bytes=max_bytes, actual_bytes=actual_bytes,
        )


def _serialize_problem(problem: _Problem, instance: str) -> JSONResponse:
    # instance identifies the client's request, never an exception-supplied path.
    return JSONResponse(
        status_code=problem.status,
        content={
            "type": "about:blank",
            "title": problem.title,
            "status": problem.status,
            "detail": problem.detail,
            "instance": instance if type(instance) is str else "/",
            "code": problem.code,
        },
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def build_problem_response(
    status: int, title: str, detail: str, instance: str, code: str,
) -> JSONResponse:
    """Compatibility entry point for routes such as readiness.

    Select a fixed catalog entry. Caller-supplied title/detail are deliberately
    unused; equality or hashing on a string subclass cannot authorize content.
    """
    problem = _CODE_PROBLEMS.get(code, _INTERNAL) if type(code) is str else _INTERNAL
    if type(status) is not int or status != problem.status:
        problem = _INTERNAL
    return _serialize_problem(problem, instance)


def _log_exception(exc: Exception) -> None:
    # A diagnostic formatter or a hostile __str__ must not break the safe response.
    try:
        logger.error("Request processing failed", exc_info=(type(exc), exc, exc.__traceback__))
    except Exception:
        pass


def _domain_problem(exc: Exception) -> _Problem:
    category = next((cls for cls in type(exc).__mro__ if cls in _DOMAIN_PROBLEMS), None)
    problem = _DOMAIN_PROBLEMS.get(category, _INTERNAL)
    if category is FileSizeLimitExceededError:
        try:
            actual = getattr(exc, "actual_bytes", None)
            maximum = getattr(exc, "max_bytes", None)
        except Exception:
            return problem
        # Exact built-in types exclude bool, subclasses, and custom formatters.
        if (
            type(actual) is int and type(maximum) is int
            and 0 <= actual <= sys.maxsize and 1 <= maximum <= sys.maxsize
            and actual > maximum
        ):
            problem = problem._replace(
                detail=f"Dataset size ({actual:,} bytes) exceeds configured limit ({maximum:,} bytes)."
            )
    return problem


def register_error_handlers(app: FastAPI) -> None:
    """Register safe response construction for every supported error category."""

    async def domain_handler(request: Request, exc: Exception) -> JSONResponse:
        _log_exception(exc)
        return _serialize_problem(_domain_problem(exc), request.url.path)

    for category in _DOMAIN_PROBLEMS:
        app.add_exception_handler(category, domain_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        _log_exception(exc)
        # Do not inspect msg/input/ctx/loc/type: even custom validator diagnostics
        # and field locations may contain sensitive strings.
        return _serialize_problem(_VALIDATION, request.url.path)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        _log_exception(exc)
        status = exc.status_code
        if type(status) not in (int, http.HTTPStatus) or not 400 <= status <= 599:
            return _serialize_problem(_INTERNAL, request.url.path)

        # FastAPI may wrap a parsing failure. Only its category and validated
        # numeric counts may influence the public response, never cause text.
        if isinstance(exc, (IngestionError, StorageError)):
            return _serialize_problem(_domain_problem(exc), request.url.path)
        cause = exc.__cause__
        if isinstance(cause, (IngestionError, StorageError)):
            return _serialize_problem(_domain_problem(cause), request.url.path)

        try:
            phrase = http.HTTPStatus(status).phrase
        except ValueError:
            phrase = "HTTP Error"
        problem = _Problem(int(status), phrase, phrase, _HTTP_CODES.get(status, "ERR_HTTP_ERROR"))
        return _serialize_problem(problem, request.url.path)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        _log_exception(exc)
        return _serialize_problem(_INTERNAL, request.url.path)
