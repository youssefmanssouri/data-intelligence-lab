import type { Metadata, Viewport } from "next";
import Link from "next/link";
import SidebarNav from "@/components/SidebarNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "Data Intelligence Lab",
  description: "Understand the structure and health of your CSV dataset.",
};
export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#0b0b0b",
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <a className="skip" href="#main">
          Skip to content
        </a>
        <div className="shell">
          <aside className="sidebar">
            <Link
              href="/"
              className="brand"
              aria-label="Data Intelligence Lab home"
            >
              {/* "Rising i" logo mark in its brand colours (see assets/brand/). */}
              <svg
                className="mark"
                viewBox="0 0 48 48"
                aria-hidden="true"
                focusable="false"
              >
                <circle cx="8.5" cy="38" r="5" fill="#fb923c" />
                <path
                  d="M19.5 38 24 25M33 38 41.5 11"
                  fill="none"
                  stroke="#7c3aed"
                  strokeWidth="10"
                  strokeLinecap="round"
                />
              </svg>
              <span>Data Intelligence Lab</span>
            </Link>
            <SidebarNav />
            <div className="sideFoot">
              <span>Local engine</span>
              <strong>
                <span className="dot" aria-hidden="true" />
                Local workspace
              </strong>
              <span>No account · no external analytics</span>
            </div>
          </aside>
          <div className="content">
            <main id="main" tabIndex={-1}>
              {children}
            </main>
            <footer className="footer">
              <span>Data Intelligence Lab</span>
              <span>Descriptive profiles. Transparent methods.</span>
            </footer>
          </div>
        </div>
      </body>
    </html>
  );
}
