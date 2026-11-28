# Data Intelligence Lab 1.1: release verification report

**Status: verified; draft and publication follow this report.** 1.1.0 adds rule-based, deterministic insights ("What this data says"). Everything else (platform, runtimes, security controls, known limitations) is unchanged from 1.0.0; see [VERIFICATION-REPORT-1.0.md](VERIFICATION-REPORT-1.0.md) for those.

All evidence below was read from the job logs, not inferred from green status.

## Release build

Release run 46088895257, built from `main` at `03b1263` (merge of pull request #23), on Windows Server 2022.

| Check | Result |
|---|---|
| Preflight | `LICENSE` present, version 1.1.0 equals `backend/version.py`, no existing `v1.1.0` tag |
| Bundle | `Bundle verified: 13164 files match manifest.json`, no CRT files, CPython 3.14.10, Node.js v22.23.5, PyArrow 26.0.1; `BUNDLE_SMOKE_TEST: PASS` |
| Installer lifecycle | `INSTALLER_LIFECYCLE_TEST: PASS`: install, installed smoke test, install refused while running, reinstall with obsolete-file cleanup, uninstall, `user-storage-preserved` |
| **Upgrade from the published 1.0.0** (new) | `UPGRADE_TEST: PASS`, details below |

### Upgrade test

`packaging/test_upgrade.py`, run by the release workflow on the exact installer above:

1. It downloads `DataIntelligenceLab-Setup-1.0.0.exe` from the v1.0.0 release and checks its SHA-256 (`4d1b26af…1e35`).
2. It installs 1.0.0; Windows reports DisplayVersion 1.0.0. The 1.0.0 app accepts an upload through its proxy (HTTP 201) and stores the dataset.
3. It upgrades in place with the 1.1.0 installer: exit 0, DisplayVersion 1.1.0, 13,164 installed files match the new manifest, and the old manifest is kept as `manifest.previous.json`.
4. The upgraded app serves the 1.0.0 dataset (HTTP 200) and computes insights for it (HTTP 200: `CONSTANT_COLUMN`, `IDENTIFIER_COLUMN`, `MISSING_VALUES`, `OUTLIERS`).
5. Its `data.parquet` and `metadata.json` are byte-identical before and after the upgrade: insights are not written to disk.
6. Uninstalling keeps the dataset.

The same test also passed on the pull-request build (run 46038413560).

## Tests

| Suite | Where | Result |
|---|---|---|
| Backend (pytest) | CI, PR #23 head `4803cd0`, Windows, Python 3.14.10 | 1,488 passed |
| Backend (pytest) | Local, Linux, Python 3.13 | 1,456 passed, 32 skipped |
| Frontend lint, typecheck, build, proxy and security | CI, PR #23 | pass |
| UI (mocked API) | Local | 40 passed (4 new for insights) |
| End to end against the real backend | CI and local | 7 passed (1 new for insights) |

The insights rules have 25 unit tests and 6 API tests. The API tests cover determinism across app instances, that nothing is persisted, missing and invalid ids, cache invalidation on delete, and sampling.

## Release files

| File | Size | SHA-256 | Built from |
|---|---|---|---|
| `DataIntelligenceLab-Setup-1.1.0.exe` | 102,351,042 bytes | `aa575b6810664c9d3b25e906ebd42562d64b981fa02b7aa214e6e0a3d5ad81b5` | `main` `03b1263` (release run 46088895257) |

## Not verified

- **Windows 11 client:** accepted exception, as for 1.0.0.
- **Windows Server 2025:** this release was built and tested on Server 2022 only.
- **Quality of the insights on real-world files:** checked on synthetic data only. Testing on a realistic synthetic sales file found three false findings, which were fixed before release (see [INSIGHTS.md](INSIGHTS.md)). Other kinds of files may expose further cases.
