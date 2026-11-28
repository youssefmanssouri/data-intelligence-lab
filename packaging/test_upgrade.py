"""
Data Intelligence Lab - upgrade test from a published release (Windows only).

Usage:
    python packaging/test_upgrade.py --old-setup <published setup.exe> --old-version 1.0.0
        --new-setup <new setup.exe> --new-version 1.1.0 --bundle <new BUNDLE_DIR>
        --work-dir <EMPTY_DIR> [--evidence out.json]

Drives both real installers silently, as a per-user install, and checks:
  1. the published installer installs and its app creates a dataset (upload through the proxy);
  2. the new installer upgrades that installation in place: exit 0, the installed files match
     the new bundle manifest exactly, the uninstall entry reports the new version;
  3. the upgraded app serves the dataset created by the old version, computes insights for it,
     and leaves its stored files byte-identical (insights are never written to disk);
  4. uninstalling the upgraded app keeps the dataset.

Each check prints "CHECK <name>: PASS"; any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Dict, List, Tuple
import uuid

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from build_runtime_bundle import compute_sha256  # noqa: E402
from smoke_test_bundle import SmokeFailure, check, http, port_closed, scrubbed_env  # noqa: E402
from test_installer import (  # noqa: E402
    APP_NAME, account_info, os_info, run_setup, start_menu_dir, uninstall_entry, verify_installed,
)

# 40 rows: a column with missing values, an identifier and an outlier, so insights are not empty.
UPGRADE_CSV = "order_id,region,amount,note\n" + "".join(
    f"{i + 1},{'North' if i % 2 else 'South'},{9000 if i == 39 else 50 + i % 7},{'' if i % 3 else 'x'}\n"
    for i in range(40)
)


def start_app(app_dir: Path, storage: Path) -> Tuple[subprocess.Popen, Dict[str, int], List[str]]:
    py = app_dir / "runtime" / "python" / "python.exe"
    lines: List[str] = []
    proc = subprocess.Popen([str(py), "-u", "launcher.py"], cwd=app_dir, env=scrubbed_env(storage),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True).start()
    ports: Dict[str, int] = {}
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and len(ports) < 2:
        if proc.poll() is not None:
            raise SmokeFailure(f"launcher exited {proc.returncode}:\n" + "".join(lines))
        for line in list(lines):
            m = re.search(r"(Backend|Frontend):\s+http://127\.0\.0\.1:(\d+)", line)
            if m:
                ports[m.group(1).lower()] = int(m.group(2))
        time.sleep(0.2)
    if len(ports) < 2:
        proc.kill()
        raise SmokeFailure("launcher did not announce both ports:\n" + "".join(lines)[-4000:])
    return proc, ports, lines


def stop_app(proc: subprocess.Popen, ports: Dict[str, int]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not all(port_closed(p) for p in ports.values()):
        time.sleep(0.5)


def dataset_files(storage: Path, dataset_id: str) -> Dict[str, str]:
    folder = storage / "datasets" / dataset_id
    return {p.name: compute_sha256(p) for p in sorted(folder.iterdir()) if p.is_file()}


def upgrade(old_setup: Path, old_version: str, new_setup: Path, new_version: str, bundle: Path, work: Path,
            evidence: Dict[str, object]) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("The installer targets Windows; run this test on Windows.")
    if work.exists() and any(work.iterdir()):
        raise SmokeFailure(f"--work-dir must be empty: {work}")
    work.mkdir(parents=True, exist_ok=True)
    if uninstall_entry() is not None or start_menu_dir().exists():
        raise SmokeFailure("A previous Data Intelligence Lab installation exists for this user; use a clean account.")
    evidence.update(account=account_info(), os=os_info(), old_setup_sha256=compute_sha256(old_setup),
                    new_setup_sha256=compute_sha256(new_setup))
    app_dir = work / "Local Programs" / APP_NAME
    storage = work / "user storage"
    storage.mkdir()

    # 1. Published version: install and create a dataset with its own app.
    code = run_setup(old_setup, app_dir, work / "install-old.log")
    check("old-install-exit-code", code == 0, code)
    entry = uninstall_entry() or {}
    check("old-version-installed", entry.get("DisplayVersion") == old_version, entry)
    proc, ports, lines = start_app(app_dir, storage)
    try:
        fe = f"http://127.0.0.1:{ports['frontend']}"
        boundary = uuid.uuid4().hex
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"upgrade.csv\"\r\n"
                   f"Content-Type: text/csv\r\n\r\n{UPGRADE_CSV}\r\n--{boundary}--\r\n").encode()
        status, body = http("POST", fe + "/api/v1/datasets/upload", payload, {
            "Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": fe, "Sec-Fetch-Site": "same-origin",
        }, timeout=120)
        check("old-app-upload", status == 201, f"HTTP {status} {body[:300]!r}")
        dataset_id = json.loads(body)["dataset_id"]
    finally:
        stop_app(proc, ports)
        evidence["old_launcher_output"] = "".join(lines)[-4000:]
    before = dataset_files(storage, dataset_id)
    evidence["dataset_id"] = dataset_id
    evidence["dataset_files_before"] = before
    check("old-dataset-stored", set(before) == {"data.parquet", "metadata.json"}, before)

    # 2. Upgrade in place.
    code = run_setup(new_setup, app_dir, work / "install-new.log")
    check("upgrade-exit-code", code == 0, code)
    entry = uninstall_entry() or {}
    evidence["uninstall_entry_after_upgrade"] = entry
    check("new-version-installed", entry.get("DisplayVersion") == new_version, entry)
    result = verify_installed(app_dir)
    check("upgraded-files-match-new-manifest", True, f"{result['file_count']} files")
    check("new-manifest-promoted",
          (app_dir / "manifest.current.json").read_bytes() == (bundle / "manifest.json").read_bytes())
    check("old-manifest-kept-as-previous", (app_dir / "manifest.previous.json").is_file())

    # 3. The upgraded app reads the old dataset and computes insights for it.
    proc, ports, lines = start_app(app_dir, storage)
    try:
        fe = f"http://127.0.0.1:{ports['frontend']}"
        status, body = http("GET", f"{fe}/api/v1/datasets/{dataset_id}")
        check("old-dataset-served", status == 200 and json.loads(body).get("dataset_id") == dataset_id,
              f"HTTP {status}")
        status, body = http("GET", f"{fe}/api/v1/datasets/{dataset_id}/insights", timeout=60)
        insights = json.loads(body) if status == 200 else {}
        codes = sorted({item.get("code") for item in insights.get("items", [])})
        evidence["insights_codes"] = codes
        check("insights-for-old-dataset",
              status == 200 and insights.get("dataset_id") == dataset_id and len(codes) > 0,
              f"HTTP {status} {codes}")
    finally:
        stop_app(proc, ports)
        evidence["new_launcher_output"] = "".join(lines)[-4000:]
    after = dataset_files(storage, dataset_id)
    check("old-dataset-unchanged", after == before, after)

    # 4. Uninstall keeps the dataset.
    code = subprocess.run([str(app_dir / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                           f"/LOG={work / 'uninstall.log'}"], timeout=600).returncode
    check("uninstall-exit-code", code == 0, code)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and (app_dir.exists() or uninstall_entry() is not None):
        time.sleep(1)
    check("uninstalled", not app_dir.exists() and uninstall_entry() is None)
    check("dataset-kept-after-uninstall", dataset_files(storage, dataset_id) == before)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--old-setup", required=True, type=Path)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-setup", required=True, type=Path)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()
    evidence: Dict[str, object] = {}
    try:
        upgrade(args.old_setup.resolve(), args.old_version, args.new_setup.resolve(), args.new_version,
                args.bundle.resolve(), args.work_dir.resolve(), evidence)
        evidence["result"] = "PASS"
        print("UPGRADE_TEST: PASS")
        return 0
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        print(f"UPGRADE_TEST: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
