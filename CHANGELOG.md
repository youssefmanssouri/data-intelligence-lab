# Release notes

## 1.0.0 — release candidate (built 27 November 2026, not yet published)

The first release of Data Intelligence Lab: a local-first Windows application that turns CSV files into profiled, quality-scored datasets without sending anything off your PC.

### What it does
- **CSV import** with strict parsing: encoding and delimiter detection, header validation, and explicit type inference.
- **Data Health Dashboard:** dataset totals, missing values by column, and a health score split by its 60/30/10 weights, with quality notes.
- **Column profiles** with type-specific statistics.
- **Local storage:** each dataset is saved as Parquet with its metadata, written atomically and durably, in `%USERPROFILE%\.data-intelligence-lab\storage`.

### Platform and requirements
- Windows 11, 64-bit. Windows 10 is not supported.
- Microsoft Visual C++ 2015-2022 Redistributable (x64) 14.44 or later, installed by the user. Setup and the app check for it and never download it.
- Per-user install, no administrator rights. Bundled runtimes: CPython 3.14.10 and Node.js 22.23.5.

### Security and privacy
- No telemetry. Both services listen only on `127.0.0.1`, on random ports.
- The backend refuses DNS-rebinding, cross-site and foreign-origin requests. Uploads are bounded: 200 MB, 1,000,000 rows, 1,000 columns, 32 KB per cell, 30 s of processing.
- Every installed file is listed with its SHA-256 in `manifest.json`.
- No Microsoft C runtime files are redistributed. PyArrow 26.0.1 is a source build that bundles none.
- Dependency audit: no known vulnerabilities in the Python closure (PyPI advisory database) and 0 production npm advisories.

### Known limitations
- **The installer is not code-signed.** SmartScreen warns on first run; verify the SHA-256 below before running it.
- **Closing the console window stops the app abruptly.** Saved datasets are safe because writes are atomic, but use Ctrl+C for an orderly stop.
- **Datasets are not encrypted at rest.** They are protected by your Windows account's file permissions.
- **Not tested on Windows 11 itself:** verified on Windows Server 2022 and 2025 only (see Verification).
- **Upgrade from an earlier version is untested:** 1.0.0 is the first release, so only reinstalling over 1.0.0 has been tested.

The full list is in [SECURITY.md](SECURITY.md), section 5.

### Verification
Tested in Windows CI on Server 2022 and 2025, as administrator and as a standard user: install, reinstall, uninstall, a missing VC++ runtime, Ctrl+C shutdown and restart. **Not tested on a Windows 11 client machine**: Windows 11 is the supported platform, but this release was verified on Windows Server 2022 and 2025 only. SmartScreen, Defender and a manual Start menu launch on Windows 11 have not been exercised. Please report any problem.

### Release files
| File | SHA-256 |
|---|---|
| `DataIntelligenceLab-Setup-1.0.0.exe` (102,329,462 bytes) | `4d1b26af4cc61532a2b008c4dcd9b381f8d0c3aa6e4b05291cd85e68208e1e35` |

See [Installation](docs/INSTALL.md) and [Troubleshooting](docs/TROUBLESHOOTING.md).
