# Data Intelligence Lab 1.0: release verification report

**Status: READY FOR OWNER APPROVAL.** Six conditions are evidenced. Condition 3 is an **accepted exception**: the owner decided on 27 November 2026 to release without a Windows 11 client run, so it is verified on Windows Server only. The release installer was built and re-tested by release run 46029439983 from `main` `7443cbc`; its hash is recorded below. Publishing requires the owner's explicit approval.

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
| 3 | Successful clean Windows installation and execution | **Accepted exception (owner decision, 27 Nov 2026)** | Verified on Windows Server 2022 and 2025 only, as administrator and as a standard user: kit `RESULT: PASS (phase 1 and 2)` and installer lifecycle PASS (run 46022914900, `main` `705f8c1`). **Not tested on Windows 11 client.** Not covered: SmartScreen and consumer Defender behaviour, a Start menu launch by hand, a machine with nothing preinstalled (the CI runners have Python, Node.js and VC++ files; only the VC++ registration was removed), and a restart on a real user profile |
| 4 | Verified data preservation and lifecycle behaviour | **Evidenced (CI)** | Run 46016934380: `user-storage-preserved`, `datasets-preserved` and `install-refused-while-running` all PASS; reinstall removes obsolete files and keeps outside files. Reliability run 46016939702: Ctrl+C stop with exit 0, ports closed, no processes left, and the dataset served again after restart (`LIFECYCLE_TEST: PASS`, Server 2022 and 2025). Missing VC++: setup and launcher refuse safely and write no data (`MISSING_VC_RUNTIME_TEST: PASS`) |
| 5 | Passing required tests | **Evidenced** | PR #16 CI run 46013958360: Linux, Python 3.12/3.13/3.14 (3.14: 1,418 passed, 30 skipped); Windows 3.14.10: 1,448 passed; Windows 3.12 passed; frontend checks passed. Run 46016939702 on `main`: Windows 3.14.10, 1,448 passed. PR #18 adds tests; its CI results replace these once it merges |
| 6 | Accurate documentation | **Drafted, owner review needed** | [INSTALL.md](INSTALL.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md), [CHANGELOG.md](../CHANGELOG.md), [SECURITY.md](../SECURITY.md), [packaging/README.md](../packaging/README.md), README status. Outside this repository: the website's "Windows desktop application" wording is unreviewed |
| 7 | A complete release verification report | **This document (draft)** | Complete when the release installer's hash, size and commit are filled in |

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

To be filled in at release time from the final build, which is made by `.github/workflows/release-windows.yml`. That workflow builds from the chosen ref, re-tests the installer, and writes `SHA256SUMS.txt` and `release-info.json`: the installer's SHA-256, its size and the commit it was built from. It refuses to run without a `LICENSE`. At most it creates a draft release; publishing is a separate step that needs the owner's approval.

| File | Size | SHA-256 | Built from |
|---|---|---|---|
| `DataIntelligenceLab-Setup-1.0.0.exe` | 102,329,462 bytes | `4d1b26af4cc61532a2b008c4dcd9b381f8d0c3aa6e4b05291cd85e68208e1e35` | `main` `7443cbc` (release run 46029439983) |

## Go / no-go

**Go, subject to the owner's explicit approval to publish.** Release run 46029439983 (`main` `7443cbc`) passed every step:
- preflight: `LICENSE` present, version 1.0.0, no existing tag;
- `Bundle verified: 13162 files match manifest.json`, 0 CRT binaries, `BUNDLE_SMOKE_TEST: PASS`;
- `INSTALLER_LIFECYCLE_TEST: PASS` on the built installer, including `user-storage-preserved`.

Its artifact `dil-release-1.0.0-7443cbc2b68c26546d0d376ed9132af8d5e7b4f4` holds the installer, `SHA256SUMS.txt`, `release-info.json` and the evidence. Commits after `7443cbc` that change only documentation do not change the installer, which ships none of these files.

To publish, once the owner approves:
1. Run the release workflow on `main` with `version` 1.0.0, `create_draft` set and `reuse_run_id` 46029439983. That drafts exactly this installer, without rebuilding: it re-checks `SHA256SUMS.txt` and refuses if CHANGELOG.md does not record this installer's hash.
2. Check the draft.
3. Publish it.
