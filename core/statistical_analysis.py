"""Count-only, deterministic analysis of already typed columns.

Results have schema_version, analysis, method_version, status, total_count,
null_count, non_null_count, analysis-specific counts, and one finding. The finding
contains code, method_version, and evidence: available aggregate counts only.
EMPTY and ALL_NULL describe missing data; INSUFFICIENT_SAMPLE, CONSTANT and
ZERO_IQR explain why screening was not applied; SCREENED and COUNTED identify
completed analyses. INVALID_INPUT, UNSUPPORTED_NUMERIC_RANGE and
CATEGORY_LIMIT_EXCEEDED expose no partial counts. No observations, labels,
quartiles, fences, paths, or input-derived messages are returned.

Float ratios represent the binary value supplied, not the original decimal text.
Limits do not guarantee a process memory ceiling. Deadline checks are cooperative;
they cannot interrupt Python's sort while it is executing.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Any, Optional

from .types import ExecutionDeadline

_MAX_ENTRIES = 1_000_000
_MAX_CATEGORIES = 10_000
_MAX_CATEGORY_BYTES = 32 * 1024
_MAX_DISTINCT_BYTES = 1024 * 1024
_POLL_INTERVAL = 500
_NUMERIC_METHOD = "tukey-linear-exact-v1"
_CATEGORY_METHOD = "category-counts-v1"


def _check(deadline: Optional[ExecutionDeadline]) -> None:
    if deadline is not None:
        deadline.check()


def _validate_container(values: Any, deadline: Optional[ExecutionDeadline]) -> None:
    _check(deadline)
    if type(values) not in (list, tuple):
        _check(deadline)
        raise TypeError("Values must be a list or tuple.")
    if len(values) > _MAX_ENTRIES:
        _check(deadline)
        raise ValueError("Column exceeds the entry limit.")


@dataclass(frozen=True)
class NumericScreenResult:
    """Numeric status and counts; method describes the fixed screening policy.

    Outlier counts are present only for SCREENED. Common counts are absent for
    INVALID_INPUT and UNSUPPORTED_NUMERIC_RANGE. to_dict returns fresh objects.
    """
    status: str
    total_count: Optional[int]
    null_count: Optional[int]
    non_null_count: Optional[int]
    below_fence_count: Optional[int] = None
    above_fence_count: Optional[int] = None
    potential_outlier_count: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        counts = {
            "total_count": self.total_count,
            "null_count": self.null_count,
            "non_null_count": self.non_null_count,
        }
        outliers = {
            "below_fence_count": self.below_fence_count,
            "above_fence_count": self.above_fence_count,
            "potential_outlier_count": self.potential_outlier_count,
        }
        evidence = {} if self.total_count is None else dict(counts)
        if self.status == "SCREENED":
            evidence.update(outliers)
        return {
            "schema_version": "1.0",
            "analysis": "numeric_outliers",
            "method_version": _NUMERIC_METHOD,
            "status": self.status,
            **counts,
            **outliers,
            "method": {
                "quartiles": "linear interpolation at p*(n-1)",
                "multiplier": "3/2",
                "fence_rule": "strictly outside",
                "minimum_non_null_count": 4,
            },
            "findings": [{"code": self.status, "method_version": _NUMERIC_METHOD, "evidence": evidence}],
        }


@dataclass(frozen=True)
class CategorySummaryResult:
    """Category status and aggregate counts; no labels or frequency map.

    Distinct and singleton counts are present only for COUNTED; all common
    counts are absent on INVALID_INPUT or CATEGORY_LIMIT_EXCEEDED.
    """
    status: str
    total_count: Optional[int]
    null_count: Optional[int]
    non_null_count: Optional[int]
    distinct_count: Optional[int] = None
    singleton_category_count: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        counts = {
            "total_count": self.total_count,
            "null_count": self.null_count,
            "non_null_count": self.non_null_count,
        }
        summary = {
            "distinct_count": self.distinct_count,
            "singleton_category_count": self.singleton_category_count,
        }
        evidence = {} if self.total_count is None else dict(counts)
        if self.status == "COUNTED":
            evidence.update(summary)
        return {
            "schema_version": "1.0",
            "analysis": "categorical_counts",
            "method_version": _CATEGORY_METHOD,
            "status": self.status,
            **counts,
            **summary,
            "findings": [{"code": self.status, "method_version": _CATEGORY_METHOD, "evidence": evidence}],
        }


def screen_numeric(values: list[Any] | tuple[Any, ...], *, deadline: Optional[ExecutionDeadline] = None) -> NumericScreenResult:
    """Screen finite typed values with exact Tukey 1.5-IQR fences, returning counts only."""
    _validate_container(values, deadline)
    numbers: list[int | float] = []
    null_count = 0
    for index, value in enumerate(values):
        if index % _POLL_INTERVAL == 0:
            _check(deadline)
        if value is None:
            null_count += 1
        elif type(value) is int:
            if not -(2**63) <= value <= 2**63 - 1:
                _check(deadline)
                return NumericScreenResult("UNSUPPORTED_NUMERIC_RANGE", None, None, None)
            numbers.append(value)
        elif type(value) is float and math.isfinite(value):
            numbers.append(value)
        else:
            _check(deadline)
            return NumericScreenResult("INVALID_INPUT", None, None, None)

    total, n = len(values), len(numbers)
    counts = (total, null_count, n)
    if not total:
        status = "EMPTY"
    elif not n:
        status = "ALL_NULL"
    elif n < 4:
        status = "INSUFFICIENT_SAMPLE"
    else:
        _check(deadline)
        numbers.sort()
        _check(deadline)
        if numbers[0] == numbers[-1]:
            status = "CONSTANT"
        else:
            def quartile(numerator: int) -> Fraction:
                lower, remainder = divmod(numerator * (n - 1), 4)
                first = Fraction(numbers[lower])
                if not remainder:
                    return first
                return (first * (4 - remainder) + Fraction(numbers[lower + 1]) * remainder) / 4

            q1, q3 = quartile(1), quartile(3)
            iqr = q3 - q1
            if not iqr:
                status = "ZERO_IQR"
            else:
                lower, upper = q1 - Fraction(3, 2) * iqr, q3 + Fraction(3, 2) * iqr
                below = above = 0
                for index, value in enumerate(numbers):
                    if index % _POLL_INTERVAL == 0:
                        _check(deadline)
                    exact = Fraction(value)
                    below += exact < lower
                    above += exact > upper
                _check(deadline)
                return NumericScreenResult("SCREENED", *counts, below, above, below + above)
    _check(deadline)
    return NumericScreenResult(status, *counts)


def summarize_categories(values: list[Any] | tuple[Any, ...], *, deadline: Optional[ExecutionDeadline] = None) -> CategorySummaryResult:
    """Count homogeneous literal string or boolean categories without returning labels."""
    _validate_container(values, deadline)
    counts: dict[str | bool, int] = {}
    category_type = None
    null_count = distinct_bytes = 0
    for index, value in enumerate(values):
        if index % _POLL_INTERVAL == 0:
            _check(deadline)
        if value is None:
            null_count += 1
            continue
        kind = type(value)
        if kind not in (str, bool) or (category_type is not None and kind is not category_type):
            _check(deadline)
            return CategorySummaryResult("INVALID_INPUT", None, None, None)
        category_type = kind
        # Equal existing keys have already passed UTF-8 and size validation.
        if value in counts:
            counts[value] += 1
            continue
        size = 0
        if kind is str:
            if len(value) > _MAX_CATEGORY_BYTES:
                _check(deadline)
                return CategorySummaryResult("INVALID_INPUT", None, None, None)
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError:
                size = None
            if size is None:
                _check(deadline)
                return CategorySummaryResult("INVALID_INPUT", None, None, None)
            if size > _MAX_CATEGORY_BYTES:
                _check(deadline)
                return CategorySummaryResult("INVALID_INPUT", None, None, None)
        if len(counts) == _MAX_CATEGORIES or distinct_bytes + size > _MAX_DISTINCT_BYTES:
            _check(deadline)
            return CategorySummaryResult("CATEGORY_LIMIT_EXCEEDED", None, None, None)
        distinct_bytes += size
        counts[value] = 1
    total = len(values)
    if not total:
        status = "EMPTY"
    elif total == null_count:
        status = "ALL_NULL"
    else:
        _check(deadline)
        singletons = 0
        for index, count in enumerate(counts.values()):
            if index % _POLL_INTERVAL == 0:
                _check(deadline)
            singletons += count == 1
        _check(deadline)
        return CategorySummaryResult("COUNTED", total, null_count, total - null_count, len(counts), singletons)
    _check(deadline)
    return CategorySummaryResult(status, total, null_count, total - null_count)
