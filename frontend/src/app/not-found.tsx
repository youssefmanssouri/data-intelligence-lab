import Link from "next/link";
import s from "@/components/workspace.module.css";
export default function NotFound() {
  return (
    <div className={s.errorPage}>
      <p className={s.eyebrow}>404 / Page not found</p>
      <h1>This page isn’t in the workspace.</h1>
      <p>Check the dataset link or start with a CSV.</p>
      <div className={s.errorActions}>
        <Link className={s.primary} href="/">
          Upload a CSV
        </Link>
      </div>
    </div>
  );
}
