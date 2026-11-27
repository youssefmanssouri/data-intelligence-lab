# Insights: how "What this data says" works

Since version 1.1.0 the dashboard opens with a panel of short findings in plain language, for example *"'order_date' has a gap of 22 days with no rows"* or *"Typical 'revenue' is 2.8x higher for 'Casablanca' than for 'Rabat'"*.

The findings are **rule-based and deterministic**:
- Every finding comes from a fixed rule with a fixed threshold, listed below.
- There is no AI model, no randomness and no network access.
- The same file always gives the same findings, on any PC.
- The rule set has a version, currently `1.0`, shown under the panel. Any change to a rule or threshold changes that version.

The code is in [`core/insights.py`](../core/insights.py), and every rule has a unit test in [`tests/unit/test_insights.py`](../tests/unit/test_insights.py).

## Where the findings come from

The backend computes the findings on request (`GET /api/v1/datasets/{id}/insights`), from the dataset's stored Parquet file and profile.

**Nothing is written to disk.** `metadata.json` keeps its rule of never containing cell values. The panel itself may quote a category label (up to 60 characters) or a number from your data, but only to you, in your own browser, on `127.0.0.1`.

Results are cached in memory for the 16 most recently viewed datasets and dropped when a dataset is deleted. Datasets saved by version 1.0 get findings too.

## Severity

| Label in the app | Value | Meaning |
|---|---|---|
| Important | `high` | Likely to distort any analysis: a completely empty column, at least half of a column missing, at least 10% duplicate rows. |
| Worth checking | `medium` | Should be looked at before relying on totals or averages. |
| Good to know | `info` | Describes the data: its shape, period, identifiers and relationships. |

Findings are listed by severity, then by strength. At most 25 are returned; the panel shows 8 and expands to all.

## Rules

| Code | Fires when | Severity |
|---|---|---|
| `EMPTY_COLUMN` | Every value in a column is missing. | high |
| `MISSING_VALUES` | At least 5% of a column is missing. | info from 5%, medium from 20%, high from 50% |
| `DUPLICATE_ROWS` | Some rows are exact copies of others. | medium; high from 10% |
| `CONSTANT_COLUMN` | Every filled row has the same value. | medium |
| `IDENTIFIER_COLUMN` | An integer or text column is unique in every row, in a dataset of at least 10 rows. | info |
| `OUTLIERS` | Values fall outside Tukey's fences (more than 1.5 × the interquartile range beyond the middle half), with at least 8 values. | info; medium when at least 1% of values |
| `SEPARATE_VALUE_GROUP` | More than 10% of values are beyond the fences. That many usually means a long tail or two kinds of rows (two price levels, for example), not errors. | info |
| `SKEWED_DISTRIBUTION` | Sample skewness \|g1\| ≥ 1, with at least 20 values and 10 distinct values. Coarse data such as 1–5 ratings is skipped. | info |
| `DATE_RANGE` | A date or timestamp column has values; states its first and last date. | info |
| `DATE_GAP` | The largest gap between consecutive dates is at least 7 days and more than 5 × the typical step, with at least 7 distinct dates. | medium |
| `DOMINANT_CATEGORY` | One value makes up at least 80% of a text column with 2–50 distinct values and at least 10 filled rows. | info |
| `CORRELATION` | Two numeric columns have Pearson \|r\| ≥ 0.70 over at least 10 rows. At most 25 columns are compared (300 pairs), and the strongest 5 are reported. | info |
| `GROUP_DIFFERENCE` | For a category column (2–20 groups with at least 5 rows each) and a positive numeric column, the highest group's **median and mean** are both at least 2 × the lowest group's. The best 3 are reported. | info |

### Why some rules are stricter than they first look

These checks were added after testing on realistic data, where the simpler versions produced false findings:
- **Group differences need both the median and the mean.** With medians alone, a discount column that only takes 0, 5, 10 and 15 showed a "2x" difference by chance. With means alone, a single extreme row could make one region look higher.
- **Skewness is computed from the values, not from mean minus median.** For 3–5 ratings the median is always a whole number, which made left-skewed ratings look right-skewed.
- **A large share beyond the fences is not called "outliers".** A price column with two regional price levels had 21% of its values "outside the range". It is reported as a separate group instead.

## Wording

- Relationships are described as columns that "rise together" or groups that "differ". Every relationship finding states that it is **not proof of a cause**.
- Findings state the evidence behind them, such as counts, percentages, medians or the date range, so they can be checked.
- An empty panel says that no notable pattern was found. It does not say that the data is correct.

## Limits

- **Sampling:** value-based rules read at most 20,000 rows, evenly spaced through the file, always the same rows for the same file. The panel says when a sample was used. Rules based on the profile (missing values, duplicates, identifiers, date range) always use every row.
- **Columns:** value-based rules look at the first 100 columns.
- **Time:** the rules stop after a 10-second budget, and the panel then says that some checks were skipped. A dataset of 100 columns takes about 4.5 s on a typical PC.
- **Failures:** a failure to compute insights never affects the upload or the rest of the dashboard.
