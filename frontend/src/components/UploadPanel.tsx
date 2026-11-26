"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import {
  datasetPath,
  errorMessage,
  fileError,
  sourceName,
  uploadDataset,
} from "@/services/api";
import s from "./workspace.module.css";

export default function UploadPanel() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);
  function select(next: File | null) {
    if (pending.current) return;
    setFile(next);
    setError(next ? (fileError(next) ?? "") : "");
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending.current) return;
    const validation = fileError(file);
    if (validation || !file) {
      setError(validation ?? "Choose a CSV file first.");
      return;
    }
    pending.current = true;
    setBusy(true);
    setError("");
    controller.current = new AbortController();
    try {
      const summary = await uploadDataset(file, controller.current.signal);
      router.push(datasetPath(summary.dataset_id));
      // Keep submission locked until navigation unmounts the form.
    } catch (e) {
      if (!controller.current.signal.aborted) {
        setError(errorMessage(e));
        pending.current = false;
        setBusy(false);
      }
    }
  }
  return (
    <form
      onSubmit={submit}
      className={`${s.card} ${s.panel}`}
      aria-busy={busy}
      noValidate
    >
      <div className={s.cardHead}>
        <h2>Import a dataset</h2>
        <span className={s.pill}>CSV · below 200 MiB</span>
      </div>
      <label
        className={s.drop}
        htmlFor="csv-file"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          if (busy) return;
          if (event.dataTransfer.files.length !== 1) {
            setFile(null);
            setError("Choose one CSV file at a time.");
            return;
          }
          select(event.dataTransfer.files[0]);
        }}
      >
        <span className={s.dropIcon} aria-hidden="true">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            focusable="false"
          >
            <path d="M12 16V4M7 9l5-5 5 5M4 20h16" />
          </svg>
        </span>
        <span className={s.dropTitle}>Bring your data into focus.</span>
        <span className={s.help} id="file-help">
          Choose or drop one CSV. A header row is required.
        </span>
        <input
          className={s.file}
          id="csv-file"
          name="file"
          type="file"
          accept=".csv,text/csv"
          disabled={busy}
          aria-describedby="file-help file-limit"
          onChange={(event) => select(event.target.files?.[0] ?? null)}
        />
      </label>
      <p className={s.help} id="file-limit">
        Below 200 MiB. The backend enforces the configured limit, including
        upload framing.
      </p>
      {file ? (
        <p className={s.selected}>
          Selected: <b>{sourceName(file.name)}</b>
        </p>
      ) : null}
      {error ? (
        <div ref={errorRef} tabIndex={-1} role="alert" className={s.error}>
          <p>{error}</p>
          <p>
            Choose another file or try again. Uploads are never retried
            automatically.
          </p>
        </div>
      ) : null}
      <div className={s.submitRow}>
        <button
          className={`${s.primary} ${s.full}`}
          type="submit"
          disabled={busy || !!(file && fileError(file))}
        >
          {busy ? "Uploading & profiling…" : "Create data profile"}
          <span aria-hidden="true">↗</span>
        </button>
      </div>
      {busy ? (
        <div role="status" className={s.status}>
          <p>Uploading and analysing your CSV. Please keep this page open.</p>
          <progress aria-label="Upload and profiling in progress" />
        </div>
      ) : null}
      <p className={s.privacy}>
        Your CSV is processed by your configured local backend. This dashboard
        displays aggregate profiles, without row previews.
      </p>
    </form>
  );
}
