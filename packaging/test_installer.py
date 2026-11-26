"""
Data Intelligence Lab - installer lifecycle test (Windows only).

Usage:
    python packaging/test_installer.py --setup <DataIntelligenceLab-Setup.exe>
        --vc-gate-setup <setup compiled with /DVCMinMinor=99> --bundle <BUNDLE_DIR>
        --work-dir <EMPTY_DIR> [--evidence out.json]

Drives the real installer silently, as a per-user install, and checks:
  1. fresh install into a path with spaces: exit 0, installed files match the
     bundle manifest, manifest promoted, Start menu shortcut targets the console
     launcher, uninstall entry registered, no desktop shortcut by default;
  2. the installed copy passes the full smoke test in place (smoke_test_bundle);
  3. re-running setup while the app runs from the install folder is refused
     (no file replaced, app keeps running);
  4. reinstall over an existing install: files listed only in the previous
     manifest are deleted, while unsafe entries (traversal, absolute paths)
     are ignored and files outside the install folder survive;
  5. silent uninstall removes the install folder, shortcuts and uninstall entry,
     and preserves %USERPROFILE%\\.data-intelligence-lab\\storage;
  6. an installer whose VC++ requirement the machine does not meet (test build
     with VCMinMinor=99) exits non-zero without creating the install folder.

Each check prints "CHECK <name>: PASS"; any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from build_runtime_bundle import compute_sha256, verify_bundle  # noqa: E402
from smoke_test_bundle import (  # noqa: E402
    INSTALLER_FILES, RUNTIME_CACHE_PREFIX, SmokeFailure, check, port_closed, scrubbed_env, smoke,
)

APP_ID = "{D1A7B1CE-1A8B-4C9D-9E5A-3A6D47C3B8E1}"
APP_NAME = "Data Intelligence Lab"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"


def start_menu_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def shortcut(path: Path) -> Dict[str, str]:
    # -Command does not bind trailing arguments to $args; pass the path via the environment.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:DIL_LNK); "
        "@{TargetPath=$s.TargetPath; Arguments=$s.Arguments; WorkingDirectory=$s.WorkingDirectory} | ConvertTo-Json"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, env={**os.environ, "DIL_LNK": str(path)})
    if result.returncode != 0:
        raise SmokeFailure(f"Reading shortcut {path} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def uninstall_entry() -> Optional[Dict[str, str]]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            values = {}
            for name in ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString"):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    pass
            return values
    except OSError:
        return None


def find_uninstall_keys(fragment: str) -> List[str]:
    """Diagnostics: every Uninstall subkey (HKCU/HKLM, both views) whose name contains ``fragment``."""
    import winreg

    found = []
    base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, hive_name in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, base, 0, winreg.KEY_READ | view) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        name = winreg.EnumKey(key, i)
                        if fragment.lower() in name.lower():
                            found.append(f"{hive_name}({'64' if view == winreg.KEY_WOW64_64KEY else '32'})\\{name}")
            except OSError:
                pass
    return sorted(set(found))


def run_setup(setup: Path, app_dir: Path, log: Path, extra: Optional[List[str]] = None) -> int:
    cmd = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={app_dir}", f"/LOG={log}"]
    return subprocess.run(cmd + (extra or []), timeout=900).returncode


def same_path(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a.rstrip("\\"))) == os.path.normcase(os.path.realpath(str(b)))


def verify_installed(app_dir: Path) -> Dict[str, object]:
    return verify_bundle(app_dir, allow_unlisted=lambda rel: rel in INSTALLER_FILES or rel.startswith(RUNTIME_CACHE_PREFIX))


def lifecycle(setup: Path, vc_gate_setup: Path, bundle: Path, work: Path, evidence: Dict[str, object]) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("The installer targets Windows; run this test on Windows.")
    if work.exists() and any(work.iterdir()):
        raise SmokeFailure(f"--work-dir must be empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    if uninstall_entry() is not None or start_menu_dir().exists():
        raise SmokeFailure("A previous Data Intelligence Lab installation exists for this user; use a clean account.")
    evidence["setup_sha256"] = compute_sha256(setup)
    app_dir = work / "Local Programs" / APP_NAME
    bundle_manifest = (bundle / "manifest.json").read_bytes()

    # 1. Fresh install
    code = run_setup(setup, app_dir, work / "install-1.log")
    check("install-exit-code", code == 0, code)
    result = verify_installed(app_dir)
    check("installed-files-match-manifest", True, f"{result['file_count']} files")
    check("manifest-promoted", (app_dir / "manifest.current.json").read_bytes() == bundle_manifest)
    check("journal-completed", not (app_dir / "install.journal").exists())
    lnk = start_menu_dir() / f"{APP_NAME}.lnk"
    target = shortcut(lnk) if lnk.is_file() else {}
    evidence["shortcut"] = target
    check("start-menu-shortcut",
          same_path(target.get("TargetPath", ""), app_dir / "runtime" / "python" / "python.exe")
          and "launcher.py" in target.get("Arguments", "")
          and same_path(target.get("WorkingDirectory", ""), app_dir), target)
    desktop = Path(os.environ["USERPROFILE"]) / "Desktop" / f"{APP_NAME}.lnk"
    check("no-desktop-shortcut-by-default", not desktop.exists())
    entry = uninstall_entry()
    evidence["uninstall_entry"] = entry
    if not entry:
        evidence["uninstall_keys_found"] = find_uninstall_keys("D1A7B1CE")
    check("uninstall-entry", bool(entry) and same_path(entry.get("InstallLocation", ""), app_dir),
          entry or evidence.get("uninstall_keys_found"))

    # 2. Installed copy, tested in place
    smoke_work = work / "smoke"
    smoke_evidence: Dict[str, object] = {}
    try:
        smoke(app_dir, smoke_work, smoke_evidence, installed=True)
    finally:
        evidence["installed_smoke"] = {k: v for k, v in smoke_evidence.items() if k != "launcher_output"}
    check("installed-smoke-test", True)

    # 3. Setup refuses to replace files while the app runs from the install folder
    storage = work / "running-storage"
    storage.mkdir()
    env = scrubbed_env(storage)
    py = app_dir / "runtime" / "python" / "python.exe"
    lines: List[str] = []
    proc = subprocess.Popen([str(py), "-u", "launcher.py"], cwd=app_dir, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace")
    threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True).start()
    try:
        ports: Dict[str, int] = {}
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and len(ports) < 2 and proc.poll() is None:
            for line in list(lines):
                m = re.search(r"(Backend|Frontend):\s+http://127\.0\.0\.1:(\d+)", line)
                if m:
                    ports[m.group(1).lower()] = int(m.group(2))
            time.sleep(0.2)
        check("app-running-for-block-test", len(ports) == 2, ports)
        before = (app_dir / "manifest.current.json").stat().st_mtime_ns
        code = run_setup(setup, app_dir, work / "install-while-running.log")
        evidence["install_while_running_exit_code"] = code
        log_text = (work / "install-while-running.log").read_text(encoding="utf-8", errors="replace")
        # Category 1 (confirmed instance in {app}), not merely "could not verify".
        check("install-refused-while-running", code != 0 and "currently running (Process ID" in log_text, code)
        check("app-untouched-while-running",
              (app_dir / "manifest.current.json").stat().st_mtime_ns == before
              and proc.poll() is None and not any(port_closed(p) for p in ports.values()))
    finally:
        proc.kill()
        proc.wait(timeout=30)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not all(port_closed(p) for p in ports.values()):
        time.sleep(0.5)

    # 4. Reinstall: manifest-driven cleanup of obsolete files, confined to the install folder
    obsolete = app_dir / "backend" / "obsolete_module.py"
    obsolete.write_text("# shipped by an older version\n", encoding="utf-8")
    outside_rel = work / "outside-relative.txt"
    outside_abs = work / "outside-absolute.txt"
    for f in (outside_rel, outside_abs):
        f.write_text("must survive", encoding="utf-8")
    current = json.loads((app_dir / "manifest.current.json").read_text(encoding="utf-8"))
    current["files"] += [
        {"path": "backend/obsolete_module.py", "size": 0, "sha256": "0" * 64},
        {"path": "../outside-relative.txt", "size": 0, "sha256": "0" * 64},
        {"path": outside_abs.as_posix(), "size": 0, "sha256": "0" * 64},
    ]
    (app_dir / "manifest.current.json").write_text(json.dumps(current, indent=2), encoding="utf-8")
    code = run_setup(setup, app_dir, work / "install-2.log")
    check("reinstall-exit-code", code == 0, code)
    check("obsolete-file-removed", not obsolete.exists())
    check("outside-files-preserved", outside_rel.is_file() and outside_abs.is_file())
    check("manifest-rotated",
          (app_dir / "manifest.previous.json").is_file()
          and (app_dir / "manifest.current.json").read_bytes() == bundle_manifest)
    log_text = (work / "install-2.log").read_text(encoding="utf-8", errors="replace")
    check("unsafe-paths-rejected", log_text.count("Rejected unsafe candidate obsolete path") >= 2)
    verify_installed(app_dir)
    check("reinstalled-files-match-manifest", True)

    # 5. Uninstall preserves user data
    user_storage = Path(os.environ["USERPROFILE"]) / ".data-intelligence-lab" / "storage"
    sentinel = user_storage / "datasets" / "ds_uninstall_sentinel" / "metadata.json"
    sentinel_created = not sentinel.exists()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    if sentinel_created:
        sentinel.write_text('{"dataset_id": "ds_uninstall_sentinel"}', encoding="utf-8")
    sentinel_hash = compute_sha256(sentinel)
    uninstaller = app_dir / "unins000.exe"
    check("uninstaller-present", uninstaller.is_file())
    code = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                           f"/LOG={work / 'uninstall.log'}"], timeout=600).returncode
    check("uninstall-exit-code", code == 0, code)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and (app_dir.exists() or uninstall_entry() is not None):
        time.sleep(1)
    leftovers = sorted(p.relative_to(app_dir).as_posix() for p in app_dir.rglob("*")) if app_dir.exists() else []
    evidence["uninstall_leftovers"] = leftovers[:50]
    check("install-folder-removed", not app_dir.exists(), leftovers[:20])
    check("shortcuts-removed", not start_menu_dir().exists())
    check("uninstall-entry-removed", uninstall_entry() is None)
    check("user-storage-preserved", sentinel.is_file() and compute_sha256(sentinel) == sentinel_hash)
    if sentinel_created:
        shutil.rmtree(sentinel.parent)

    # 6. VC++ prerequisite gate
    gated_dir = work / "Gated" / APP_NAME
    code = run_setup(vc_gate_setup, gated_dir, work / "install-vc-gate.log")
    evidence["vc_gate_exit_code"] = code
    log_text = (work / "install-vc-gate.log").read_text(encoding="utf-8", errors="replace")
    vc_line = next((line for line in log_text.splitlines() if "VC++ runtime prerequisite not met" in line), "")
    evidence["vc_gate_log"] = vc_line
    check("vc-gate-blocks-install", code != 0 and bool(vc_line) and not gated_dir.exists(), f"exit {code}: {vc_line}")
    check("vc-gate-no-uninstall-entry", uninstall_entry() is None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--vc-gate-setup", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()
    evidence: Dict[str, object] = {}
    try:
        lifecycle(args.setup.resolve(), args.vc_gate_setup.resolve(), args.bundle.resolve(),
                  args.work_dir.resolve(), evidence)
        evidence["result"] = "PASS"
        print("INSTALLER_LIFECYCLE_TEST: PASS")
        return 0
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        print(f"INSTALLER_LIFECYCLE_TEST: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
