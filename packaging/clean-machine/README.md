# Data Intelligence Lab: clean Windows 11 verification kit

This kit checks the installer the way a real user meets it: on a fresh Windows machine, with a normal (non-administrator) account and nothing else installed. Your datasets are never sent anywhere. The evidence file records checks, versions and paths only.

## What is in the kit

| File | Purpose |
|---|---|
| `DataIntelligenceLab-Setup-1.1.0.exe` | The installer under test (unsigned build) |
| `SHA256SUMS` | Hash of the installer, checked by the script |
| `Verify-DataIntelligenceLab.ps1` | The verification script |
| `smoke_test_bundle.py`, `build_runtime_bundle.py` | Automated checks, run with the app's own bundled Python after installation |

## Machines to test

Run the kit on **Windows 11**, current release (24H2 or later), x64.

Windows 10 is not a supported platform for v1.0 (Microsoft ended its support in October 2025), so no Windows 10 run is required. The script still runs there and reports the machine as Windows 10 if you want an informational result.

Use a **fresh** virtual machine (Hyper-V, VMware or VirtualBox) installed from Microsoft's official media, with:
- a **standard local user account**, not an administrator. Create one after setup if the installer made you an administrator, and sign in as it;
- no extra software installed, in particular **no Microsoft Visual C++ Redistributable**;
- a **snapshot** taken before you start, so you can repeat the test.

## Running it

1. Copy the unpacked kit folder into the VM, for example to `Downloads\dil-kit`.
2. Open **Windows PowerShell** (not as administrator) in that folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\Verify-DataIntelligenceLab.ps1
   ```

3. **Phase 1** runs first, because a fresh Windows has no Visual C++ runtime. The script checks that setup refuses to install and names the prerequisite. It then asks you to double-click the setup once and confirm the message you see.
4. Install the **Microsoft Visual C++ Redistributable (x64)** from Microsoft: <https://aka.ms/vs/17/release/vc_redist.x64.exe>. This step needs administrator approval (UAC); that is expected.
5. Run the script again. **Phase 2** installs the app, runs the automated checks, then guides you through:
   - starting the app from the **Start menu**: a console window shows the local address and your browser opens the app;
   - uploading any small CSV file and opening its profile;
   - **closing the console window**: the script confirms every app process stopped;
   - uninstalling: the script confirms your datasets in `%USERPROFILE%\.data-intelligence-lab\storage` were kept.
6. Each phase writes `dil-verification-<build>-<time>.json` to your Desktop. Send back both files from each machine.

Answer the questions honestly. A "no" is a useful result.

## Notes

- The installer is not code-signed (an accepted limitation, SECURITY.md L4). If you double-click it, Windows SmartScreen may warn "Windows protected your PC". The script checks the installer against `SHA256SUMS` first; then choose **More info**, then **Run anyway**.
- The script installs to the default location (`%LOCALAPPDATA%\Programs\Data Intelligence Lab`) and removes the app at the end. Apart from the Visual C++ runtime you install yourself, the VM is left as it was, so you can use the snapshot again.
