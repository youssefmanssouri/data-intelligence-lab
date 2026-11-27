// Synthetic contract fixtures for UI tests only. Never part of the application bundle.
export const id = "ds_0123456789abcdef";
export const summary = {
  dataset_id: id,
  filename: "research.csv",
  created_at: "2026-10-29T10:00:00+00:00",
  row_count: 2,
  column_count: 1,
  data_quality_score: 100,
};
export function column(
  name = "amount",
  type = "integer",
  stats: Record<string, unknown> = { min: 1, max: 2, mean: 1.5 },
) {
  return {
    index: 0,
    name,
    raw_name: name,
    inferred_type: type,
    total_count: 2,
    null_count: 0,
    null_ratio: 0,
    distinct_count: 2,
    is_unique: true,
    stats,
    inference_reason: "NUMERIC_INTEGER",
    inference_policy_version: "1.0",
  };
}
export function metadata() {
  return {
    ...summary,
    encoding: "utf-8",
    delimiter: ",",
    quality: {
      overall_score: 100,
      completeness_score: 100,
      uniqueness_score: 100,
      validity_score: 100,
      total_cells: 2,
      total_missing_cells: 0,
      duplicate_row_count: 0,
      warnings: [] as string[],
    },
    columns: [column()],
    column_metadata: [],
    warnings: [] as string[],
  };
}
export function insights(items: Array<Record<string, unknown>> = []) {
  return {
    dataset_id: id,
    method_version: "1.0",
    rows_examined: 2,
    sampled: false,
    partial: false,
    truncated: false,
    items,
  };
}
export function insight(
  severity: "high" | "medium" | "info",
  title: string,
  detail = "Why it matters.",
) {
  return {
    code: "TEST_RULE",
    severity,
    title,
    detail,
    columns: ["amount"],
    evidence: { value: 1 },
  };
}
