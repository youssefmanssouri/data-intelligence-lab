"""
Data Intelligence Lab - independent smoke test of a staged Windows runtime bundle.

Usage (Windows only, from any Python 3.12+):
    python packaging/smoke_test_bundle.py --bundle <BUNDLE_DIR> --work-dir <EMPTY_DIR> [--evidence out.json]
    python packaging/smoke_test_bundle.py --installed <APP_DIR> --work-dir <EMPTY_DIR>   (an installed copy)

The bundle is first re-verified against its manifest, then copied to a path that
contains spaces (as an installed copy would be; with --installed, the installed
directory is tested where it is) and exercised only through its
own executables, with PATH reduced to the Windows system directories so nothing
can fall back to the build machine's Python or Node.js:

  1. Bundled python.exe: version, sys.path confined to the bundle, no Microsoft
     CRT DLL inside the bundle, the C runtime loaded from the system, vendored
     imports (numpy/pandas/httpx absent), and a Parquet round trip.
  2. Bundled node.exe: pinned version.
  3. launcher.py under the bundled python.exe: backend and frontend start inside
     the Job Object; the UI, the proxied health check, a CSV upload through the
     frontend proxy and the dataset lookup succeed; data.parquet is written to
     STORAGE_DIR and read back by the bundled runtime.
  4. Killing the launcher process tears down both services (Job Object
     KILL_ON_JOB_CLOSE): both ports stop accepting connections.
  5. Running the app wrote nothing into the application directory except the
     Next.js route cache (which the uninstaller removes).

Each check prints "CHECK <name>: PASS"; any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional
import urllib.error
import urllib.request
import uuid

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from build_runtime_bundle import NODE_VERSION, verify_bundle  # noqa: E402

# Files Inno Setup adds next to the bundle (packaging/setup.iss).
INSTALLER_FILES = frozenset({"unins000.exe", "unins000.dat", "unins000.msg",
                             "manifest.current.json", "manifest.previous.json"})
RUNTIME_CACHE_PREFIX = "frontend/.next/server/route-cache/"

SAMPLE_CSV = (
    "id,name,amount,joined\n"
    "1,alpha,10.5,2024-01-03\n"
    "2,beta,,2024-02-11\n"
    "3,gamma,7.25,2024-03-29\n"
    "4,delta,3.0,2024-04-30\n"
)

PYTHON_PROBE = r"""
import ctypes, importlib.util, json, os, sys
norm = lambda p: os.path.normcase(os.path.realpath(p))
root = norm(sys.argv[1])
out = {"version": sys.version.split()[0], "executable": sys.executable, "path": sys.path}
out["path_outside_bundle"] = [p for p in sys.path if p and not norm(p).startswith(root)]
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetModuleHandleW.restype = ctypes.c_void_p
k32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
loaded = {}
for dll in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"):
    h = k32.GetModuleHandleW(dll)
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        k32.GetModuleFileNameW(h, buf, 1024)
        loaded[dll] = buf.value
import pyarrow as pa, pyarrow.parquet as pq, pyarrow.csv  # noqa: F401
import fastapi, uvicorn, pydantic, starlette, zoneinfo
for dll in ("msvcp140.dll",):
    h = k32.GetModuleHandleW(dll)
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        k32.GetModuleFileNameW(h, buf, 1024)
        loaded[dll] = buf.value
out["crt_loaded_from"] = loaded
out["absent"] = {m: importlib.util.find_spec(m) is None for m in ("numpy", "pandas", "httpx")}
out["pyarrow"] = pa.__version__
out["fastapi"] = fastapi.__version__
tmp = os.path.join(os.environ["TEMP"], "dil-smoke-roundtrip.parquet")
pq.write_table(pa.table({"x": [1, 2, None], "s": ["a", "", "c"]}), tmp)
t = pq.read_table(tmp)
out["roundtrip"] = t.to_pydict()
os.remove(tmp)
zoneinfo.ZoneInfo("Africa/Casablanca")
from backend.app import create_app
out["routes"] = len(create_app().routes)
print("PROBE_JSON " + json.dumps(out))
"""

PARQUET_READ = r"""
import json, sys, pyarrow.parquet as pq
t = pq.read_table(sys.argv[1])
print("PARQUET_JSON " + json.dumps({"rows": t.num_rows, "columns": t.column_names}))
"""


class SmokeFailure(RuntimeError):
    pass


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise SmokeFailure(f"CHECK {name}: FAIL {detail}")
    print(f"CHECK {name}: PASS {detail}".rstrip())


def scrubbed_env(storage_dir: Path) -> Dict[str, str]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    keep = ("SystemRoot", "SystemDrive", "windir", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
            "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "ComSpec", "PATHEXT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = os.pathsep.join([str(Path(system_root) / "System32"), system_root,
                                   str(Path(system_root) / "System32" / "Wbem")])
    env["STORAGE_DIR"] = str(storage_dir)
    # webbrowser honours BROWSER: run a no-op instead of opening a real browser in CI.
    env["BROWSER"] = f'"{Path(system_root) / "System32" / "cmd.exe"}" /c rem'
    return env


def run(cmd: List[str], env: Dict[str, str], cwd: Path, timeout: float = 120) -> str:
    result = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SmokeFailure(f"{cmd[0]} exited {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def http(method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
         timeout: float = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def port_closed(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) != 0


def smoke(bundle: Path, work: Path, evidence: Dict[str, object], installed: bool = False) -> Dict[str, object]:
    if sys.platform != "win32":
        raise SmokeFailure("The bundle targets Windows; run this smoke test on Windows.")

    installer_extra = (lambda rel: rel in INSTALLER_FILES) if installed else None
    verified = verify_bundle(bundle, allow_unlisted=installer_extra)
    check("manifest", True, f"{verified['file_count']} files match manifest.json")
    evidence["manifest"] = verified

    if work.exists() and any(work.iterdir()):
        raise SmokeFailure(f"--work-dir must be empty: {work}")
    if installed:
        install = bundle
    else:
        install = work / "Program Files Copy" / "Data Intelligence Lab"
        shutil.copytree(bundle, install)
    storage = work / "user storage"
    storage.mkdir(parents=True)
    env = scrubbed_env(storage)
    py = install / "runtime" / "python" / "python.exe"
    node = install / "node" / "node.exe"
    check("no-crt-files", not [p for p in install.rglob("*") if re.fullmatch(
        r"(vcruntime140(_1)?|msvcp140(_\w+)?|vcomp140|concrt140|ucrtbase)\.dll", p.name, re.I)])

    # 1. Bundled Python
    out = run([str(py), "-c", PYTHON_PROBE, str(install)], env, cwd=install)
    probe = json.loads(next(line for line in out.splitlines() if line.startswith("PROBE_JSON "))[11:])
    evidence["python"] = probe
    check("python-version", probe["version"] == "3.12.10", probe["version"])
    check("python-sys-path-confined", not probe["path_outside_bundle"], probe["path_outside_bundle"])
    system_dir = os.path.realpath(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")).lower()
    crt = probe["crt_loaded_from"]
    check("crt-from-system", bool(crt) and all(os.path.realpath(p).lower().startswith(system_dir) for p in crt.values()), crt)
    check("vendored-imports", probe["pyarrow"] == "18.1.0" and all(probe["absent"].values()),
          f"pyarrow {probe['pyarrow']}, fastapi {probe['fastapi']}, absent {probe['absent']}")
    check("parquet-roundtrip", probe["roundtrip"] == {"x": [1, 2, None], "s": ["a", "", "c"]})

    # 2. Bundled Node.js
    node_version = run([str(node), "--version"], env, cwd=install).strip()
    evidence["node"] = node_version
    check("node-version", node_version == f"v{NODE_VERSION}", node_version)

    # 3. Launcher end to end
    lines: List[str] = []
    proc = subprocess.Popen([str(py), "-u", "launcher.py"], cwd=install, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()
    try:
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
        check("launcher-started", len(ports) == 2, ports)
        evidence["ports"] = ports
        fe = f"http://127.0.0.1:{ports['frontend']}"

        status, body = http("GET", fe + "/")
        check("ui-served", status == 200 and b"Data Intelligence Lab" in body, f"HTTP {status}, {len(body)} bytes")
        status, body = http("GET", fe + "/health")
        check("proxied-health", status == 200, f"HTTP {status} {body[:120]!r}")
        status, body = http("GET", fe + "/ready")
        check("proxied-ready", status == 200, f"HTTP {status} {body[:200]!r}")

        boundary = uuid.uuid4().hex
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"smoke.csv\"\r\n"
            f"Content-Type: text/csv\r\n\r\n{SAMPLE_CSV}\r\n--{boundary}--\r\n"
        ).encode()
        status, body = http("POST", fe + "/api/v1/datasets/upload", payload, {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Origin": fe, "Sec-Fetch-Site": "same-origin",
        }, timeout=120)
        check("upload-via-proxy", status == 201, f"HTTP {status} {body[:300]!r}")
        uploaded = json.loads(body)
        evidence["upload"] = uploaded
        check("upload-summary", uploaded.get("row_count") == 4 and uploaded.get("column_count") == 4, uploaded)

        status, body = http("GET", f"{fe}/api/v1/datasets/{uploaded['dataset_id']}")
        check("dataset-lookup", status == 200 and json.loads(body).get("dataset_id") == uploaded["dataset_id"],
              f"HTTP {status}")

        parquet = list(storage.rglob("data.parquet"))
        check("parquet-written", len(parquet) == 1, [str(p) for p in parquet])
        out = run([str(py), "-c", PARQUET_READ, str(parquet[0])], env, cwd=install)
        stored = json.loads(next(line for line in out.splitlines() if line.startswith("PARQUET_JSON "))[13:])
        evidence["parquet"] = stored
        check("parquet-readback", stored["rows"] == 4, stored)

        # 4. Job Object containment: killing the launcher must take both services down.
        proc.kill()
        proc.wait(timeout=30)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(port_closed(p) for p in ports.values()):
            time.sleep(0.5)
        check("job-object-teardown", all(port_closed(p) for p in ports.values()), ports)

        # 5. The app directory is read-only at runtime apart from Next.js's route cache.
        written: List[str] = []

        def runtime_written(rel: str) -> bool:
            if (installed and rel in INSTALLER_FILES) or rel.startswith(RUNTIME_CACHE_PREFIX):
                written.append(rel)
                return True
            return False

        try:
            verify_bundle(install, allow_unlisted=runtime_written)
        except Exception as exc:
            raise SmokeFailure(f"CHECK app-dir-unchanged: FAIL {exc}") from exc
        cache_files = [w for w in written if w.startswith(RUNTIME_CACHE_PREFIX)]
        evidence["runtime_cache_files"] = len(cache_files)
        check("app-dir-unchanged", True, f"only {len(cache_files)} Next.js route-cache file(s) added")
    finally:
        if proc.poll() is None:
            proc.kill()
        evidence["launcher_output"] = "".join(lines)[-8000:]
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--bundle", type=Path, help="staged bundle (tested on a copy)")
    target.add_argument("--installed", type=Path, help="installed application directory (tested in place)")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()
    evidence: Dict[str, object] = {}
    try:
        target = args.installed or args.bundle
        smoke(target.resolve(), args.work_dir.resolve(), evidence, installed=args.installed is not None)
        evidence["result"] = "PASS"
        print("BUNDLE_SMOKE_TEST: PASS")
        return 0
    except Exception as exc:
        evidence["result"] = "FAIL"
        evidence["error"] = str(exc)
        print(f"BUNDLE_SMOKE_TEST: FAIL\n{exc}", file=sys.stderr)
        return 1
    finally:
        if args.evidence:
            args.evidence.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
