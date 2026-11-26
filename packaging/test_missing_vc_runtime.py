"""
Data Intelligence Lab - behaviour on a machine without the VC++ runtime (Windows only).

DESTRUCTIVE: uninstalls the Microsoft Visual C++ 2015-2022 Redistributable (x64)
from the machine. Run it only on a disposable machine (CI runner or throwaway VM);
it refuses to run unless --i-understand-this-uninstalls-vc-runtime is given.

Usage:
    python packaging/test_missing_vc_runtime.py --setup <DataIntelligenceLab-Setup.exe>
        --work-dir <EMPTY_DIR> --i-understand-this-uninstalls-vc-runtime [--evidence out.json]

Scenario (the real prerequisite, not a test build):
  1. With the runtime present, install the app silently (exit 0).
  2. Uninstall the VC++ x64 runtime the way Windows does (its registered uninstaller).
  3. Start the installed app: no service may start, and the user must get either the
     launcher's actionable message or Windows' missing-DLL failure; which one is recorded.
  4. Re-running setup is refused by the real 14.44 check ("It is not installed").
  5. The app can still be uninstalled without the runtime.

The interpreter running this script must not depend on the system VC++ runtime
(the python.org / setup-python CPython ships its own vcruntime140.dll).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from smoke_test_bundle import SmokeFailure, check, scrubbed_env  # noqa: E402
from test_installer import APP_NAME, run_setup, uninstall_entry  # noqa: E402

VC_KEY = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
VC_DISPLAY_NAME = re.compile(r"^Microsoft Visual C\+\+ (2015-20\d\d|v14) Redistributable \(x64\)", re.I)
STATUS_DLL_NOT_FOUND = 0xC0000135


def vc_registration() -> Optional[Dict[str, int]]:
    import winreg

    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VC_KEY, 0, winreg.KEY_READ | view) as key:
                return {n: winreg.QueryValueEx(key, n)[0] for n in ("Installed", "Major", "Minor", "Bld")}
        except OSError:
            continue
    return None


def vc_bundles() -> List[Dict[str, str]]:
    """Registered VC++ x64 redistributable bundles (Burn), with their cached uninstallers."""
    import winreg

    found = []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, i)
                try:
                    with winreg.OpenKey(root, name) as key:
                        display = winreg.QueryValueEx(key, "DisplayName")[0]
                        if not VC_DISPLAY_NAME.match(display):
                            continue
                        entry = {"key": name, "display_name": display}
                        for value in ("BundleCachePath", "QuietUninstallString", "UninstallString", "DisplayVersion"):
                            try:
                                entry[value] = winreg.QueryValueEx(key, value)[0]
                            except OSError:
                                pass
                        found.append(entry)
                except OSError:
                    continue
    return found


def uninstall_vc_runtime(evidence: Dict[str, object]) -> None:
    bundles = [b for b in vc_bundles() if b.get("BundleCachePath")]
    evidence["vc_bundles"] = bundles
    if not bundles:
        raise SmokeFailure("No registered VC++ x64 redistributable bundle with a cached uninstaller was found")
    codes = []
    for bundle in bundles:
        result = subprocess.run([bundle["BundleCachePath"], "/uninstall", "/quiet", "/norestart"], timeout=600)
        codes.append(result.returncode)
    evidence["vc_uninstall_exit_codes"] = codes
    # 0 = done, 3010 = done, reboot required to replace files in use.
    check("vc-runtime-uninstalled", all(c in (0, 3010) for c in codes) and vc_registration() is None,
          {"exit_codes": codes, "registration": vc_registration()})
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    evidence["system32_after_uninstall"] = {n: (system32 / n).exists() for n in ("vcruntime140.dll", "msvcp140.dll")}


def scenario(setup: Path, work: Path, evidence: Dict[str, object]) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("Windows only.")
    if work.exists() and any(work.iterdir()):
        raise SmokeFailure(f"--work-dir must be empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    app_dir = work / "Local Programs" / APP_NAME

    before = vc_registration()
    evidence["vc_before"] = before
    check("vc-runtime-present-initially", bool(before) and before.get("Installed") == 1, before)

    # 1. Install while the prerequisite is met
    code = run_setup(setup, app_dir, work / "install.log")
    check("install-with-runtime", code == 0, code)

    # 2. Remove the runtime
    uninstall_vc_runtime(evidence)

    # 3. Start the installed app without the runtime
    storage = work / "storage"
    storage.mkdir()
    py = app_dir / "runtime" / "python" / "python.exe"
    try:
        result = subprocess.run([str(py), "-u", "launcher.py"], cwd=app_dir, env=scrubbed_env(storage),
                                capture_output=True, text=True, errors="replace", timeout=120)
        code, output = result.returncode, (result.stdout + result.stderr)
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure("CHECK launch-without-runtime: FAIL the launcher kept running without the runtime") from exc
    unsigned = code & 0xFFFFFFFF
    evidence["launch_without_runtime"] = {"exit_code": code, "exit_code_hex": hex(unsigned), "output": output[-2000:]}
    started = re.search(r"(Backend|Frontend):\s+http://127\.0\.0\.1:\d+", output) is not None
    if unsigned == STATUS_DLL_NOT_FOUND:
        outcome = "windows-missing-dll (python.exe cannot load VCRUNTIME140.dll)"
    elif code == 1 and "requires the Microsoft Visual C++ 2015-2022 Redistributable (x64)" in output:
        outcome = "launcher-message"
    else:
        outcome = "unexpected"
    evidence["launch_without_runtime_outcome"] = outcome
    check("launch-without-runtime-fails-safely", outcome != "unexpected" and not started,
          f"{outcome}, exit {hex(unsigned)}")
    check("no-data-written-without-runtime", not any(storage.rglob("*")))

    # 4. Setup refuses with the real 14.44 requirement
    code = run_setup(setup, work / "Second" / APP_NAME, work / "install-without-runtime.log")
    log_text = (work / "install-without-runtime.log").read_text(encoding="utf-8", errors="replace")
    line = next((l for l in log_text.splitlines() if "VC++ runtime prerequisite not met" in l), "")
    evidence["install_without_runtime"] = {"exit_code": code, "log": line}
    check("install-blocked-without-runtime",
          code != 0 and "required 14.44 or later" in line and "It is not installed." in line
          and not (work / "Second").exists(), f"exit {code}: {line}")

    # 5. Uninstall still works
    uninstaller = app_dir / "unins000.exe"
    code = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                           f"/LOG={work / 'uninstall.log'}"], timeout=600).returncode
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and (app_dir.exists() or uninstall_entry() is not None):
        time.sleep(1)
    check("uninstall-without-runtime", code == 0 and not app_dir.exists() and uninstall_entry() is None, code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--i-understand-this-uninstalls-vc-runtime", action="store_true", dest="confirmed")
    args = parser.parse_args()
    if not args.confirmed:
        print("Refusing to run: this uninstalls the VC++ runtime. Pass --i-understand-this-uninstalls-vc-runtime "
              "on a disposable machine only.", file=sys.stderr)
        return 2
    evidence: Dict[str, object] = {}
    try:
        scenario(args.setup.resolve(), args.work_dir.resolve(), evidence)
        evidence["result"] = "PASS"
        print("MISSING_VC_RUNTIME_TEST: PASS")
        return 0
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        print(f"MISSING_VC_RUNTIME_TEST: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
