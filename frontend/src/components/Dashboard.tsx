"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  display,
  clipText,
  errorMessage,
  getDataset,
  sourceName,
  STAT_FIELDS,
  type Column,
  type Dataset,
} from "@/services/api";
import s from "./workspace.module.css";

export function MetadataLoading() {
  return (
    <div role="status" className={s.loading}>
      <p className={s.eyebrow}>Dataset profile</p>
      <h1>Retrieving your profile.</h1>
      <p>Reading stored metadata from the local backend.</p>
      <progress aria-label="Loading dataset metadata" />
    </div>
  );
}

function ColumnProfile({ column: c }: { column: Column }) {
  const [expanded, setExpanded] = useState(false);
  const hasStats = Object.keys(c.stats).length > 0;
  return (
    <details
      className={s.column}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span className={s.columnName}>{clipText(c.name)}</span>
        <span className={s.badge}>{c.inferred_type}</span>
        <span className={s.columnHint}>{display(c.null_count)} missing</span>
        <span className={s.plus} aria-hidden="true">
          +
        </span>
      </summary>
      {expanded ? (
        <div className={s.columnBody}>
          <dl className={s.stats}>
            <div>
              <dt>Total values</dt>
              <dd>{display(c.total_count)}</dd>
            </div>
            <div>
              <dt>Missing values</dt>
              <dd>{display(c.null_count)}</dd>
            </div>
            <div>
              <dt>Missing ratio (0–1)</dt>
              <dd>{display(c.null_ratio)}</dd>
            </div>
            <div>
              <dt>Distinct non-null values</dt>
              <dd>{display(c.distinct_count)}</dd>
            </div>
            <div>
              <dt>All values unique & non-null</dt>
              <dd>{display(c.is_unique)}</dd>
            </div>
          </dl>
          {hasStats ? (
            <dl className={s.stats}>
              {STAT_FIELDS[c.inferred_type].map(([key, label]) => (
                <div key={key}>
                  <dt>{label}</dt>
                  <dd>
                    {typeof c.stats[key] === "string"
                      ? clipText(c.stats[key])
                      : display(c.stats[key])}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className={s.quiet}>
              {c.total_count.value === "0"
                ? "No observations. This dataset contains a header row only."
                : c.null_count.value === c.total_count.value
                  ? "All values are missing. No type-specific statistics are available."
                  : "No type-specific statistics are available for this column."}
            </p>
          )}
          <p className={s.inference}>
            Inference reason: {clipText(c.inference_reason)} · Policy version{" "}
            {clipText(c.inference_policy_version)}. Uniqueness describes
            observed values and does not classify an identifier.
          </p>
        </div>
      ) : null}
    </details>
  );
}

const COLUMNS_PER_PAGE = 25;
function ColumnProfiles({ columns }: { columns: Column[] }) {
  const [page, setPage] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const current = Math.min(
    page,
    Math.max(0, Math.ceil(columns.length / COLUMNS_PER_PAGE) - 1),
  );
  const first = current * COLUMNS_PER_PAGE;
  function move(next: number) {
    setPage(next);
    heading.current?.focus();
  }
  return (
    <section className={s.section} aria-labelledby="columns-title">
      <div className={s.sectionHeader}>
        <h2 id="columns-title" ref={heading} tabIndex={-1}>
          Column profiles
        </h2>
        <p>Expand a column to inspect its statistics</p>
      </div>
      <p className={s.quiet}>
        Long text is shortened for display. Numeric statistics are shown without
        truncation.
      </p>
      {columns.length ? (
        <>
          <div className={s.columns}>
            {columns
              .slice(first, first + COLUMNS_PER_PAGE)
              .map((column, index) => (
                <ColumnProfile
                  key={`${first + index}-${column.name}`}
                  column={column}
                />
              ))}
          </div>
          {columns.length > COLUMNS_PER_PAGE ? (
            <nav aria-label="Column profile pages" className={s.errorActions}>
              <button
                className={s.secondary}
                disabled={current === 0}
                onClick={() => move(current - 1)}
              >
                Previous columns
              </button>
              <p role="status">
                Columns {first + 1}–
                {Math.min(first + COLUMNS_PER_PAGE, columns.length)} of{" "}
                {columns.length}
              </p>
              <button
                className={s.secondary}
                disabled={first + COLUMNS_PER_PAGE >= columns.length}
                onClick={() => move(current + 1)}
              >
                Next columns
              </button>
            </nav>
          ) : null}
        </>
      ) : (
        <p className={s.quiet}>No column profiles were returned.</p>
      )}
    </section>
  );
}

function Profile({
  dataset: d,
  refresh,
  refreshing,
}: {
  dataset: Dataset;
  refresh: () => void;
  refreshing: boolean;
}) {
  const [warningLimit, setWarningLimit] = useState(20);
  const warnings = [...new Set([...d.quality.warnings, ...d.warnings])];
  const breakdown = [
    {
      label: "Completeness",
      score: d.quality.completeness_score,
      note: "60% weight · Proportion of populated cells.",
    },
    {
      label: "Row uniqueness",
      score: d.quality.uniqueness_score,
      note: "30% weight · Proportion of distinct raw rows.",
    },
    {
      label: "Column variation",
      score: d.quality.validity_score,
      note: "10% weight · The backend “validity” score penalises constant columns.",
    },
  ];
  return (
    <>
      <Link className={s.back} href="/">
        ← Import another dataset
      </Link>
      <div className={s.dashboardHead}>
        <div>
          <p className={s.eyebrow}>Dataset overview</p>
          <h1>Data Health Dashboard</h1>
          <p className={s.filename}>{clipText(sourceName(d.filename))}</p>
          <div className={s.meta}>
            <span>Dataset {d.dataset_id}</span>
            <span>Uploaded {clipText(d.created_at ?? "Not available")}</span>
          </div>
        </div>
        <button className={s.secondary} disabled={refreshing} onClick={refresh}>
          {refreshing ? "Refreshing…" : "Refresh metadata"}
        </button>
      </div>
      <section className={s.overview} aria-label="Dataset health summary">
        <div className={s.health}>
          <h2>Data health score</h2>
          <p className={s.healthValue}>
            {display(d.quality.overall_score)} <span>/ 100</span>
          </p>
          <p className={s.healthNote}>
            A structural quality measure of completeness, row uniqueness, and
            column variation. It does not measure semantic validity or fitness
            for your analysis.
          </p>
        </div>
        <dl className={s.facts}>
          <div className={s.fact}>
            <dt>Rows</dt>
            <dd>{display(d.row_count)}</dd>
          </div>
          <div className={s.fact}>
            <dt>Columns</dt>
            <dd>{display(d.column_count)}</dd>
          </div>
          <div className={s.fact}>
            <dt>Missing cells</dt>
            <dd>{display(d.quality.total_missing_cells)}</dd>
          </div>
          <div className={s.fact}>
            <dt>Duplicate rows</dt>
            <dd>{display(d.quality.duplicate_row_count)}</dd>
          </div>
        </dl>
      </section>
      {d.row_count.value === "0" ? (
        <div className={s.empty}>
          <h2>A structure without observations.</h2>
          <p>
            This CSV contains a header row only. The backend assigns a score of
            100 to an empty dataset; it is not evidence of data quality. Add
            observations to obtain meaningful profiles.
          </p>
        </div>
      ) : null}
      <section className={s.section} aria-labelledby="quality-title">
        <div className={s.sectionHeader}>
          <h2 id="quality-title">How the score is built</h2>
          <p>Backend metrics · no additional scoring</p>
        </div>
        <div className={s.breakdown}>
          {breakdown.map((item) => (
            <div key={item.label}>
              <p className={s.scoreTitle}>
                {item.label}
                <span>{display(item.score)} / 100</span>
              </p>
              <span className={s.bar} aria-hidden="true">
                <span style={{ width: `${Number(item.score.value)}%` }} />
              </span>
              <p className={s.scoreText}>{item.note}</p>
            </div>
          ))}
        </div>
      </section>
      <section className={s.section} aria-labelledby="warnings-title">
        <div className={s.sectionHeader}>
          <h2 id="warnings-title">Quality notes</h2>
          <p>
            {warnings.length} {warnings.length === 1 ? "note" : "notes"}
          </p>
        </div>
        {warnings.length ? (
          <>
            <ul id="quality-notes-list" className={s.warnings}>
              {warnings.slice(0, warningLimit).map((warning) => (
                <li key={warning}>{clipText(warning, 400)}</li>
              ))}
            </ul>
            {warnings.length > 20 ? (
              <div className={s.errorActions}>
                <p role="status">
                  Showing {Math.min(warningLimit, warnings.length)} of{" "}
                  {warnings.length} notes.
                </p>
                {warningLimit < warnings.length ? (
                  <button
                    className={s.secondary}
                    aria-controls="quality-notes-list"
                    onClick={() => setWarningLimit((limit) => limit + 20)}
                  >
                    Show more notes
                  </button>
                ) : null}
                {warningLimit > 20 ? (
                  <button
                    className={s.secondary}
                    aria-controls="quality-notes-list"
                    onClick={() => setWarningLimit(20)}
                  >
                    Show less notes
                  </button>
                ) : null}
              </div>
            ) : null}
          </>
        ) : (
          <p className={s.quiet}>
            No quality warnings were reported by the backend. This does not
            establish semantic correctness.
          </p>
        )}
      </section>
      <ColumnProfiles columns={d.columns} />
      <section className={s.section} aria-labelledby="source-title">
        <div className={s.sectionHeader}>
          <h2 id="source-title">Import details</h2>
          <p>Stored dataset metadata</p>
        </div>
        <dl className={s.stats}>
          <div>
            <dt>Encoding</dt>
            <dd>{clipText(d.encoding)}</dd>
          </div>
          <div>
            <dt>Delimiter</dt>
            <dd>
              {d.delimiter === "\t"
                ? "Tab"
                : d.delimiter === ","
                  ? "Comma"
                  : d.delimiter === ";"
                    ? "Semicolon"
                    : clipText(d.delimiter)}
            </dd>
          </div>
          <div>
            <dt>Total cells</dt>
            <dd>{display(d.quality.total_cells)}</dd>
          </div>
          <div>
            <dt>Source file</dt>
            <dd>{clipText(sourceName(d.filename))}</dd>
          </div>
        </dl>
      </section>
    </>
  );
}

export default function Dashboard({ datasetId }: { datasetId: string }) {
  const [state, setState] = useState<{
    data?: Dataset;
    error?: string;
    busy: boolean;
  }>({ busy: true });
  const [revision, setRevision] = useState(0);
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    getDataset(datasetId, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ data, busy: false });
      },
      (error) => {
        if (!controller.signal.aborted)
          setState({ error: errorMessage(error), busy: false });
      },
    );
    return () => controller.abort();
  }, [datasetId, revision]);
  useEffect(() => {
    if (state.error) errorRef.current?.focus();
  }, [state.error]);
  if (state.error)
    return (
      <div className={s.errorPage}>
        <p className={s.eyebrow}>Dataset unavailable</p>
        <h1>Let’s get you back to your data.</h1>
        <div className={s.error} role="alert" tabIndex={-1} ref={errorRef}>
          {state.error}
        </div>
        <div className={s.errorActions}>
          <button
            className={s.primary}
            onClick={() => {
              setState({ busy: true });
              setRevision((value) => value + 1);
            }}
          >
            Try again
          </button>
          <Link href="/" className={s.secondary}>
            Upload a CSV
          </Link>
        </div>
      </div>
    );
  if (!state.data) return <MetadataLoading />;
  return (
    <div aria-busy={state.busy}>
      <Profile
        dataset={state.data}
        refreshing={state.busy}
        refresh={() => {
          if (state.busy) return;
          setState((current) => ({ ...current, busy: true }));
          setRevision((value) => value + 1);
        }}
      />
      {state.busy ? (
        <p role="status" className={s.status}>
          Retrieving the latest metadata…
        </p>
      ) : null}
    </div>
  );
}
