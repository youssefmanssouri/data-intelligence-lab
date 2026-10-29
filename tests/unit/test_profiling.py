"""
Unit Test Suite for Data Intelligence Lab - Phase 3 Profiling Arithmetic Guards
Validates NumericRangeError boundaries, sanitized messages, deadline precedence,
column index attribution, integer finiteness, and reference variance preservation.
"""

import math
import time
from typing import List
import pytest

from core.profiling import calculate_numeric_stats, profile_column
from core.types import ExecutionDeadline, NumericRangeError, ProcessingTimeoutError


# =====================================================================
# Reference Variance Preservation
# =====================================================================

def test_p3_a10_variance_reference_preserved() -> None:
    """Confirms reference case [1_000_000] + [0] * 10_000 produces variance 99990000.9999."""
    values = [1_000_000] + [0] * 10_000
    res = calculate_numeric_stats(values)
    assert res["variance"] == 99990000.9999
    assert res["mean"] == 99.99
    assert res["min"] == 0
    assert res["max"] == 1_000_000


def test_empty_numeric_stats_returns_empty_dict() -> None:
    """Empty list returns empty dict {} without raising errors."""
    assert calculate_numeric_stats([]) == {}


# =====================================================================
# P3-A10: Numeric Input Boundary Guards
# =====================================================================

def test_non_finite_float_input_raises_numeric_range_error() -> None:
    """Direct helper rejects non-finite float inputs (nan, inf, -inf) with operation='input'."""
    with pytest.raises(NumericRangeError) as exc_info:
        calculate_numeric_stats([1.0, float("nan"), 2.0])
    err = exc_info.value
    assert err.operation == "input"
    assert err.code == "ERR_NUMERIC_RANGE"
    assert "input" in err.message
    # Content sanitization: does not leak cell value
    assert "nan" not in err.message.lower()

    with pytest.raises(NumericRangeError) as exc_info2:
        calculate_numeric_stats([1.0, float("inf")])
    assert exc_info2.value.operation == "input"


def test_integers_treated_as_finite_without_float_conversion() -> None:
    """Arbitrary precision Python integers are finite and do not fail input validation."""
    large_int = 10**50
    res = calculate_numeric_stats([large_int, large_int])
    assert res["min"] == large_int
    assert res["max"] == large_int
    assert res["variance"] == 0.0


# =====================================================================
# P3-A10: Narrow Arithmetic Operation Guards
# =====================================================================

def test_mean_overflow_raises_numeric_range_error() -> None:
    """Float addition overflow during sum/mean raises NumericRangeError(operation='mean')."""
    # 1e308 + 1e308 overflows to infinity in float64
    with pytest.raises(NumericRangeError) as exc_info:
        calculate_numeric_stats([1e308, 1e308])
    assert exc_info.value.operation == "mean"
    assert exc_info.value.code == "ERR_NUMERIC_RANGE"


def test_variance_overflow_raises_numeric_range_error() -> None:
    """Square diff overflow during variance raises NumericRangeError(operation='variance')."""
    # (1e200 - (-1e200)) ** 2 = (2e200)**2 = 4e400 (overflows float64)
    with pytest.raises(NumericRangeError) as exc_info:
        calculate_numeric_stats([1e200, -1e200])
    assert exc_info.value.operation == "variance"
    assert exc_info.value.code == "ERR_NUMERIC_RANGE"


# =====================================================================
# Column Profiling Boundary & column_index Population
# =====================================================================

def test_profile_column_populates_column_index_and_sanitizes() -> None:
    """profile_column catches NumericRangeError and populates column_index."""
    col_idx = 4
    with pytest.raises(NumericRangeError) as exc_info:
        profile_column(
            index=col_idx,
            raw_name="overflow_col",
            normalized_name="overflow_col",
            inferred_type="float",
            typed_values=[1e200, -1e200],
            total_rows=2,
        )
    err = exc_info.value
    assert err.column_index == col_idx
    assert err.operation == "variance"
    assert f"column {col_idx}" in err.message
    # Assert public error message is exact and sanitized
    assert err.message == f"Numeric calculation exceeded representation limits in column {col_idx} during 'variance'."
    assert "1e200" not in err.message


# =====================================================================
# Deadline Expiry Precedence & Exception Propagation
# =====================================================================

def test_deadline_expiry_has_precedence_over_numeric_range_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If deadline is expired before raising range error, ProcessingTimeoutError propagates instead."""
    start_time = 1000.0
    timeout_seconds = 2.0
    simulated_time = start_time

    def mock_monotonic() -> float:
        # Monotonic time past deadline
        return start_time + timeout_seconds + 5.0

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    # Values that would otherwise cause variance overflow
    with pytest.raises(ProcessingTimeoutError):
        calculate_numeric_stats([1e200, -1e200], deadline=deadline)


def test_unrelated_programmer_exceptions_propagate_unchanged() -> None:
    """Unrelated exceptions (e.g., TypeError) propagate without being masked as NumericRangeError."""
    with pytest.raises(TypeError):
        # Passing invalid type inside values sequence
        calculate_numeric_stats([1.0, "not_a_number"])  # type: ignore[list-item]


# =====================================================================
# Phase 3 Targeted Remediation Tests (P3-V03, P3-V04)
# =====================================================================

def test_p3_v03_odd_length_median_overflow() -> None:
    """Odd-length median conversion float(sorted_vals[mid]) must raise sanitized NumericRangeError without chaining."""
    h = 10**400
    # Sum is zero: -2*h + h + h = 0. Execution reaches odd-length median float(sorted_vals[1]) -> OverflowError
    with pytest.raises(NumericRangeError) as exc_info:
        calculate_numeric_stats([-2 * h, h, h])
    err = exc_info.value
    assert err.code == "ERR_NUMERIC_RANGE"
    assert err.operation == "median"
    assert err.column_index is None
    # Requirement: absence of native exception chaining in sanitized public errors
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    assert str(h) not in err.message


def test_p3_v03_profile_column_odd_median_overflow() -> None:
    """profile_column catches median OverflowError, populates column_index, and suppresses exception chaining."""
    h = 10**400
    col_idx = 3
    with pytest.raises(NumericRangeError) as exc_info:
        profile_column(
            index=col_idx,
            raw_name="huge_ints",
            normalized_name="huge_ints",
            inferred_type="integer",
            typed_values=[-2 * h, h, h],
            total_rows=3,
        )
    err = exc_info.value
    assert err.code == "ERR_NUMERIC_RANGE"
    assert err.column_index == col_idx
    assert err.operation == "median"
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    assert f"column {col_idx}" in err.message
    assert str(h) not in err.message


def test_p3_v03_timeout_precedence_before_median_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout occurring before median calculation raises ProcessingTimeoutError, not NumericRangeError."""
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    check_count = 0

    def mock_monotonic() -> float:
        nonlocal check_count, simulated_time
        check_count += 1
        # Advance time past deadline after sort and mean
        if check_count >= 4:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    h = 10**400
    with pytest.raises(ProcessingTimeoutError):
        calculate_numeric_stats([-2 * h, h, h], deadline=deadline)


def test_p3_v03_sqrt_value_error_translation_and_no_chaining(monkeypatch: pytest.MonkeyPatch) -> None:
    """Square-root ValueError translation must produce sanitized NumericRangeError without chaining."""
    def mock_sqrt(val: float) -> float:
        raise ValueError("math domain error")

    monkeypatch.setattr(math, "sqrt", mock_sqrt)
    with pytest.raises(NumericRangeError) as exc_info:
        calculate_numeric_stats([10, 20])
    err = exc_info.value
    assert err.code == "ERR_NUMERIC_RANGE"
    assert err.operation == "std_dev"
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


def test_p3_v04_deadline_expires_during_numeric_input_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deadline expiring during numeric input validation loop must raise ProcessingTimeoutError."""
    start_time = 1000.0
    timeout_seconds = 5.0
    simulated_time = start_time
    check_count = 0

    def mock_monotonic() -> float:
        nonlocal check_count, simulated_time
        check_count += 1
        if check_count >= 10:
            simulated_time = start_time + timeout_seconds + 1.0
        return simulated_time

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    deadline = ExecutionDeadline(timeout_seconds=timeout_seconds, start_time=start_time)

    # 2,000 numbers with poll_interval=100
    values = [1.5] * 2000
    with pytest.raises(ProcessingTimeoutError):
        calculate_numeric_stats(values, deadline=deadline, poll_interval=100)
