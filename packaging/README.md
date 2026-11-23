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
│   └── node.exe                     # Bundled 64-bit Node.js standalone executable
├── runtime\
│   └── python\                      # Bundled CPython 3.12 64-bit embeddable runtime
│       ├── python.exe
│       ├── pythonw.exe
│       ├── python312._pth           # Configured with 'import site'
│       └── Lib\site-packages\       # Vendored production dependencies
├── backend\                         # FastAPI / Uvicorn backend service
├── core\                            # Analysis & execution engine
└── frontend\                        # Next.js custom server & proxy
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

All production Python dependencies are locked with full, unabbreviated SHA-256 wheel hashes in `packaging/requirements-vendor.txt`:
```powershell
pip download -r packaging/requirements-vendor.txt --require-hashes --dest <TEMP_CACHE_DIR>
```

---

## 4. Building the Standalone Runtime Bundle

The bundling script automates staging the application tree, fetching verified runtimes, and generating `manifest.json`:

```powershell
python packaging/build_runtime_bundle.py --output-dir "$env:TEMP\dil_bundle" --mode release
```

### Options:
- `--output-dir <DIR>`: Directory outside the repository to stage the bundle.
- `--mode [development|release]`: In `release` mode, enforces the CRT redistribution gate fail-closed.
- `--allow-crt-redistribution`: Authorizes packaging Microsoft Visual C++ CRT binaries.
- `--skip-download`: Uses pre-cached local runtime archives.

---

## 5. Compiling the Inno Setup Installer

Prerequisite: [Inno Setup 6](https://jrsoftware.org/isdl.php)

```powershell
ISCC.exe /DSourcePath="$env:TEMP\dil_bundle" packaging/setup.iss
```
Generated installer: `dist\DataIntelligenceLab-Setup-1.0.0.exe`

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
- Production packages vendor their own DLLs within their respective `site-packages` directories.
- Unvendored CRT DLLs placed in the application root are blocked fail-closed by `build_runtime_bundle.py` in release mode unless `--allow-crt-redistribution` is explicitly provided.
