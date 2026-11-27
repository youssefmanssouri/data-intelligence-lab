"""
Data Intelligence Lab - shutdown and restart behaviour of a staged bundle (Windows only).

Usage:
    python packaging/test_lifecycle.py --bundle <BUNDLE_DIR> --work-dir <EMPTY_DIR> [--evidence out.json]

Uses only the bundle's own executables, with PATH reduced to the Windows system
directories (as smoke_test_bundle.py does), and checks:
  1. the launcher, started in its own console, serves the UI and accepts a CSV upload;
  2. Ctrl+C in that console (the event a user's keypress sends, delivered with
     AttachConsole + GenerateConsoleCtrlEvent from a helper process) stops the app in
     order: launcher exit code 0 and "Data Intelligence Lab stopped.", both ports
     closed, the launcher's port files removed, no process left running from the
     bundle;
  3. a restart with the same storage serves the uploaded dataset again (API and page);
  4. a second Ctrl+C stops it in order again.

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
from typing import Dict, List
import uuid

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from smoke_test_bundle import SAMPLE_CSV, SmokeFailure, check, http, port_closed, scrubbed_env  # noqa: E402

CREATE_NEW_CONSOLE = 0x00000010
CTRL_C_EVENT = 0

# Runs in its own process: leaves its console, joins the launcher's, ignores Ctrl+C
# itself, then raises Ctrl+C for every process attached to that console.
CTRL_C_HELPER = r"""
import ctypes, sys
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit(10 + ctypes.get_last_error())
k.SetConsoleCtrlHandler(None, True)
if not k.GenerateConsoleCtrlEvent(0, 0):
    sys.exit(20)
"""


def app_processes(app_dir: Path) -> List[str]:
    """Processes whose executable lives under app_dir (via CIM; no extra dependency)."""
    script = (
        "$p = $env:DIL_APP_DIR.TrimEnd('\\') + '\\'; "
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
        "$_.ExecutablePath.StartsWith($p, [StringComparison]::OrdinalIgnoreCase) } | "
        "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, env={**os.environ, "DIL_APP_DIR": str(app_dir)})
    return [line for line in out.stdout.splitlines() if line.strip()]


class Launch:
    def __init__(self, app_dir: Path, env: Dict[str, str]) -> None:
        py = app_dir / "runtime" / "python" / "python.exe"
        self.lines: List[str] = []
        self.proc = subprocess.Popen([str(py), "-u", "launcher.py"], cwd=app_dir, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, errors="replace", creationflags=CREATE_NEW_CONSOLE)
        threading.Thread(target=lambda: self.lines.extend(iter(self.proc.stdout.readline, "")), daemon=True).start()

    def output(self) -> str:
        return "".join(self.lines)

    def wait_ready(self, timeout: float = 180) -> Dict[str, int]:
        ports: Dict[str, int] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(ports) < 2:
            if self.proc.poll() is not None:
                raise SmokeFailure(f"launcher exited {self.proc.returncode}:\n{self.output()}")
            for m in re.finditer(r"(Backend|Frontend):\s+http://127\.0\.0\.1:(\d+)", self.output()):
                ports[m.group(1).lower()] = int(m.group(2))
            time.sleep(0.2)
        if len(ports) < 2:
            raise SmokeFailure(f"launcher did not announce both ports:\n{self.output()}")
        return ports

    def ctrl_c(self) -> int:
        helper = subprocess.run([sys.executable, "-c", CTRL_C_HELPER, str(self.proc.pid)], timeout=30)
        return helper.returncode


def orderly_stop(launch: Launch, ports: Dict[str, int], app_dir: Path, label: str,
                 evidence: Dict[str, object]) -> None:
    temp = Path(os.environ.get("TEMP", os.environ.get("TMP", r"C:\Temp")))
    pattern = f"dil_*_{launch.proc.pid}_*.port"
    started = time.monotonic()
    helper_code = launch.ctrl_c()
    check(f"{label}-ctrl-c-delivered", helper_code == 0, f"helper exit {helper_code}")
    try:
        code = launch.proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        launch.proc.kill()
        raise SmokeFailure(f"CHECK {label}-launcher-exits: FAIL still running 30 s after Ctrl+C\n{launch.output()}")
    elapsed = round(time.monotonic() - started, 2)
    time.sleep(1)
    output = launch.output()
    evidence[f"{label}_shutdown"] = {"exit_code": code, "seconds": elapsed, "output_tail": output[-1500:]}
    check(f"{label}-launcher-exit-0", code == 0, f"exit {code} after {elapsed} s")
    check(f"{label}-orderly-stop-message", "Data Intelligence Lab stopped." in output and "[ERROR]" not in output)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(port_closed(p) for p in ports.values()):
        time.sleep(0.5)
    check(f"{label}-ports-closed", all(port_closed(p) for p in ports.values()), ports)
    left = sorted(str(p.name) for p in temp.glob(pattern))
    check(f"{label}-port-files-removed", not left, left)
    procs = app_processes(app_dir)
    check(f"{label}-no-app-processes-left", not procs, procs)


def lifecycle(bundle: Path, work: Path, evidence: Dict[str, object]) -> None:
    if sys.platform != "win32":
        raise SmokeFailure("Windows only.")
    if work.exists() and any(work.iterdir()):
        raise SmokeFailure(f"--work-dir must be empty: {work}")
    app_dir = work / "Lifecycle Copy" / "Data Intelligence Lab"
    shutil.copytree(bundle, app_dir)
    storage = work / "user storage"
    storage.mkdir(parents=True)
    env = scrubbed_env(storage)

    # 1. First run: upload through the UI proxy
    first = Launch(app_dir, env)
    ports = first.wait_ready()
    fe = f"http://127.0.0.1:{ports['frontend']}"
    boundary = uuid.uuid4().hex
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"lifecycle.csv\"\r\n"
               f"Content-Type: text/csv\r\n\r\n{SAMPLE_CSV}\r\n--{boundary}--\r\n").encode()
    status, body = http("POST", fe + "/api/v1/datasets/upload", payload, {
        "Content-Type": f"multipart/form-data; boundary={boundary}", "Origin": fe, "Sec-Fetch-Site": "same-origin"},
        timeout=120)
    check("first-run-upload", status == 201, f"HTTP {status}")
    dataset_id = json.loads(body)["dataset_id"]
    evidence["dataset_id"] = dataset_id

    # 2. Ctrl+C
    orderly_stop(first, ports, app_dir, "first", evidence)

    # 3. Restart with the same storage
    second = Launch(app_dir, env)
    ports2 = second.wait_ready()
    fe2 = f"http://127.0.0.1:{ports2['frontend']}"
    status, body = http("GET", f"{fe2}/api/v1/datasets/{dataset_id}")
    meta = json.loads(body) if status == 200 else {}
    check("restart-dataset-api", status == 200 and meta.get("dataset_id") == dataset_id
          and meta.get("row_count") == 4, f"HTTP {status}")
    status, body = http("GET", f"{fe2}/datasets/{dataset_id}")
    check("restart-dataset-page", status == 200, f"HTTP {status}, {len(body)} bytes")

    # 4. Ctrl+C again
    orderly_stop(second, ports2, app_dir, "second", evidence)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()
    evidence: Dict[str, object] = {}
    try:
        lifecycle(args.bundle.resolve(), args.work_dir.resolve(), evidence)
        evidence["result"] = "PASS"
        print("LIFECYCLE_TEST: PASS")
        return 0
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        print(f"LIFECYCLE_TEST: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
