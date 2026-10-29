"""
Data Intelligence Lab - Data Profiling and Quality Scoring
Calculates descriptive statistics, completeness, uniqueness, and data health scores.
"""

from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple
from .types import ColumnMetadata, ColumnProfile, DataQualityReport, DatasetProfile, ExecutionDeadline, NumericRangeError


PROFILING_DEADLINE_POLL_INTERVAL: int = 500


def calculate_numeric_stats(
    values: List[float | int],
    deadline: Optional[ExecutionDeadline] = None,
    poll_interval: int = PROFILING_DEADLINE_POLL_INTERVAL,
) -> Dict[str, Any]:
    """Calculates five-number summary and dispersion metrics for numeric data with narrow arithmetic guards."""
    if not values:
        return {}

    def _raise_range_error(operation: str) -> None:
        if deadline is not None:
            deadline.check()
        raise NumericRangeError(operation=operation) from None

    # Input boundary guard: reject non-finite float inputs while treating ints as finite
    for idx, v in enumerate(values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        if isinstance(v, float) and not math.isfinite(v):
            _raise_range_error("input")

    if deadline is not None:
        deadline.check()

    n = len(values)
    # Check deadline immediately before and after indivisible sort operation
    sorted_vals = sorted(values)
    if deadline is not None:
        deadline.check()

    min_val = sorted_vals[0]
    max_val = sorted_vals[-1]

    # Mean calculation with overflow and finiteness guards
    try:
        sum_val = sum(values)
    except OverflowError:
        _raise_range_error("mean")
    if isinstance(sum_val, float) and not math.isfinite(sum_val):
        _raise_range_error("mean")

    try:
        mean_val = sum_val / n
    except OverflowError:
        _raise_range_error("mean")
    if isinstance(mean_val, float) and not math.isfinite(mean_val):
        _raise_range_error("mean")

    # Median (50th percentile)
    mid = n // 2
    if n % 2 == 1:
        try:
            median_val = float(sorted_vals[mid])
        except OverflowError:
            _raise_range_error("median")
    else:
        try:
            median_val = (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
        except OverflowError:
            _raise_range_error("median")
    if isinstance(median_val, float) and not math.isfinite(median_val):
        _raise_range_error("median")

    # Quartiles (25th and 75th percentiles using standard linear interpolation)
    def percentile(p: float, op_name: str = "quartile") -> float:
        idx = p * (n - 1)
        lower = int(math.floor(idx))
        upper = int(math.ceil(idx))
        weight = idx - lower
        try:
            val = sorted_vals[lower] * (1.0 - weight) + sorted_vals[upper] * weight
        except OverflowError:
            _raise_range_error(op_name)
        if isinstance(val, float) and not math.isfinite(val):
            _raise_range_error(op_name)
        return val

    q25 = percentile(0.25, "quartile")
    q75 = percentile(0.75, "quartile")
    try:
        iqr = q75 - q25
    except OverflowError:
        _raise_range_error("iqr")
    if isinstance(iqr, float) and not math.isfinite(iqr):
        _raise_range_error("iqr")

    if deadline is not None:
        deadline.check()

    # Sample variance and standard deviation with periodic deadline polling
    if n > 1:
        def polled_sq_diff():
            for idx, x in enumerate(values):
                if idx % poll_interval == 0 and deadline is not None:
                    deadline.check()
                diff = x - mean_val
                try:
                    sq = diff ** 2
                except OverflowError:
                    _raise_range_error("variance")
                if isinstance(sq, float) and not math.isfinite(sq):
                    _raise_range_error("variance")
                yield sq

        try:
            sum_sq_diff = sum(polled_sq_diff())
        except OverflowError:
            _raise_range_error("variance")
        if isinstance(sum_sq_diff, float) and not math.isfinite(sum_sq_diff):
            _raise_range_error("variance")

        variance = sum_sq_diff / (n - 1)
        if isinstance(variance, float) and not math.isfinite(variance):
            _raise_range_error("variance")

        try:
            std_dev = math.sqrt(variance)
        except (OverflowError, ValueError):
            _raise_range_error("std_dev")
        if isinstance(std_dev, float) and not math.isfinite(std_dev):
            _raise_range_error("std_dev")
    else:
        variance = 0.0
        std_dev = 0.0

    if deadline is not None:
        deadline.check()

    return {
        "min": min_val,
        "max": max_val,
        "mean": round(mean_val, 4),
        "median": round(median_val, 4),
        "q25": round(q25, 4),
        "q75": round(q75, 4),
        "iqr": round(iqr, 4),
        "std_dev": round(std_dev, 4),
        "variance": round(variance, 4),
    }


def calculate_string_stats(
    values: List[str],
    deadline: Optional[ExecutionDeadline] = None,
    poll_interval: int = PROFILING_DEADLINE_POLL_INTERVAL,
) -> Dict[str, Any]:
    """Calculates text length and distribution metrics for string data with periodic deadline polling."""
    if not values:
        return {}

    if deadline is not None:
        deadline.check()

    lengths: List[int] = []
    empty_string_count = 0
    total_len = 0
    for idx, v in enumerate(values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        l = len(v)
        lengths.append(l)
        total_len += l
        if l == 0:
            empty_string_count += 1

    if deadline is not None:
        deadline.check()

    return {
        "min_length": min(lengths),
        "max_length": max(lengths),
        "avg_length": round(total_len / len(lengths), 2),
        "empty_string_count": empty_string_count,
    }


def profile_column(
    index: int,
    raw_name: str,
    normalized_name: str,
    inferred_type: str,
    typed_values: List[Any],
    total_rows: int,
    deadline: Optional[ExecutionDeadline] = None,
    poll_interval: int = PROFILING_DEADLINE_POLL_INTERVAL,
) -> ColumnProfile:
    """Profiles a single column, generating structural and descriptive statistics."""
    if deadline is not None:
        deadline.check()

    non_null_vals: List[Any] = []
    for idx, v in enumerate(typed_values):
        if idx % poll_interval == 0 and deadline is not None:
            deadline.check()
        if v is not None:
            non_null_vals.append(v)

    null_count = total_rows - len(non_null_vals)
    null_ratio = round(null_count / total_rows, 4) if total_rows > 0 else 0.0

    if deadline is not None:
        deadline.check()

    # Distinct values and uniqueness
    distinct_set = set(non_null_vals)
    distinct_count = len(distinct_set)
    is_unique = (distinct_count == total_rows) and (total_rows > 0) and (null_count == 0)

    # Sample values (up to 5 distinct non-null values without full list allocation)
    sample_values: List[Any] = []
    for val in distinct_set:
        sample_values.append(val)
        if len(sample_values) == 5:
            break

    if deadline is not None:
        deadline.check()

    stats: Dict[str, Any] = {}
    if inferred_type in ("integer", "float") and non_null_vals:
        try:
            stats = calculate_numeric_stats(non_null_vals, deadline=deadline, poll_interval=poll_interval)
        except NumericRangeError as e:
            if deadline is not None:
                deadline.check()
            if e.column_index is None:
                raise NumericRangeError(operation=e.operation, column_index=index) from None
            raise
    elif inferred_type == "string" and non_null_vals:
        stats = calculate_string_stats(non_null_vals, deadline=deadline, poll_interval=poll_interval)
    elif inferred_type == "boolean" and non_null_vals:
        true_cnt = 0
        false_cnt = 0
        for idx, v in enumerate(non_null_vals):
            if idx % poll_interval == 0 and deadline is not None:
                deadline.check()
            if v is True:
                true_cnt += 1
            elif v is False:
                false_cnt += 1
        stats = {"true_count": true_cnt, "false_count": false_cnt}
    elif inferred_type in ("date", "datetime") and non_null_vals:
        if deadline is not None:
            deadline.check()
        stats = {
            "min_date": str(min(non_null_vals)),
            "max_date": str(max(non_null_vals)),
        }

    if deadline is not None:
        deadline.check()

    return ColumnProfile(
        index=index,
        name=normalized_name,
        raw_name=raw_name,
        inferred_type=inferred_type,
        total_count=total_rows,
        null_count=null_count,
        null_ratio=null_ratio,
        distinct_count=distinct_count,
        is_unique=is_unique,
        sample_values=sample_values,
        stats=stats,
    )


def compute_dataset_quality(
    row_count: int,
    column_count: int,
    columns: List[ColumnProfile],
    duplicate_rows: int,
) -> DataQualityReport:
    """
    Computes a deterministic 0–100 Data Quality Health Score.
    
    Formula:
    - Completeness (60% weight): Ratio of populated cells to total cells.
    - Uniqueness (30% weight): Ratio of distinct rows to total rows.
    - Validity / Uniformity (10% weight): Penalty for single-value / constant columns.
    """
    total_cells = row_count * column_count
    if total_cells == 0:
        return DataQualityReport(
            overall_score=100.0,
            completeness_score=100.0,
            uniqueness_score=100.0,
            validity_score=100.0,
            total_cells=0,
            total_missing_cells=0,
            duplicate_row_count=0,
            warnings=["Dataset has 0 rows of data."],
        )

    total_missing = sum(col.null_count for col in columns)
    completeness_score = max(0.0, 100.0 * (1.0 - total_missing / total_cells))

    uniqueness_score = 100.0
    if row_count > 1:
        uniqueness_score = max(0.0, 100.0 * (1.0 - duplicate_rows / row_count))

    warnings: List[str] = []

    # Flag column-level anomalies
    constant_columns = 0
    for col in columns:
        if col.null_ratio >= 0.50:
            warnings.append(f"Column '{col.name}' has high missingness ({col.null_ratio * 100:.1f}% missing).")
        if col.distinct_count == 1 and col.total_count > 1:
            constant_columns += 1
            warnings.append(f"Column '{col.name}' is constant (contains only 1 distinct value).")

    if duplicate_rows > 0:
        warnings.append(f"Dataset contains {duplicate_rows} duplicate rows ({duplicate_rows / row_count * 100:.1f}%).")

    # Validity score penalty for constant columns
    validity_penalty = (constant_columns / column_count) * 100.0 if column_count > 0 else 0.0
    validity_score = max(0.0, 100.0 - validity_penalty)

    # Weighted overall score
    overall_score = round(
        (0.60 * completeness_score) + (0.30 * uniqueness_score) + (0.10 * validity_score),
        1
    )

    return DataQualityReport(
        overall_score=overall_score,
        completeness_score=round(completeness_score, 1),
        uniqueness_score=round(uniqueness_score, 1),
        validity_score=round(validity_score, 1),
        total_cells=total_cells,
        total_missing_cells=total_missing,
        duplicate_row_count=duplicate_rows,
        warnings=warnings,
    )


def profile_dataset(
    row_count: int,
    column_metadata: List[ColumnMetadata],
    columns_data: Dict[str, Tuple[str, List[Any], int]],
    raw_row_hashes: Optional[Sequence[int]] = None,
    encoding: str = "utf-8",
    delimiter: str = ",",
    duplicate_rows: Optional[int] = None,
    deadline: Optional[ExecutionDeadline] = None,
) -> DatasetProfile:
    """Profiles the complete dataset, aggregating column profiles and overall quality."""
    column_profiles: List[ColumnProfile] = []
    
    for meta in column_metadata:
        if deadline is not None:
            deadline.check()
        inferred_type, typed_values, _ = columns_data[meta.normalized_name]
        prof = profile_column(
            index=meta.index,
            raw_name=meta.raw_name,
            normalized_name=meta.normalized_name,
            inferred_type=inferred_type,
            typed_values=typed_values,
            total_rows=row_count,
            deadline=deadline,
        )
        column_profiles.append(prof)

    if deadline is not None:
        deadline.check()

    # Determine duplicate rows (either pre-calculated or via raw_row_hashes)
    if duplicate_rows is None:
        distinct_rows_count = len(set(raw_row_hashes)) if raw_row_hashes else 0
        duplicate_rows = row_count - distinct_rows_count if row_count > 0 else 0

    quality = compute_dataset_quality(
        row_count=row_count,
        column_count=len(column_metadata),
        columns=column_profiles,
        duplicate_rows=duplicate_rows,
    )

    warnings: List[str] = list(quality.warnings)
    if len(column_metadata) == 1:
        warnings.append("Dataset contains only a single column; verify delimiter was detected correctly.")

    return DatasetProfile(
        row_count=row_count,
        column_count=len(column_metadata),
        encoding=encoding,
        delimiter=delimiter,
        columns=column_profiles,
        quality=quality,
        warnings=warnings,
    )

