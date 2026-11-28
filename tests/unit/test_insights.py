"""Unit tests for core.insights: every rule, its thresholds, ranking and determinism."""

from __future__ import annotations

from datetime import date, timedelta
import itertools
import json

import pytest

from core import ingest_csv
from core.insights import (
    MAX_INSIGHTS,
    SAMPLE_ROWS,
    fmt_number,
    fmt_pct,
    generate_insights,
    sample_indices,
)


def col(name, inferred_type="float", *, total=100, nulls=0, distinct=50, unique=False, stats=None):
    return {
        "name": name,
        "inferred_type": inferred_type,
        "total_count": total,
        "null_count": nulls,
        "null_ratio": nulls / total if total else 0.0,
        "distinct_count": distinct,
        "is_unique": unique,
        "stats": stats or {},
    }


def codes(result):
    return [item["code"] for item in result["items"]]


def by_code(result, code):
    return [item for item in result["items"] if item["code"] == code]


def run(columns, values=None, row_count=100, duplicates=0, **kw):
    return generate_insights(columns, values or {}, row_count=row_count, duplicate_rows=duplicates, **kw)


# ---------------------------------------------------------------- formatting

def test_number_and_percent_formatting():
    assert fmt_number(1234567) == "1,234,567"
    assert fmt_number(12.0) == "12"
    assert fmt_number(3.14159) == "3.14"
    assert fmt_number(0.012345) == "0.01235"
    assert fmt_number(12345.678) == "12,345.7"
    assert fmt_pct(0.5) == "50%"
    assert fmt_pct(0.034) == "3.4%"
    assert fmt_pct(0.0012) == "0.12%"


# ---------------------------------------------------------------- profile rules

def test_missing_values_thresholds_and_severity():
    result = run([
        col("a", nulls=4), col("b", nulls=5), col("c", nulls=20), col("d", nulls=50), col("e", nulls=100),
    ])
    missing = {item["columns"][0]: item for item in result["items"] if item["code"] in ("MISSING_VALUES", "EMPTY_COLUMN")}
    assert "a" not in missing  # 4% is below the 5% threshold
    assert missing["b"]["severity"] == "info"
    assert missing["c"]["severity"] == "medium"
    assert missing["d"]["severity"] == "high"
    assert missing["e"]["code"] == "EMPTY_COLUMN"
    assert "missing 20% of its values (20 rows)" in missing["c"]["title"]


def test_duplicate_rows():
    assert by_code(run([], row_count=100, duplicates=0), "DUPLICATE_ROWS") == []
    medium = by_code(run([], row_count=100, duplicates=3), "DUPLICATE_ROWS")[0]
    assert medium["severity"] == "medium"
    assert medium["title"].startswith("3 rows are exact duplicates (3.0%")
    assert by_code(run([], row_count=100, duplicates=10), "DUPLICATE_ROWS")[0]["severity"] == "high"


def test_constant_and_identifier_columns():
    result = run([
        col("status", "string", distinct=1),
        col("order_id", "integer", distinct=100, unique=True),
        col("price", "float", distinct=100, unique=True),
    ])
    assert by_code(result, "CONSTANT_COLUMN")[0]["columns"] == ["status"]
    ids = by_code(result, "IDENTIFIER_COLUMN")
    assert [i["columns"] for i in ids] == [["order_id"]]  # unique floats are measurements, not ids


def test_identifier_needs_enough_rows():
    assert by_code(run([col("id", "integer", total=5, distinct=5, unique=True)], row_count=5), "IDENTIFIER_COLUMN") == []


def test_skew_uses_moment_skewness_from_values():
    right = [1.0] * 30 + [2.0] * 20 + [3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 25.0, 40.0, 80.0]
    result = run([col("income")], {"income": right})
    skew = by_code(result, "SKEWED_DISTRIBUTION")
    assert [s["columns"] for s in skew] == [["income"]]
    assert "pull the average up" in skew[0]["title"]
    assert skew[0]["evidence"]["skewness"] > 1


def test_no_skew_claim_for_coarse_ratings_or_symmetric_data():
    ratings = [3, 4, 4, 5, 5, 5] * 20  # mean > median only because the median is a whole number
    assert by_code(run([col("rating", "integer", distinct=3)], {"rating": ratings}), "SKEWED_DISTRIBUTION") == []
    symmetric = [float(i) for i in range(100)]
    assert by_code(run([col("x")], {"x": symmetric}), "SKEWED_DISTRIBUTION") == []


def test_date_range():
    result = run([col("day", "date", stats={"min_date": "2024-01-01", "max_date": "2024-12-31"})])
    assert by_code(result, "DATE_RANGE")[0]["title"] == "'day' runs from 2024-01-01 to 2024-12-31."


# ---------------------------------------------------------------- value rules

def test_outliers_tukey_fences():
    values = list(range(1, 21)) + [500]
    result = run([col("amount", "integer", total=21, distinct=21)], {"amount": values}, row_count=21)
    item = by_code(result, "OUTLIERS")[0]
    assert item["evidence"]["above_fence"] == 1
    assert item["evidence"]["below_fence"] == 0
    assert item["evidence"]["most_extreme"] == 500
    assert item["severity"] == "medium"  # 1 of 21 is more than 1%
    assert "1 of 21 examined" in item["title"]
    assert item["detail"].startswith("The other values lie between 1 and 20;")


def test_no_outliers_for_uniform_or_short_columns():
    assert by_code(run([col("x")], {"x": list(range(100))}), "OUTLIERS") == []
    assert by_code(run([col("x")], {"x": [1, 2, 3, 1000]}), "OUTLIERS") == []  # fewer than 8 values


def test_dominant_category():
    values = ["Morocco"] * 85 + ["France"] * 10 + ["Spain"] * 5
    result = run([col("country", "string", distinct=3)], {"country": values})
    item = by_code(result, "DOMINANT_CATEGORY")[0]
    assert item["evidence"]["top_value"] == "Morocco"
    assert "85%" in item["title"]
    balanced = ["a", "b"] * 50
    assert by_code(run([col("c", "string", distinct=2)], {"c": balanced}), "DOMINANT_CATEGORY") == []


def test_date_gap():
    start = date(2024, 1, 1)
    days = [start + timedelta(days=i) for i in range(30)] + [start + timedelta(days=i) for i in range(60, 90)]
    result = run([col("day", "date", total=60)], {"day": days}, row_count=60)
    gap = by_code(result, "DATE_GAP")[0]
    assert gap["evidence"]["gap_days"] == 31
    assert gap["evidence"]["gap_start"] == "2024-01-30"
    assert gap["evidence"]["typical_step_days"] == 1
    regular = [start + timedelta(days=7 * i) for i in range(20)]
    assert by_code(run([col("week", "date", total=20)], {"week": regular}, row_count=20), "DATE_GAP") == []


def test_correlation_wording_and_threshold():
    xs = list(range(50))
    ys = [2 * x + (x % 3) for x in xs]
    zs = [((x * 37) % 11) for x in xs]
    result = run([col("ads"), col("sales"), col("noise")], {"ads": xs, "sales": ys, "noise": zs}, row_count=50)
    corr = by_code(result, "CORRELATION")
    assert [c["columns"] for c in corr] == [["ads", "sales"]]
    assert "rise together" in corr[0]["title"]
    assert "does not show that one causes the other" in corr[0]["detail"]
    negative = run([col("a"), col("b")], {"a": xs, "b": [-v for v in xs]}, row_count=50)
    assert "opposite directions" in by_code(negative, "CORRELATION")[0]["title"]


def test_correlation_needs_ten_pairs():
    xs = list(range(9))
    assert by_code(run([col("a"), col("b")], {"a": xs, "b": xs}), "CORRELATION") == []


def test_group_difference():
    regions = ["North"] * 10 + ["South"] * 10
    revenue = [100.0] * 10 + [30.0] * 10
    result = run([col("region", "string", distinct=2), col("revenue")], {"region": regions, "revenue": revenue}, row_count=20)
    item = by_code(result, "GROUP_DIFFERENCE")[0]
    assert item["evidence"]["highest_group"] == "North"
    assert item["evidence"]["lowest_group"] == "South"
    assert item["title"].startswith("Typical 'revenue' is 3.3x higher for 'North' (median 100)")
    assert "not proof of a cause" in item["detail"]


def test_group_difference_uses_medians_so_one_extreme_row_does_not_decide():
    regions = ["North"] * 10 + ["South"] * 10
    revenue = [50.0] * 9 + [5000.0] + [40.0] * 10  # a single extreme North row
    result = run([col("region", "string", distinct=2), col("revenue")], {"region": regions, "revenue": revenue}, row_count=20)
    assert by_code(result, "GROUP_DIFFERENCE") == []


def test_group_difference_skips_small_groups_and_non_positive_means():
    regions = ["North"] * 10 + ["South"] * 4
    small = run([col("region", "string", distinct=2), col("v")], {"region": regions, "v": [10.0] * 10 + [1.0] * 4})
    assert by_code(small, "GROUP_DIFFERENCE") == []
    signed = run([col("g", "string", distinct=2), col("v")],
                 {"g": ["a"] * 10 + ["b"] * 10, "v": [5.0] * 10 + [-5.0] * 10})
    assert by_code(signed, "GROUP_DIFFERENCE") == []


def test_columns_without_values_skip_value_rules():
    result = run([col("amount", "integer", total=21, distinct=21)], {}, row_count=21)
    assert by_code(result, "OUTLIERS") == []


# ---------------------------------------------------------------- ordering, limits, determinism

def test_items_are_sorted_by_severity_and_capped():
    columns = [col(f"c{i}", nulls=5 + i) for i in range(40)]
    result = run(columns + [col("empty", nulls=100)])
    severities = [item["severity"] for item in result["items"]]
    order = {"high": 0, "medium": 1, "info": 2}
    assert severities == sorted(severities, key=order.get)
    assert result["items"][0]["code"] == "EMPTY_COLUMN"
    assert len(result["items"]) == MAX_INSIGHTS
    assert result["truncated"] is True
    assert all("_rank" not in item for item in result["items"])


def test_time_budget_marks_result_partial():
    ticks = itertools.count(0, 100)  # each clock read advances 100 s
    xs = list(range(50))
    result = run([col("a"), col("b")], {"a": xs, "b": xs}, row_count=50, clock=lambda: next(ticks))
    assert result["partial"] is True
    assert by_code(result, "CORRELATION") == []


def test_sample_indices_are_even_and_deterministic():
    assert sample_indices(10) == list(range(10))
    picked = sample_indices(1_000_000)
    assert len(picked) == SAMPLE_ROWS
    assert picked[0] == 0 and picked[1] == 50 and picked[-1] == 999_950
    assert picked == sample_indices(1_000_000)


def test_sampled_flag():
    result = run([col("a")], {"a": [1.0] * 10}, row_count=1000, rows_examined=10)
    assert result["sampled"] is True and result["rows_examined"] == 10
    assert run([col("a")], {"a": [1.0] * 10}, row_count=10)["sampled"] is False


def test_end_to_end_from_ingested_csv_is_deterministic_and_json_safe():
    lines = ["order_id,region,amount,discount,day"]
    for i in range(60):
        region = "North" if i % 3 else "South"
        amount = 900.0 if i == 59 else (40.0 if region == "North" else 12.0) + (i % 5)
        discount = "" if i % 4 == 0 else str(amount / 10)
        lines.append(f"{i + 1},{region},{amount},{discount},2024-01-{(i % 28) + 1:02d}")
    result = ingest_csv(("\n".join(lines) + "\n").encode())
    columns = [
        {
            "name": c.name, "inferred_type": c.inferred_type, "total_count": c.total_count,
            "null_count": c.null_count, "null_ratio": c.null_ratio, "distinct_count": c.distinct_count,
            "is_unique": c.is_unique, "stats": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in c.stats.items()},
        }
        for c in result.profile.columns
    ]
    values = {name: [row[name] for row in result.records] for name in (c["name"] for c in columns)}
    first = generate_insights(columns, values, row_count=60, duplicate_rows=0)
    second = generate_insights(columns, values, row_count=60, duplicate_rows=0)
    assert first == second
    json.dumps(first)  # serializable as returned
    found = set(codes(first))
    assert {"IDENTIFIER_COLUMN", "MISSING_VALUES", "OUTLIERS", "GROUP_DIFFERENCE", "DATE_RANGE"} <= found
    assert all(item["severity"] in ("high", "medium", "info") for item in first["items"])


def test_many_far_values_are_described_as_a_separate_group_not_errors():
    values = [100.0 + (i % 10) for i in range(80)] + [300.0 + (i % 10) for i in range(20)]  # 20% at a second level
    result = run([col("price")], {"price": values})
    assert by_code(result, "OUTLIERS") == []
    group = by_code(result, "SEPARATE_VALUE_GROUP")[0]
    assert group["severity"] == "info"
    assert "separate group of high values: 20%" in group["title"]
    assert "mixes different kinds of rows" in group["detail"]


def test_group_difference_ignores_median_flips_on_discrete_values():
    groups = ["a"] * 20 + ["b"] * 20
    b = [5] * 11 + [10] * 9  # median 5, mean 7.25
    a = [10] * 11 + [5] * 9  # median 10, mean 7.75
    result = run([col("g", "string", distinct=2), col("discount")], {"g": groups, "discount": a + b}, row_count=40)
    assert by_code(result, "GROUP_DIFFERENCE") == []
