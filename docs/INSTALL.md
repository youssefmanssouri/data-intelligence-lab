# Installing Data Intelligence Lab

Data Intelligence Lab runs entirely on your PC. It installs for your Windows account only, needs no administrator rights, and sends nothing over the network.

## 1. Requirements

| | |
|---|---|
| **Windows** | Windows 11, 64-bit (x64). Windows 10 is not supported. Microsoft ended its support in October 2025, and this release is not tested on it. |
| **Microsoft Visual C++ Redistributable** | The 2015-2022 Redistributable (x64), version **14.44 or later**. See step 2. |
| **Disk space** | About 550 MB for the application, plus room for your datasets. |
| **Browser** | Any current browser. The app opens in your default browser. |

You do not need Python, Node.js or any other runtime: the installer includes the ones the app uses.

## 2. Install the Visual C++ Redistributable (once)

Data Intelligence Lab relies on Microsoft's Visual C++ runtime but does not ship or download it. If it is missing or older than 14.44, setup stops and tells you so.

1. Download `vc_redist.x64.exe` from Microsoft: <https://aka.ms/vs/17/release/vc_redist.x64.exe>.
2. Run it and follow its steps. This one needs administrator rights, because it installs for the whole PC.

Many PCs already have it. To check, open **Settings → Apps → Installed apps** and look for *Microsoft Visual C++ 2015-2022 Redistributable (x64)* with a version of 14.44 or later.

## 3. Check the installer file (recommended)

The installer is **not code-signed**. Windows cannot confirm who published it, so it shows a warning (see step 4). Before you run it, confirm that it is the exact file that was released by comparing its SHA-256 hash with the one published in the release notes.

In PowerShell, in the folder where you saved it:

```powershell
Get-FileHash .\DataIntelligenceLab-Setup-1.1.0.exe -Algorithm SHA256
```

If the hash differs from the published one, do not run the file.

## 4. Run setup

1. Double-click `DataIntelligenceLab-Setup-1.1.0.exe`.
2. Because the installer is unsigned, Microsoft Defender SmartScreen may show **"Windows protected your PC"**. If the hash matched in step 3, click **More info**, then **Run anyway**.
3. Follow the steps. You can add a desktop shortcut (it is off by default).

Setup installs to `%LOCALAPPDATA%\Programs\Data Intelligence Lab` and adds **Data Intelligence Lab** to the Start menu.

## 5. Start and stop

- **Start:** choose **Data Intelligence Lab** in the Start menu. A console window opens, and after a few seconds the app opens in your browser. Keep the console window open while you work. It shows the local address, `http://127.0.0.1:<port>`; if the browser does not open, copy that address into it.
- **Stop:** press **Ctrl+C** in the console window, or close the window.

The app only listens on `127.0.0.1` (your own PC). Its ports are chosen at random on every start, so the address changes each time.

## 6. Your data

Uploaded datasets are stored as Parquet files in:

```
%USERPROFILE%\.data-intelligence-lab\storage
```

Only your Windows account can read this folder (normal file permissions). The files are not encrypted. To use another location, set the `STORAGE_DIR` environment variable before starting the app.

Upload limits: CSV files up to **200 MB**, **1,000,000 rows**, **1,000 columns** and **32 KB per cell**, with **30 seconds** of processing time per file.

## 7. Update or reinstall

Close Data Intelligence Lab, then run the new installer. Your datasets are kept. If the app is still running, setup asks you to close it first.

## 8. Uninstall

Use **Settings → Apps → Installed apps → Data Intelligence Lab → Uninstall**. This removes the application files and shortcuts. **Your datasets are kept.** To remove them as well, delete `%USERPROFILE%\.data-intelligence-lab` yourself.

The Visual C++ Redistributable stays installed, because other programs may need it.

## Privacy

Data Intelligence Lab has no telemetry. Dataset content, metadata and diagnostics never leave your PC. See [SECURITY.md](../SECURITY.md) for the security controls and known limitations.

If something goes wrong, see [Troubleshooting](TROUBLESHOOTING.md).
