"use client";
import Link from "next/link";
import s from "@/components/workspace.module.css";
export default function ErrorBoundary({ reset }: { reset: () => void }) {
  return (
    <div className={s.errorPage}>
      <h1>The workspace could not load.</h1>
      <p role="alert">
        An unexpected error occurred. Reload the workspace or return to the
        upload screen.
      </p>
      <div className={s.errorActions}>
        <button className={s.primary} onClick={reset}>
          Try again
        </button>
        <Link className={s.secondary} href="/">
          Upload a CSV
        </Link>
      </div>
    </div>
  );
}
