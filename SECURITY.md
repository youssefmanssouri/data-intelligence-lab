# Security Policy: Data Intelligence Lab

**Status:** v1.0 release candidate. The controls below are implemented and covered by tests unless marked otherwise. This page states what is verified, what is accepted as a limitation, and what is still open. It is not a compliance certification.

---

## 1. Reporting security vulnerabilities

Report suspected vulnerabilities privately through **GitHub Security Advisories** on this repository. If that is unavailable, contact the maintainer through their GitHub profile; do not put exploit details or private data in a public issue. Please allow time for an assessment before any public disclosure.

---

## 2. Threat model (v1.0, local single-user desktop app)

Data Intelligence Lab runs entirely on the user's Windows PC. A launcher starts two local services: a FastAPI backend and a Next.js frontend, both bound to random loopback ports. The user's browser talks only to the frontend, which proxies API calls to the backend.

| Asset | Where it lives |
|---|---|
| Uploaded datasets (Parquet + metadata JSON) | `%USERPROFILE%\.data-intelligence-lab\storage` |
| Application files | `%LOCALAPPDATA%\Programs\Data Intelligence Lab` (per-user install) |

In scope: **web pages the user visits** (they can reach loopback ports from the browser), **malformed or hostile CSV files** the user uploads, and **damaged or tampered installations**. Out of scope: malware already running as the same Windows user (it can read the user's files directly), other administrators on the machine, and network attackers (nothing listens on a network interface).

---

## 3. Implemented controls

| Control | What it does | Evidence |
|---|---|---|
| Loopback-only binding | Backend and frontend bind to `127.0.0.1` only; any other host is refused before the server starts. | `backend/config.py` `validate_host`; `tests/integration/test_api_boundary.py` |
| Backend request guard | Every backend request needs exactly one loopback `Host` (defeats DNS rebinding), a `Sec-Fetch-Site` of `same-origin`/`none` if sent, and a loopback `http://` `Origin` if sent. Others get `403 ERR_FORBIDDEN_REQUEST` before routing or body intake. | `backend/request_guard.py`; `tests/integration/test_request_guard.py` |
| Frontend inbound policy | The frontend accepts only its own `127.0.0.1`/`localhost` authority and same-origin uploads, and proxies only a fixed allow-list of API paths to a fixed backend origin. | `frontend/config/proxy.mjs`; `frontend/tests/security.test.mjs`, `proxy.test.mjs` |
| Upload limits | 200 MB request budget enforced from `Content-Length` and while streaming; 1,000,000 rows, 1,000 columns, 32 KB per cell, 30 s processing time. | `backend/app.py` `BoundedUploadMiddleware`; `core/types.py` `IngestionLimits` |
| Safe identifiers and paths | Dataset IDs are validated against path traversal and reserved names; upload file names are reduced to a base name; storage paths are bounds-checked. | `backend/adapters/local_storage.py` |
| Error hygiene | Errors are RFC 7807 problems with fixed texts; exception messages and file system paths are never returned. | `backend/errors.py`; boundary tests |
| Atomic, durable publication | Each dataset is written to a private staging directory, both files are fsynced, then the directory is renamed into place; a crash leaves either the old state or the complete dataset. Abandoned staging (a killed write) is swept after 24 hours. | `backend/adapters/local_storage.py`; `tests/unit/test_storage_durability.py` |
| Process containment | Both services run in a Win32 Job Object with kill-on-close: if the launcher ends for any reason, Windows ends the services. | `launcher.py`; CI smoke test `job-object-teardown` |
| Integrity of the shipped files | Every file of the bundle is listed in `manifest.json` with its SHA-256; runtimes are hash-pinned (CPython embeddable, Node.js); the Python closure is installed offline with `--require-hashes`. | `packaging/build_runtime_bundle.py` (`--verify`) |
| No PATH fallbacks in installed copies | The launcher uses the bundled Python (`sys.executable`) and refuses to start if the bundled `node.exe` is missing, instead of running a `node.exe` found on `PATH`. | `launcher.py` `validate_paths`; `tests/unit/test_launcher.py` |
| No Microsoft CRT redistribution | The bundle ships no `msvcp140`/`vcruntime140` files; setup and the launcher require the Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44+ and never download it. | `packaging/README.md` §7; installer CI |
| Per-user, no elevation | The installer needs no administrator rights and never touches the user's dataset folder on uninstall. | `packaging/setup.iss`; installer and clean-machine CI |
| No telemetry | No dataset content, metadata or diagnostics leave the machine; Next.js telemetry is disabled. | `frontend/scripts/server.mjs`, `launcher.py` |

---

## 4. Dependency status (27 November 2026)

| Component | Version | Known advisories | Assessment |
|---|---|---|---|
| Python closure (18 packages, `packaging/requirements-vendor.txt`, cp314-win_amd64) | as locked; PyArrow 26.0.1 (source-built, CRT-free) | 0 (`pip-audit`, PyPI advisory database) | CVE-2026-25087 (GHSA-rgxp-2hwp-jwgg, fixed in PyArrow 23.0.1) no longer applies: the bundle ships 26.0.1. The OSV database was not queried for this check. |
| Frontend production dependencies (21) | as locked | 0 (`npm audit --omit=dev`) | — |
| Frontend development tools | as locked | 7 moderate | Build-time only; not shipped. |
| Node.js runtime | 22.23.5 (security release, 11 Nov 2026) | none known | Maintenance LTS until April 2027. |
| CPython runtime | 3.14.10 embeddable (latest 3.14 release on python.org; SHA-256 pinned, Sigstore signature verified in CI) | none known | Python 3.14 receives bug-fix releases with Windows embeddable packages; update with each release. |

---

## 5. Accepted limitations and open items

| # | Limitation | Why / mitigation | Status |
|---|---|---|---|
| L1 | ~~The bundled CPython 3.12.10 lacked later security fixes~~ (3.12 has no Windows embeddable package after 3.12.10). | Resolved by moving the bundle to CPython 3.14.10 with a CRT-free PyArrow 26.0.1 build for cp314 (pre-release `build-input/pyarrow-26.0.1-crt-free-cp314-45981135518`); verified by the bundle, installer, reliability and clean-machine CI runs of 27 November 2026. Residual duty: rebuild with each CPython 3.14 bug-fix release. | **Closed** (27 Nov 2026). |
| L2 | **Closing the console window ends the services abruptly.** Windows gives a closing console only seconds; the launcher's orderly shutdown runs on Ctrl+C, and on window close the Job Object ends the services. | Dataset writes are atomic and durable, so no partial dataset appears; abandoned staging is swept. The launcher's temporary port files in `%TEMP%` may remain (a few bytes each). | Accepted. |
| L3 | **Another program running as the same user can impersonate a service** (for example by writing a port file first or binding a port). | Same-user malware is out of scope; it can read the dataset folder directly anyway. | Accepted. |
| L4 | **The installer is not code-signed.** SmartScreen warns on first run. | Signing is planned for release readiness (Milestone F). | Open. |
| L5 | **No orphan-process guarantee beyond the Job Object.** If Windows fails to apply kill-on-close (not observed in testing), services could outlive the launcher. | CI checks that both ports close and no process remains after the launcher is killed or stopped. | Accepted; no zero-orphan claim is made. |
| L6 | **Datasets are not encrypted at rest.** | They are protected by the Windows account's file permissions, like the user's other documents. | Accepted for v1.0. |

---

## 6. Zero secrets policy

The application needs no secrets. Never commit passwords, API keys, credentials or private certificates; configuration is supplied through environment variables (`HOST`, `PORT`, `STORAGE_DIR`, `MAX_UPLOAD_BYTES`, `ENVIRONMENT`).
