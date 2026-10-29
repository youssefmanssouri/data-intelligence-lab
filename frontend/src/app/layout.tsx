import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Data Intelligence Lab",
  description: "Understand the structure and health of your CSV dataset.",
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
        <header className="masthead">
          <Link
            href="/"
            className="brand"
            aria-label="Data Intelligence Lab home"
          >
            <span className="mark" aria-hidden="true">
              di<span>l</span>
            </span>
            <span>
              Data Intelligence Lab
              <span className="brandSub">Clarity starts with your data.</span>
            </span>
          </Link>
          <span className="local">
            <span aria-hidden="true">●</span> Local workspace
          </span>
        </header>
        <main id="main" tabIndex={-1}>
          {children}
        </main>
        <footer className="footer">
          <span>Data Intelligence Lab</span>
          <span>Descriptive profiles. Transparent methods.</span>
        </footer>
      </body>
    </html>
  );
}
