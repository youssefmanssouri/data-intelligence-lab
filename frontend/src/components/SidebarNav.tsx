"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

// Stroke icons drawn inline so the app loads no icon font or external asset.
function Icon({ children }: { children: ReactNode }) {
  return (
    <span className="navIcon" aria-hidden="true">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        focusable="false"
      >
        {children}
      </svg>
    </span>
  );
}

const icons = {
  upload: (
    <Icon>
      <path d="M12 16V4M7 9l5-5 5 5M4 20h16" />
    </Icon>
  ),
  overview: (
    <Icon>
      <rect x="3" y="3" width="7" height="7" rx="1.5" />
      <rect x="14" y="3" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </Icon>
  ),
  score: (
    <Icon>
      <path d="M21 12a9 9 0 1 1-9-9v9z" />
    </Icon>
  ),
  notes: (
    <Icon>
      <path d="M12 8v5M12 16.5v.5M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    </Icon>
  ),
  columns: (
    <Icon>
      <path d="M4 6h16M4 12h16M4 18h16" />
    </Icon>
  ),
  details: (
    <Icon>
      <path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18" />
    </Icon>
  ),
};

export default function SidebarNav() {
  const pathname = usePathname();
  const onDataset = pathname.startsWith("/datasets/");
  return (
    <nav aria-label="Workspace">
      <p className="navGroup">Workspace</p>
      <ul className="navList">
        <li>
          <Link
            className="navLink"
            href="/"
            aria-current={pathname === "/" ? "page" : undefined}
          >
            {icons.upload}
            Upload CSV
          </Link>
        </li>
        {onDataset ? (
          <li>
            <a className="navLink" href="#overview" aria-current="page">
              {icons.overview}
              Health overview
            </a>
          </li>
        ) : null}
      </ul>
      {onDataset ? (
        <>
          <p className="navGroup">This dataset</p>
          <ul className="navList">
            <li>
              <a className="navLink" href="#quality-title">
                {icons.score}
                Score breakdown
              </a>
            </li>
            <li>
              <a className="navLink" href="#warnings-title">
                {icons.notes}
                Quality notes
              </a>
            </li>
            <li>
              <a className="navLink" href="#columns-title">
                {icons.columns}
                Column profiles
              </a>
            </li>
            <li>
              <a className="navLink" href="#source-title">
                {icons.details}
                Import details
              </a>
            </li>
          </ul>
        </>
      ) : null}
    </nav>
  );
}
