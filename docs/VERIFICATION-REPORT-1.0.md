# Data Intelligence Lab 1.0: release verification report

**Status: DRAFT, NOT READY.** Condition 3 (clean Windows installation and execution) needs the Windows 11 VM run. No release may be published until every condition below shows *Evidenced* and the owner approves.

- **Release candidate:** `main` at `8473473`, plus the open pull request #18 (docs, version alignment and readable startup errors).
- **Supported platform:** Windows 11 x64.
- **Decisions recorded on 27 November 2026:**
  - Windows 10 is not supported.
  - No code signing.
  - The bundled runtime is CPython 3.14.10.

All evidence below comes from GitHub Actions runs on this repository. Results were read from the job logs, not inferred from green status alone. Artifacts are kept 7 to 14 days; the CRT-free PyArrow wheels are kept permanently as build-input pre-releases.

## Acceptance conditions

| # | Condition (from the v1.0 mandate) | Status | Evidence |
|---|---|---|---|
| 1 | A verified standalone runtime | **Evidenced** | Bundle run 46007600662, and every bundle built from `main` since (for example run 46016934380): `Bundle verified: 13157 files match manifest.json`, CRT gate 0 CRT binaries of 49 scanned, `EMBED_SIGSTORE: PASS` for CPython 3.14.10 (SHA-256 `90905e64…4159`), Node.js 22.23.5 SHA-256 pinned, `BUNDLE_SMOKE_TEST: PASS` with `PATH` reduced to System32 |
| 2 | A successfully compiled installer | **Evidenced** | Installer run 46007600684 and clean-machine run 46016934380 (built from `main`): Inno Setup compile; `INSTALLER_LIFECYCLE_TEST: PASS`. The kit's setup SHA-256 for `main` `8473473` is `4085d632c7d3493c173643f2b0b41beb4bee21f810c7cbf57f64b9d49aab0196` |
| 3 | Successful clean Windows installation and execution | **Pending** | CI substitute done: Server 2022 and 2025, as administrator and as a standard user (kit `RESULT: PASS (phase 2)`, run 46016934380). Still needed: the kit on a fresh **Windows 11** VM (phase 1 without VC++, phase 2 with it) plus the manual checks |
| 4 | Verified data preservation and lifecycle behaviour | **Evidenced (CI)** | Run 46016934380: `user-storage-preserved`, `datasets-preserved` and `install-refused-while-running` all PASS; reinstall removes obsolete files and keeps outside files. Reliability run 46016939702: Ctrl+C stop with exit 0, ports closed, no processes left, and the dataset served again after restart (`LIFECYCLE_TEST: PASS`, Server 2022 and 2025). Missing VC++: setup and launcher refuse safely and write no data (`MISSING_VC_RUNTIME_TEST: PASS`) |
| 5 | Passing required tests | **Evidenced** | PR #16 CI run 46013958360: Linux, Python 3.12/3.13/3.14 (3.14: 1,418 passed, 30 skipped); Windows 3.14.10: 1,448 passed; Windows 3.12 passed; frontend checks passed. Run 46016939702 on `main`: Windows 3.14.10, 1,448 passed. PR #18 adds tests; its CI results replace these once it merges |
| 6 | Accurate documentation | **Drafted, owner review needed** | [INSTALL.md](INSTALL.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [CHANGELOG.md](../CHANGELOG.md), [SECURITY.md](../SECURITY.md), [packaging/README.md](../packaging/README.md), README status. Outside this repository: the website's "Windows desktop application" wording is unreviewed |
| 7 | A complete release verification report | **This document (draft)** | Complete when condition 3 is evidenced and the release hashes are filled in |

## Supply chain

| Component | Version | Provenance |
|---|---|---|
| CPython (embeddable) | 3.14.10 | SHA-256 pinned; Sigstore signature verified against hugo@python.org in CI |
| Node.js | 22.23.5 | SHA-256 pinned against nodejs.org's SHASUMS256 |
| PyArrow (CRT-free) | 26.0.1 | Built from PGP-verified Apache Arrow source, MSVC toolset 14.44.35207 (run 45981135518); 12 PE files, 0 CRT violations; pre-release `build-input/pyarrow-26.0.1-crt-free-cp314-45981135518`, SHA-256 `c2ebaacb…7703` |
| Python closure | 18 packages | Every wheel hash-pinned (`packaging/requirements-vendor.txt`), installed offline with `--require-hashes`; `pip-audit` (PyPI advisory database): no known vulnerabilities |
| Frontend production dependencies | 21 | `npm audit --omit=dev`: 0 advisories |
| Microsoft VC++ runtime | ≥ 14.44 | Not redistributed. It is a documented prerequisite, checked by setup and the launcher, and never downloaded |

## Known limitations accepted for 1.0

These are SECURITY.md L2 to L6:
- Closing the console window stops the app abruptly.
- Same-user malware is out of scope.
- The installer is unsigned.
- There is no orphan-process guarantee beyond the Job Object.
- Datasets are not encrypted at rest.

Not yet tested:
- A real 1.0.0 to 1.0.1 upgrade.
- A machine with exactly VC++ 14.44; CI machines had 14.52.

## Release files

To be filled in at release time from the final build: the installer's SHA-256, its size, and the commit it was built from.

| File | Size | SHA-256 | Built from |
|---|---|---|---|
| `DataIntelligenceLab-Setup-1.0.0.exe` | — | — | — |

## Go / no-go

**No-go for now**, because condition 3 is pending. When the Windows 11 VM evidence passes:
1. Review this report.
2. Build the release installer from the final `main` commit.
3. Record its hash here and in CHANGELOG.md.
4. Ask the owner to approve publishing.
