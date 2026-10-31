"""Contract, arithmetic, privacy, and cooperative-deadline tests for v1."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
import itertools
import json
import math
import sys

import pytest

from core.statistical_analysis import screen_numeric, summarize_categories
from core.types import ExecutionDeadline, ProcessingTimeoutError


def reference(values):
    """Independent exact-rank reference using rational ranks and weights."""
    ordered = sorted(Fraction(v) for v in values if v is not None)
    def quantile(p):
        rank = p * (len(ordered) - 1)
        i = rank.numerator // rank.denominator
        weight = rank - i
        return ordered[i] if not weight else ordered[i] + weight * (ordered[i + 1] - ordered[i])
    q1, q3 = quantile(Fraction(1, 4)), quantile(Fraction(3, 4))
    low = q1 - (q3 - q1) * Fraction(3, 2)
    high = q3 + (q3 - q1) * Fraction(3, 2)
    return sum(v < low for v in ordered), sum(v > high for v in ordered)


@pytest.mark.parametrize("values,expected", [
    ([1, 2, 3, 4, 100], (0, 1)),
    ([1, 2, 3, 4], (0, 0)),
    ([-4, 2, 2, 2, 6, 6, 6, 12], (0, 0)),
    ([math.nextafter(-4.0, -math.inf), 2, 2, 2, 6, 6, 6, 12], (1, 0)),
    ([-4, 2, 2, 2, 6, 6, 6, math.nextafter(12.0, math.inf)], (0, 1)),
    ([-100, -4, -3, -2, -1], (1, 0)),
    ([2**53, 2**53+1, 2**53+2, 2**53+3], (0, 0)),
    ([-(2**63), -1, 0, 1, 2**63-1], (1, 1)),
    ([-sys.float_info.max, -5e-324, 0.0, 5e-324, sys.float_info.max], (1, 1)),
    ([float(2**53+1), 2**53+1, 2**53+2, 2**53+3], (0, 0)),
])
def test_numeric_exact_reference(values, expected):
    original = values.copy()
    result = screen_numeric(values)
    assert result.status == "SCREENED"
    assert (result.below_fence_count, result.above_fence_count) == expected == reference(values)
    assert result.potential_outlier_count == sum(expected)
    assert values == original


def test_rounding_sensitive_fence():
    # Q1=0.00002, Q3=0.00006; the exact upper fence is near 0.00012.
    # Rounding the quartiles to four decimals would place the fence near 0.00025.
    values = [0.0, 0.00002, 0.00002, 0.00002, 0.00006, 0.00006, 0.00006, 0.00013]
    result = screen_numeric(values)
    assert (result.below_fence_count, result.above_fence_count) == reference(values) == (0, 1)


@pytest.mark.parametrize("values,status,total,nulls", [
    ([], "EMPTY", 0, 0), ([None, None], "ALL_NULL", 2, 2),
    ([1], "INSUFFICIENT_SAMPLE", 1, 0), ([1, 2], "INSUFFICIENT_SAMPLE", 2, 0),
    ([1, 2, None, 3], "INSUFFICIENT_SAMPLE", 4, 1),
    ([5, 5, 5, 5], "CONSTANT", 4, 0), ([-0.0, 0.0, 0, 0], "CONSTANT", 4, 0),
    ([0, 0, 0, 0, 1], "ZERO_IQR", 5, 0), ([None, 1, 2, 3, 4], "SCREENED", 5, 1),
])
def test_numeric_states(values, status, total, nulls):
    result = screen_numeric(values)
    assert (result.status, result.total_count, result.null_count, result.non_null_count) == (status, total, nulls, total - nulls)
    if status != "SCREENED":
        assert result.below_fence_count is result.above_fence_count is result.potential_outlier_count is None


class IntSubclass(int):
    pass


class StrSubclass(str):
    pass


@pytest.mark.parametrize("bad,status", [
    (True, "INVALID_INPUT"), ("secret-input", "INVALID_INPUT"),
    (Decimal("1"), "INVALID_INPUT"), (object(), "INVALID_INPUT"),
    (IntSubclass(1), "INVALID_INPUT"), (float("nan"), "INVALID_INPUT"),
    (float("inf"), "INVALID_INPUT"), (-float("inf"), "INVALID_INPUT"),
    (2**63, "UNSUPPORTED_NUMERIC_RANGE"), (-(2**63)-1, "UNSUPPORTED_NUMERIC_RANGE"),
])
def test_numeric_invalid(bad, status):
    result = screen_numeric([1, None, bad])
    assert result.status == status
    assert result.total_count is result.null_count is result.non_null_count is None
    assert result.to_dict()["findings"][0]["evidence"] == {}


@pytest.mark.parametrize("values,expected", [
    (["a", "b", "a", "b", "c", None], (6, 1, 3, 1)),
    (["", "NA", "nan", "na"], (4, 0, 4, 4)),
    ([True, False, True, None], (4, 1, 2, 1)),
])
def test_categories(values, expected):
    copy = values.copy()
    result = summarize_categories(values)
    assert result.status == "COUNTED"
    assert (result.total_count, result.null_count, result.distinct_count, result.singleton_category_count) == expected
    assert values == copy


@pytest.mark.parametrize("values,status", [
    ([], "EMPTY"), ([None, None], "ALL_NULL"), (["a", True], "INVALID_INPUT"),
    ([1], "INVALID_INPUT"), ([StrSubclass("x")], "INVALID_INPUT"),
    (["\ud800"], "INVALID_INPUT"), (["x" * 32769], "INVALID_INPUT"),
    (["é" * 16385], "INVALID_INPUT"),
])
def test_category_states(values, status):
    result = summarize_categories(values)
    assert result.status == status
    assert result.distinct_count is result.singleton_category_count is None
    if status == "INVALID_INPUT":
        assert result.total_count is result.null_count is result.non_null_count is None


def test_category_cardinality_cap_and_duplicate():
    values = [f"category-{i}" for i in range(10000)]
    result = summarize_categories(values + [values[0]])
    assert (result.distinct_count, result.singleton_category_count) == (10000, 9999)
    failed = summarize_categories(values + ["one-more"])
    assert failed.status == "CATEGORY_LIMIT_EXCEEDED"
    assert failed.to_dict()["findings"][0]["evidence"] == {}
    assert failed.total_count is failed.distinct_count is None


def test_category_byte_budget_exact_and_next_key():
    # 32 distinct keys, each exactly 32768 UTF-8 bytes.
    values = [f"{i:02}" + "x" * 32766 for i in range(32)]
    assert summarize_categories(values + [values[0]]).distinct_count == 32
    failed = summarize_categories(values + ["z"])
    assert failed.status == "CATEGORY_LIMIT_EXCEEDED"
    assert failed.total_count is failed.singleton_category_count is None


@pytest.mark.parametrize("fn", [screen_numeric, summarize_categories])
def test_container_entry_limit_and_privacy(fn):
    with pytest.raises(TypeError, match="^Values must be a list or tuple\\.$"):
        fn(iter(["secret-input"]))
    with pytest.raises(ValueError, match="^Column exceeds the entry limit\\.$"):
        fn(["secret-input"] * 1_000_001)


def test_first_invalid_event_wins_and_entry_limit_precedes_scan():
    assert screen_numeric([2**63, True]).status == "UNSUPPORTED_NUMERIC_RANGE"
    assert screen_numeric([True, 2**63]).status == "INVALID_INPUT"
    keys = [str(i) for i in range(10001)]
    assert summarize_categories(keys + [1]).status == "CATEGORY_LIMIT_EXCEEDED"
    assert summarize_categories(keys[:10000] + [1, keys[-1]]).status == "INVALID_INPUT"
    with pytest.raises(ValueError):
        screen_numeric([True] * 1_000_001)


def test_contract_serialization_findings_immutability_and_privacy():
    cases = [screen_numeric([1, 2, 3, 4, 100]), screen_numeric(["private-sentinel"]),
             screen_numeric([None]), summarize_categories(["private-sentinel", "other"]),
             summarize_categories([None]), summarize_categories([True, "private-sentinel"])]
    common = ["schema_version", "analysis", "method_version", "status", "total_count", "null_count", "non_null_count"]
    for result in cases:
        payload = result.to_dict()
        numeric = payload["analysis"] == "numeric_outliers"
        specific = ["below_fence_count", "above_fence_count", "potential_outlier_count", "method"] if numeric else ["distinct_count", "singleton_category_count"]
        assert list(payload) == common + specific + ["findings"]
        assert payload["schema_version"] == "1.0"
        assert len(payload["findings"]) == 1
        finding = payload["findings"][0]
        assert list(finding) == ["code", "method_version", "evidence"]
        assert finding["code"] == payload["status"] and finding["method_version"] == payload["method_version"]
        if payload["total_count"] is not None:
            assert payload["total_count"] == payload["null_count"] + payload["non_null_count"]
            assert all(payload[key] == value for key, value in finding["evidence"].items())
        else:
            assert finding["evidence"] == {}
        serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        assert "private-sentinel" not in serialized and "other" not in serialized
        assert serialized == json.dumps(result.to_dict(), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        with pytest.raises(FrozenInstanceError):
            result.status = "changed"
        payload["findings"].clear()
        assert len(result.to_dict()["findings"]) == 1


def test_permutations_are_deterministic():
    for fn, values in [(screen_numeric, [1, 2, 3, 4, 100]), (summarize_categories, ["a", "b", "a", None])]:
        serialize = lambda v: json.dumps(fn(v).to_dict(), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        expected = serialize(values)
        assert all(serialize(p) == expected for p in itertools.permutations(values))


class Deadline:
    def __init__(self, expire_at):
        self.calls = 0
        self.expire_at = expire_at

    def check(self):
        self.calls += 1
        if self.calls >= self.expire_at:
            raise ProcessingTimeoutError(2.0, 1.0)


@pytest.mark.parametrize("fn,values", [(screen_numeric, [1, 2, 3, 4, 100]), (summarize_categories, ["a", "b"])])
def test_cooperative_deadline_at_every_checkpoint(fn, values):
    probe = Deadline(10_000)
    fn(values, deadline=probe)
    for checkpoint in range(1, probe.calls + 1):
        with pytest.raises(ProcessingTimeoutError):
            fn(values, deadline=Deadline(checkpoint))


@pytest.mark.parametrize("fn,values", [(screen_numeric, list(range(2000))), (summarize_categories, [str(i) for i in range(2000)])])
def test_deadline_during_long_iteration(fn, values):
    with pytest.raises(ProcessingTimeoutError):
        fn(values, deadline=Deadline(3))


def test_actual_expired_deadline_precedes_validation():
    deadline = ExecutionDeadline(1.0, start_time=-10000.0)
    with pytest.raises(ProcessingTimeoutError):
        screen_numeric("private-sentinel", deadline=deadline)
    with pytest.raises(ProcessingTimeoutError):
        summarize_categories(["private-sentinel"] * 1_000_001, deadline=deadline)


def test_timeout_after_invalid_unicode_does_not_chain_cell_value():
    with pytest.raises(ProcessingTimeoutError) as caught:
        summarize_categories(["private-sentinel\ud800"], deadline=Deadline(3))
    assert caught.value.__context__ is None


@pytest.mark.parametrize("fn,values,status,counts,specific", [
    (screen_numeric, [], "EMPTY", (0, 0, 0), (None, None, None)),
    (screen_numeric, [None], "ALL_NULL", (1, 1, 0), (None, None, None)),
    (screen_numeric, [1], "INSUFFICIENT_SAMPLE", (1, 0, 1), (None, None, None)),
    (screen_numeric, [1] * 4, "CONSTANT", (4, 0, 4), (None, None, None)),
    (screen_numeric, [0, 0, 0, 0, 1], "ZERO_IQR", (5, 0, 5), (None, None, None)),
    (screen_numeric, [1, 2, 3, 4, 100], "SCREENED", (5, 0, 5), (0, 1, 1)),
    (screen_numeric, [None, True], "INVALID_INPUT", (None, None, None), (None, None, None)),
    (screen_numeric, [None, 2**63], "UNSUPPORTED_NUMERIC_RANGE", (None, None, None), (None, None, None)),
    (summarize_categories, [], "EMPTY", (0, 0, 0), (None, None)),
    (summarize_categories, [None], "ALL_NULL", (1, 1, 0), (None, None)),
    (summarize_categories, ["a", "b", "a", None], "COUNTED", (4, 1, 3), (2, 1)),
    (summarize_categories, [None, "a", True], "INVALID_INPUT", (None, None, None), (None, None)),
    (summarize_categories, [str(i) for i in range(10001)], "CATEGORY_LIMIT_EXCEEDED", (None, None, None), (None, None)),
])
def test_exact_public_schema_all_statuses(fn, values, status, counts, specific):
    numeric = fn is screen_numeric
    analysis = "numeric_outliers" if numeric else "categorical_counts"
    version = "tukey-linear-exact-v1" if numeric else "category-counts-v1"
    names = (["below_fence_count", "above_fence_count", "potential_outlier_count"]
             if numeric else ["distinct_count", "singleton_category_count"])
    base_counts = dict(zip(("total_count", "null_count", "non_null_count"), counts))
    metrics = dict(zip(names, specific))
    evidence = {} if counts[0] is None else dict(base_counts)
    if status in ("SCREENED", "COUNTED"):
        evidence.update(metrics)
    expected = {"schema_version": "1.0", "analysis": analysis, "method_version": version,
                "status": status, **base_counts, **metrics}
    if numeric:
        expected["method"] = {"quartiles": "linear interpolation at p*(n-1)",
                              "multiplier": "3/2", "fence_rule": "strictly outside",
                              "minimum_non_null_count": 4}
    expected["findings"] = [{"code": status, "method_version": version, "evidence": evidence}]
    payload = fn(values).to_dict()
    assert payload == expected
    assert list(payload) == list(expected)
    assert list(payload["findings"][0]["evidence"]) == list(evidence)
    assert json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False) == json.dumps(expected, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def test_repeated_unicode_categories_are_literal_and_fully_validated():
    key = "é" * 16384  # Exactly 32768 bytes.
    equal_copy = (key + "!")[:-1]
    assert equal_copy == key and equal_copy is not key
    values = [key, equal_copy] * 250 + ["é", "e\u0301", "NA", "na", ""]
    result = summarize_categories(values)
    assert (result.status, result.distinct_count, result.singleton_category_count) == ("COUNTED", 6, 5)
    assert key not in json.dumps(result.to_dict(), ensure_ascii=False)
    for invalid in ("é" * 16385, "bad\ud800", True):
        failed = summarize_categories(values + [invalid]).to_dict()
        assert failed["status"] == "INVALID_INPUT"
        assert [failed[k] for k in ("total_count", "null_count", "non_null_count", "distinct_count", "singleton_category_count")] == [None] * 5
        assert failed["findings"][0]["evidence"] == {}


def test_repeated_keys_do_not_exhaust_byte_budget_but_new_key_does():
    values = [f"{i:02}" + "é" * 16383 for i in range(32)]
    assert sum(len(v.encode("utf-8")) for v in values) == 1024 * 1024
    result = summarize_categories(values + [values[0]] * 500)
    assert (result.status, result.distinct_count, result.singleton_category_count) == ("COUNTED", 32, 31)
    failed = summarize_categories(values + [values[0]] * 500 + ["x"]).to_dict()
    assert failed["status"] == "CATEGORY_LIMIT_EXCEEDED"
    assert all(failed[k] is None for k in ("total_count", "null_count", "non_null_count", "distinct_count", "singleton_category_count"))
