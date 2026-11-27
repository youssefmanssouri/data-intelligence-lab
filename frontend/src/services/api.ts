import { isLosslessNumber, LosslessNumber, parse } from "lossless-json";
import { readJsonBody, ResponseBudgetError } from "./json-body.mjs";

export type Numeric = LosslessNumber;
export type Statistic = Numeric | string | boolean | null;
export type Column = {
  index: Numeric;
  name: string;
  inferred_type: string;
  total_count: Numeric;
  null_count: Numeric;
  null_ratio: Numeric;
  distinct_count: Numeric;
  is_unique: boolean;
  inference_reason: string;
  inference_policy_version: string;
  stats: Record<string, Statistic>;
};
export type Summary = {
  dataset_id: string;
  filename: string | null;
  created_at: string | null;
  row_count: Numeric;
  column_count: Numeric;
  data_quality_score: Numeric;
};
export type Dataset = Summary & {
  encoding: string;
  delimiter: string;
  quality: {
    overall_score: Numeric;
    completeness_score: Numeric;
    uniqueness_score: Numeric;
    validity_score: Numeric;
    total_cells: Numeric;
    total_missing_cells: Numeric;
    duplicate_row_count: Numeric;
    warnings: string[];
  };
  columns: Column[];
  warnings: string[];
};
export type Severity = "high" | "medium" | "info";
export type Insight = {
  code: string;
  severity: Severity;
  title: string;
  detail: string;
  columns: string[];
};
export type Insights = {
  dataset_id: string;
  method_version: string;
  rows_examined: Numeric;
  sampled: boolean;
  partial: boolean;
  truncated: boolean;
  items: Insight[];
};

export const REQUEST_TIMEOUT_MS = 120_000;
export const FILE_CEILING = 200 * 1024 * 1024;
const reserved = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i;
export function validDatasetId(id: string): boolean {
  return /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(id) && !reserved.test(id);
}
export function datasetPath(id: string): string {
  if (!validDatasetId(id))
    throw new ApiError(
      "This dataset link is invalid. Upload a CSV to start again.",
    );
  return `/datasets/${encodeURIComponent(id)}`;
}

export const STAT_FIELDS: Record<
  string,
  ReadonlyArray<readonly [string, string]>
> = {
  integer: [
    ["min", "Minimum"],
    ["max", "Maximum"],
    ["mean", "Mean"],
    ["median", "Median"],
    ["q25", "25th percentile"],
    ["q75", "75th percentile"],
    ["iqr", "Interquartile range"],
    ["std_dev", "Sample standard deviation"],
    ["variance", "Sample variance"],
  ],
  float: [
    ["min", "Minimum"],
    ["max", "Maximum"],
    ["mean", "Mean"],
    ["median", "Median"],
    ["q25", "25th percentile"],
    ["q75", "75th percentile"],
    ["iqr", "Interquartile range"],
    ["std_dev", "Sample standard deviation"],
    ["variance", "Sample variance"],
  ],
  string: [
    ["min_length", "Shortest length"],
    ["max_length", "Longest length"],
    ["avg_length", "Average length"],
    ["empty_string_count", "Empty strings"],
  ],
  boolean: [
    ["true_count", "True values"],
    ["false_count", "False values"],
  ],
  date: [
    ["min_date", "Earliest date"],
    ["max_date", "Latest date"],
  ],
  datetime: [
    ["min_date", "Earliest datetime"],
    ["max_date", "Latest datetime"],
  ],
};

export function display(value: Statistic | undefined): string {
  if (value === undefined || value === null) return "Not available";
  if (isLosslessNumber(value)) return value.value;
  if (typeof value === "number" && Object.is(value, -0)) return "-0";
  return String(value);
}
export function sourceName(value: string | null): string {
  return (
    value
      ?.split(/[\\/]/)
      .at(-1)
      ?.replace(/[\u0000-\u001f\u007f]/g, "") || "Untitled dataset"
  );
}
// Presentation only. Never alter metadata, numeric tokens, or scoring inputs.
export function clipText(value: string, limit = 240): string {
  if (value.length <= limit) return value;
  let prefix = value.slice(0, limit);
  if (/[\uD800-\uDBFF]$/.test(prefix)) prefix = prefix.slice(0, -1);
  return `${prefix}…`;
}
export function fileError(file: File | null): string | null {
  if (!file) return "Choose a CSV file first.";
  if (!/\.csv$/i.test(file.name))
    return "Choose a file with the .csv extension.";
  if (file.size === 0)
    return "This file is empty. Choose a CSV with a header row.";
  if (file.size >= FILE_CEILING)
    return "Choose a CSV smaller than 200 MiB. The server also counts multipart framing and may use a lower configured limit.";
  return null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
const statusMessages: Record<number, string> = {
  400: "The request could not be accepted. Check that the CSV contains a valid header and data.",
  404: "Dataset not found. Check the link or upload the CSV again.",
  413: "The upload exceeds the server’s size limit. Choose a smaller CSV.",
  422: "The CSV could not be processed safely. Check its headers, structure, cell sizes, and value formats.",
  500: "The local service could not complete the request. Check the backend and try again.",
  502: "The local backend is unavailable. Start it and try again.",
  503: "The local service is not ready. Check that dataset storage is available.",
  504: "The request timed out. An upload may still have been saved; avoid repeatedly submitting the same file.",
};
export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "Something went wrong. Please try again.";
}
function invalid(): never {
  throw new ApiError(
    "The service returned an invalid response. Check the backend version and try again.",
  );
}
type Obj = Record<string, unknown>;
function object(value: unknown): Obj {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    isLosslessNumber(value)
  )
    invalid();
  return value as Obj;
}
function text(value: unknown): string {
  if (typeof value !== "string") invalid();
  return value;
}
function numeric(value: unknown, count = false, score = false): Numeric {
  if (!isLosslessNumber(value)) invalid();
  if (count && !/^\d+$/.test(value.value)) invalid();
  if (
    score &&
    (!Number.isFinite(Number(value.value)) ||
      Number(value.value) < 0 ||
      Number(value.value) > 100)
  )
    invalid();
  return value;
}
function strings(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((v) => typeof v === "string"))
    invalid();
  return value;
}
function summary(value: unknown): Summary {
  const d = object(value);
  const id = text(d.dataset_id);
  if (!validDatasetId(id)) invalid();
  return {
    dataset_id: id,
    filename: d.filename === null ? null : text(d.filename),
    created_at: d.created_at === null ? null : text(d.created_at),
    row_count: numeric(d.row_count, true),
    column_count: numeric(d.column_count, true),
    data_quality_score: numeric(d.data_quality_score, false, true),
  };
}
function dataset(value: unknown): Dataset {
  const d = object(value),
    q = object(d.quality);
  if (!Array.isArray(d.columns)) invalid();
  const columns = d.columns.map((value) => {
    const c = object(value),
      stats = object(c.stats);
    const type = text(c.inferred_type);
    if (!Object.hasOwn(STAT_FIELDS, type)) invalid();
    if (typeof c.is_unique !== "boolean") invalid();
    const selected: Record<string, Statistic> = {};
    for (const [key] of STAT_FIELDS[type]) {
      if (stats[key] === undefined) continue;
      const v = stats[key];
      selected[key] =
        v === null
          ? null
          : type === "date" || type === "datetime"
            ? text(v)
            : numeric(v);
    }
    const ratio = numeric(c.null_ratio);
    if (
      Number(ratio.value) < 0 ||
      Number(ratio.value) > 1 ||
      !Number.isFinite(Number(ratio.value))
    )
      invalid();
    return {
      index: numeric(c.index, true),
      name: text(c.name),
      inferred_type: type,
      total_count: numeric(c.total_count, true),
      null_count: numeric(c.null_count, true),
      null_ratio: ratio,
      distinct_count: numeric(c.distinct_count, true),
      is_unique: c.is_unique,
      inference_reason: text(c.inference_reason),
      inference_policy_version: text(c.inference_policy_version),
      stats: selected,
    };
  });
  const s = summary(d);
  if (BigInt(s.column_count.value) !== BigInt(columns.length)) invalid();
  return {
    ...s,
    encoding: text(d.encoding),
    delimiter: text(d.delimiter),
    columns,
    warnings: strings(d.warnings),
    quality: {
      overall_score: numeric(q.overall_score, false, true),
      completeness_score: numeric(q.completeness_score, false, true),
      uniqueness_score: numeric(q.uniqueness_score, false, true),
      validity_score: numeric(q.validity_score, false, true),
      total_cells: numeric(q.total_cells, true),
      total_missing_cells: numeric(q.total_missing_cells, true),
      duplicate_row_count: numeric(q.duplicate_row_count, true),
      warnings: strings(q.warnings),
    },
  };
}
const SEVERITIES: ReadonlySet<string> = new Set(["high", "medium", "info"]);
function flag(value: unknown): boolean {
  if (typeof value !== "boolean") invalid();
  return value;
}
function insights(value: unknown): Insights {
  const d = object(value);
  const id = text(d.dataset_id);
  if (!validDatasetId(id)) invalid();
  if (!Array.isArray(d.items) || d.items.length > 100) invalid();
  // Evidence is kept server-side for auditing; only the rendered text is displayed.
  const items = d.items.map((value): Insight => {
    const i = object(value);
    const severity = text(i.severity);
    if (!SEVERITIES.has(severity)) invalid();
    return {
      code: text(i.code),
      severity: severity as Severity,
      title: text(i.title),
      detail: text(i.detail),
      columns: strings(i.columns),
    };
  });
  return {
    dataset_id: id,
    method_version: text(d.method_version),
    rows_examined: numeric(d.rows_examined, true),
    sampled: flag(d.sampled),
    partial: flag(d.partial),
    truncated: flag(d.truncated),
    items,
  };
}
async function request(
  path: string,
  init: RequestInit,
  signal?: AbortSignal,
): Promise<unknown> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      ...init,
      signal: controller.signal,
      cache: "no-store",
      headers: { Accept: "application/json, application/problem+json" },
      credentials: "omit",
      redirect: "error",
    });
    // Never echo backend/proxy text, HTML, exception details, paths, or validation inputs.
    if (!response.ok) {
      try {
        await response.body?.cancel();
      } catch {
        /* Best effort. */
      }
      throw new ApiError(
        statusMessages[response.status] ??
          "The service could not complete this request. Please try again.",
        response.status,
      );
    }
    if (
      response.status !== (init.method === "POST" ? 201 : 200) ||
      (response.headers.get("content-type") ?? "")
        .split(";")[0]
        .trim()
        .toLowerCase() !== "application/json"
    ) {
      try {
        await response.body?.cancel();
      } catch {
        /* Best effort. */
      }
      invalid();
    }
    const body = await readJsonBody(response);
    try {
      return parse(body);
    } catch (e) {
      if (e instanceof ApiError) throw e;
      invalid();
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof ResponseBudgetError)
      throw new ApiError(
        "The metadata response exceeds the 8 MiB display limit. Try a dataset with shorter headers or fewer columns, then try again.",
      );
    if (controller.signal.aborted)
      throw new ApiError(
        "The request timed out or was cancelled. An upload may still have been saved; avoid repeatedly submitting the same file.",
      );
    throw new ApiError(
      "Cannot reach the local service. Check that the backend is running and try again.",
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}
export async function uploadDataset(
  file: File,
  signal?: AbortSignal,
): Promise<Summary> {
  const body = new FormData();
  body.append("file", file);
  return summary(
    await request("/api/v1/datasets/upload", { method: "POST", body }, signal),
  );
}
export async function getDataset(
  id: string,
  signal?: AbortSignal,
): Promise<Dataset> {
  datasetPath(id);
  const d = dataset(
    await request(
      `/api/v1/datasets/${encodeURIComponent(id)}`,
      { method: "GET" },
      signal,
    ),
  );
  if (d.dataset_id !== id) invalid();
  return d;
}
export async function getInsights(
  id: string,
  signal?: AbortSignal,
): Promise<Insights> {
  datasetPath(id);
  const d = insights(
    await request(
      `/api/v1/datasets/${encodeURIComponent(id)}/insights`,
      { method: "GET" },
      signal,
    ),
  );
  if (d.dataset_id !== id) invalid();
  return d;
}
