# Troubleshooting Data Intelligence Lab 1.0

When Data Intelligence Lab cannot start, its console window stays open with a `[FATAL] Launcher failed: …` line. Read that line, press Enter to close the window, then find the matching entry below.

There is no log file. Everything is printed in the console window. When you report a problem, copy the text from that window.

## Setup problems

### "Data Intelligence Lab requires the Microsoft Visual C++ 2015-2022 Redistributable (x64)…"
The Visual C++ runtime is missing or older than 14.44. Install or update it from <https://aka.ms/vs/17/release/vc_redist.x64.exe> and run setup again. Setup never installs it for you.

### "Windows protected your PC"
The installer is not code-signed, so SmartScreen cannot identify the publisher. Compare the file's SHA-256 with the published hash (see [Installation, step 3](INSTALL.md#3-check-the-installer-file-recommended)). If it matches, choose **More info → Run anyway**.

### "Data Intelligence Lab is currently running (Process ID: …)"
Setup does not replace files while the app is running. Stop the app (press Ctrl+C in its console window, or close the window), then click **Retry**.

### "Setup was unable to verify running background processes"
Windows did not let setup list the running programs, sometimes because security software is scanning. Make sure Data Intelligence Lab is closed and click **Retry**. If it keeps happening, restart Windows and run setup again.

## Start-up problems

### "requires the Microsoft Visual C++ 2015-2022 Redistributable (x64), version 14.44 or later"
The runtime was removed or downgraded after installation. Install it again (link above) and start the app again.

### "The bundled Node.js runtime is missing … reinstall Data Intelligence Lab"
### "Backend entry point missing" or "Frontend entry point missing"
Files in the installation folder are missing or damaged, often because security software quarantined them. Run the installer again; your datasets are kept. If your security software reported a file from `%LOCALAPPDATA%\Programs\Data Intelligence Lab`, restore it there or add an exclusion.

### "Timed out after … waiting for port announcement"
A service started but did not become ready in time, usually on a very busy or slow PC, or because security software paused it. Close other heavy programs and try again. If it keeps happening, copy the console text into a bug report.

### "requires Windows …"
Your Windows version is older than the app can run on. Version 1.0 supports Windows 11.

### The console shows the address, but no browser opens
Copy the `Frontend: http://127.0.0.1:<port>` address from the console into your browser. The console shows a `[WARN] Could not automatically open default browser` line when Windows had no default browser to open.

## While using the app

### The page stops responding, or shows a connection error
The app stops when its console window is closed. Start it again from the Start menu. Because the port changes on every start, use the new address, not an old browser tab.

### An upload is rejected
The upload page says why. Common reasons:

| Reason | What to do |
|---|---|
| File larger than 200 MB | Split the file, or remove unneeded columns. |
| More than 1,000,000 rows or 1,000 columns | Split the file. |
| A single cell larger than 32 KB | Shorten or remove that field. |
| The file could not be read as CSV (encoding, delimiter, header or malformed rows) | Save it again as UTF-8 CSV with one header row and a consistent delimiter. |
| Processing took longer than 30 seconds | Use a smaller file. |
| Empty file | Check that the file has a header and at least one data row. |

### "Forbidden request" (HTTP 403)
The app only accepts requests from its own page at `127.0.0.1`. Open the address printed in the console. Other websites, or a different host name, are refused on purpose.

### Where are my datasets?
In `%USERPROFILE%\.data-intelligence-lab\storage`, unless you set `STORAGE_DIR`. Uninstalling the app does not remove them.

## Reporting a problem

Report it through the repository's GitHub issues. Include:
- your Windows version (Settings → System → About);
- your Visual C++ Redistributable version;
- the text from the console window.

Do not attach private datasets. Report security vulnerabilities privately, as described in [SECURITY.md](../SECURITY.md).
