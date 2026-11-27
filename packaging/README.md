# Data Intelligence Lab v1.0 — Windows Packaging and Distribution

This directory contains the production packaging architecture, standalone runtime bundling scripts, dependency locks, and Inno Setup configuration for Data Intelligence Lab v1.0 on 64-bit Windows.

---

## 1. Architecture Overview

Data Intelligence Lab v1.0 packages a completely offline, self-contained desktop application without requiring pre-installed system Python or Node.js runtimes.

### Target Installation Layout
```
%LOCALAPPDATA%\Programs\Data Intelligence Lab\
├── launcher.py                      # Unified Win32 Job Object launcher
├── manifest.current.json            # Cryptographic SHA-256 file manifest of active installation
├── manifest.previous.json           # Backup manifest for rollback/cleanup
├── node\
│   └── node.exe                     # Bundled Node.js 22.23.5 (win-x64), SHA-256 pinned
├── runtime\
│   └── python\                      # Bundled CPython 3.14 64-bit embeddable runtime
│       ├── python.exe
│       ├── pythonw.exe
│       ├── python314._pth           # sys.path: stdlib zip, runtime, Lib\site-packages, bundle root
│       └── Lib\site-packages\       # Vendored production dependencies
├── backend\                         # FastAPI / Uvicorn backend service
├── core\                            # Analysis & execution engine
└── frontend\                        # Built .next output, custom server & proxy, production node_modules
```

---

## 2. Storage Safety & Preservation Guarantee

- **Authoritative Data Directory:**
  `%USERPROFILE%\.data-intelligence-lab\storage`
- **Zero Ingestion Fragmentation:**
  Application code, configuration, and defaults on Windows resolve strictly to `Path.home() / ".data-intelligence-lab" / "storage"`.
- **Zero Automatic Migrations:**
  Existing datasets are never moved, copied, or modified without explicit user authorization.
- **Uninstaller Safety:**
  The Inno Setup uninstaller explicitly purges only program files under `{app}`. User datasets under `%USERPROFILE%\.data-intelligence-lab\storage` are permanently preserved.

---

## 3. Dependency Vendoring & Cryptographic Integrity

`packaging/requirements-vendor.txt` locks the production closure for CPython 3.14 on Windows x64: 18 packages, each pinned to the SHA-256 of the one wheel used. It contains what production code imports (FastAPI, Starlette, Uvicorn, python-multipart, PyArrow), their required dependencies, and `tzdata`: Windows has no OS time zone database, and reading timezone-aware Parquet data (stored as `"UTC"` for a zero offset) resolves the zone through Python's `zoneinfo`. NumPy and pandas are not used and are not vendored.

**PyArrow is not taken from PyPI.** The PyPI wheel bundles `pyarrow/msvcp140.dll` (Microsoft CRT). The lock pins the source-built, CRT-free wheel instead (PyArrow 26.0.1, SHA-256 `c2ebaacbf888bafdba0eb052266fcf9107b5f62f7f4e6e488c1a7f4756847703`), built with MSVC toolset 14.44 by the `pyarrow26-crt-free-cp314` workflow (run 45981135518) and published with its evidence as the pre-release `build-input/pyarrow-26.0.1-crt-free-cp314-45981135518`. Download that wheel (the repository is private, so use an authenticated browser or `gh release download`), then build the wheelhouse:

```powershell
python packaging/prepare_wheelhouse.py --pyarrow-wheel <path>\pyarrow-26.0.1-cp314-cp314-win_amd64.whl --output-dir <EMPTY_DIR>
```

The script fails closed. It downloads the other 17 wheels from PyPI with `--require-hashes`, copies PyArrow only from the local file after checking its hash against the lock, resolves the whole lock offline from the wheelhouse (`--no-index --require-hashes`) to prove the closure is complete, rejects any wheel that bundles a Microsoft CRT file, and writes `wheelhouse-manifest.json`.

The bundle therefore ships no Microsoft CRT: the target machine needs the Microsoft Visual C++ 2015-2022 Redistributable (x64), version 14.44 or later, which the app uses but never downloads or installs.

---

## 4. Building the Standalone Runtime Bundle

Prerequisites on the build machine: CPython 3.14 (it compiles the bundle's bytecode, so it must match the bundled runtime's version), Node.js 22 with npm, the wheelhouse from section 3, and a production frontend build:

```powershell
cd frontend; npm ci; npm run build; cd ..
python packaging/build_runtime_bundle.py --mode release --wheelhouse <WHEELHOUSE_DIR> --output-dir "$env:TEMP\dil_bundle"
python packaging/build_runtime_bundle.py --verify "$env:TEMP\dil_bundle"
```

Release mode fails closed. It:
- downloads (or reuses from `--cache-dir`) Node.js 22.23.5 `node.exe` and the CPython 3.14.10 embeddable package, each checked against a pinned SHA-256 (the bundle CI also verifies the embeddable package's Sigstore signature from python.org); a local `node\node.exe` is used only if it matches the pin, and nothing is ever replaced by a placeholder;
- extracts the embeddable package **without** `vcruntime140.dll` and `vcruntime140_1.dll` (see section 7) and writes `python314._pth` so `python -m backend.main` resolves the bundle root and `Lib\site-packages`;
- checks the wheelhouse against `wheelhouse-manifest.json` and the current lock, then installs it offline (`--no-index --require-hashes`, cp314-win_amd64) into `runtime\python\Lib\site-packages`;
- stages the frontend's built `.next` output (without build caches and traces), `config`, `scripts`, `next.config.mjs` and `package*.json`, then runs `npm ci --omit=dev --ignore-scripts --os=win32 --cpu=x64` there;
- runs the CRT gate over every PE image (`.dll`, `.pyd`, `.node`, `.exe`) and writes `manifest.json` (size and SHA-256 of every file, plus runtime versions, the frontend BUILD_ID and the vendored wheel hashes).

`--verify <BUNDLE_DIR>` re-checks an existing bundle independently: every file on disk must be listed in `manifest.json` with a matching size and SHA-256, and nothing unlisted may be present.

### Options:
- `--mode [development|release]`: `release` (default) applies every rule above; `development` may stage placeholders for inspection only.
- `--wheelhouse <DIR>`: Verified wheelhouse from `prepare_wheelhouse.py`; required in release mode.
- `--output-dir <DIR>`: Directory outside the repository to stage the bundle (replaced if it exists).
- `--cache-dir <DIR>`: Cache for the downloaded runtimes (re-verified on every use).
- `--skip-download`: Development mode only; stages placeholder runtimes.
- `--allow-crt-redistribution` / `--crt-authorization-file`: Not used for Data Intelligence Lab releases; redistribution is not authorized.

---

## 5. Compiling the Inno Setup Installer

Prerequisite: [Inno Setup 6.3 or later](https://jrsoftware.org/isdl.php) (for the `x64compatible` architecture identifier).

```powershell
ISCC.exe /DBundleDir="$env:TEMP\dil_bundle" /DOutputDir="$env:TEMP\dil_dist" packaging\setup.iss
```
Generated installer: `<OutputDir>\DataIntelligenceLab-Setup-1.1.0.exe` (`OutputDir` defaults to `dist\` at the repository root). `BundleDir` is required and must be a bundle that passed `build_runtime_bundle.py --verify`.

What the installer does:
- **Per-user, no administrator rights:** installs to `%LOCALAPPDATA%\Programs\Data Intelligence Lab`. Supported for v1.0: 64-bit **Windows 11**; CI also verifies Windows Server 2022 and 2025. **Windows 10 is not supported** (Microsoft ended its support in October 2025): setup and the launcher still accept Windows 10 version 1703 (build 15063) or later, the technical minimum for the launcher's Job Object handling, but no Windows 10 testing or fixes are offered.
- **Prerequisite check:** before showing any page, it requires the Microsoft Visual C++ 2015-2022 Redistributable (x64), 14.44 or later. It reads the registration Microsoft documents (`HKLM\SOFTWARE[\WOW6432Node]\Microsoft\VisualStudio\14.0\VC\Runtimes\x64`), and if the runtime is missing or older it stops with Microsoft's download link. It never downloads or installs the runtime. The launcher repeats the check at every start.
- **Running-instance check:** before replacing files (and before uninstalling), it refuses to continue while any `python.exe`, `pythonw.exe` or `node.exe` runs from the install folder, or while process enumeration fails. Processes the current account is not allowed to query (other accounts', or elevated ones) are logged and ignored, because they cannot be running from this user's per-user install folder; otherwise a standard user would be blocked on any machine where another account runs Python or Node.js.
- **Shortcuts:** Start menu (and optionally desktop) shortcuts start `runtime\python\python.exe launcher.py` in a console window that shows the local URL; closing that window (or pressing Ctrl+C) stops the application.
- **Upgrades:** the bundle's `manifest.json` is promoted to `manifest.current.json`, the previous one is kept as `manifest.previous.json`, and files listed only in the previous manifest are deleted (paths are confined to the install folder).
- **Uninstall:** removes the installed files, the manifests and the Next.js route cache. It never touches `%USERPROFILE%\.data-intelligence-lab\storage`.

Silent installation (for managed deployment): `DataIntelligenceLab-Setup-1.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`. In silent mode every blocking dialog defaults to Cancel, so a missing prerequisite or a running instance ends setup with a non-zero exit code instead of waiting for input.

---

## 6. Process Lifecycle & Graceful Shutdown

1. **Job Object Containment:**
   All child processes (backend and frontend) are created inside an anonymous Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
2. **Dedicated Stdin Pipe:**
   The frontend is spawned with an inherited Win32 anonymous pipe on `stdin`.
3. **Graceful Sequence:**
   On launcher shutdown or console close, the launcher closes `stdin_write`, signaling EOF. The Next.js frontend catches EOF and runs:
   - `server.close()` (stops accepting new connections)
   - 2.0s grace period for in-flight requests, then `server.closeAllConnections()`
   - `await app.close()` (cleanly shuts down Next.js)
   - Removes port file and exits with code 0.
4. **Emergency Timeout Backstop:**
   If a process fails to exit within 5.0s, `TerminateProcess` is invoked, followed by kernel Job Object closure.

---

## 7. Microsoft CRT Legal Redistribution Restriction

Redistributing Microsoft Visual C++ Runtime binaries (`msvcp140.dll`, `vcruntime140.dll`, etc.) requires adherence to Microsoft Visual Studio licensing terms and `REDIST.TXT`.

Data Intelligence Lab packaging enforces this rule:
- No vendored wheel bundles a Microsoft CRT file: `prepare_wheelhouse.py` rejects any that does, and PyArrow is the source-built CRT-free wheel (see section 3). Binaries load the system Visual C++ runtime instead.
- The CPython embeddable package itself ships `vcruntime140.dll` and `vcruntime140_1.dll`; `build_runtime_bundle.py` never extracts them, records them in `manifest.json` as excluded, and the CRT gate would block them if they reappeared. `python.exe` then loads them from the installed Visual C++ Redistributable.
- `node.exe` links its C runtime statically, and the frontend's native addons (`@next/swc-win32-x64-msvc`, `@img/sharp-win32-x64`) ship no Microsoft CRT file; they import only the Universal CRT (`api-ms-win-crt-*`), which is part of Windows 10/11.
- Unvendored CRT DLLs placed in the application root are blocked fail-closed by `build_runtime_bundle.py` in release mode unless `--allow-crt-redistribution` is explicitly provided.

---

## 8. Release build

Releases are built by `.github/workflows/release-windows.yml`, which is run by hand: Actions → *Release build on Windows (v1.0)* → *Run workflow* on `main`, with `version` set to the value in `backend/version.py`. It:

- refuses to start without a `LICENSE` file at the repository root (the bundle ships it), if the version differs from `backend/version.py`, or if the tag `v<version>` already exists;
- builds the wheelhouse, frontend, bundle and installer with the same pinned steps as the clean-machine workflow, re-verifies the bundle, smoke-tests it, and re-runs the full installer lifecycle test on the built installer;
- uploads `dil-release-<version>-<commit>`, which holds the installer, `bundle-manifest.json`, `SHA256SUMS.txt`, `release-info.json` (size, SHA-256, commit, run) and the lifecycle evidence.

With `create_draft` set, it also creates a **draft** GitHub release, with the CHANGELOG section as the notes and every file attached. With `reuse_run_id` it skips the build and drafts the files of that earlier run, so the published installer is exactly the one whose hash was recorded. It refuses if `CHANGELOG.md` does not contain that hash. A draft is visible only to maintainers. Publishing it is a separate, deliberate owner action.
