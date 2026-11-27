"""
Data Intelligence Lab v1.0 - Verified Wheelhouse Preparation

Builds the offline wheelhouse that the runtime bundle installs from, for
CPython 3.14 on 64-bit Windows, from packaging/requirements-vendor.txt:

1. Every package except pyarrow is downloaded from PyPI with --require-hashes.
2. pyarrow is never downloaded. It must be supplied as a local file (the
   source-built, CRT-free wheel published as a build-input pre-release) and
   must match the hash pinned in the lock; the PyPI wheel cannot match it.
3. The complete lock is then resolved offline from the wheelhouse alone
   (--no-index --require-hashes), which proves the closure is complete and
   every file matches its pin.
4. Every wheel is scanned for bundled Microsoft CRT files; any hit fails.
5. wheelhouse-manifest.json records each file's SHA-256.

Any failure exits non-zero and leaves no manifest. Nothing is installed.

Usage:
    python packaging/prepare_wheelhouse.py --pyarrow-wheel <path.whl> --output-dir <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Tuple
import zipfile

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from build_runtime_bundle import (  # noqa: E402
    CRT_DLL_NAMES, CRT_PREFIXES, PYTHON_MAJOR_MINOR, PYTHON_TAG, PYTHON_TARGET, compute_sha256,
)

LOCK_PATH = _HERE / "requirements-vendor.txt"
LOCAL_ONLY_PACKAGE = "pyarrow"
# The interpreter the wheelhouse is for: the bundled CPython embeddable runtime.
TARGET_PYTHON_VERSION = ".".join(map(str, PYTHON_MAJOR_MINOR))
TARGET_ABI = f"cp{PYTHON_TAG}"
TARGET = PYTHON_TARGET
TARGET_PIP_ARGS = (
    "--only-binary=:all:",
    "--platform", "win_amd64",
    "--python-version", TARGET_PYTHON_VERSION,
    "--implementation", "cp",
    "--abi", TARGET_ABI,
)
# Additional Microsoft runtime files that must never be bundled (beyond the bundler's list).
EXTRA_CRT_PREFIXES = ("api-ms-win-crt-", "ucrtbase", "concrt140", "vccorlib140", "msvcr1")

_ENTRY_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\\s*\n\s*--hash=sha256:([0-9a-f]{64})\s*$", re.M
)


class WheelhouseError(RuntimeError):
    """Raised when any wheelhouse verification step fails."""


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> Dict[str, Tuple[str, str]]:
    """Returns {normalized name: (version, sha256)}; rejects duplicates and unparsed entries."""
    entries: Dict[str, Tuple[str, str]] = {}
    for name, version, sha in _ENTRY_RE.findall(text):
        key = normalize(name)
        if key in entries:
            raise WheelhouseError(f"Duplicate lock entry for {name}")
        entries[key] = (version, sha)
    requirement_lines = [ln for ln in text.splitlines() if re.match(r"^[A-Za-z0-9_.-]+==", ln)]
    if len(requirement_lines) != len(entries):
        raise WheelhouseError("Lock contains entries that are not 'name==version \\ --hash=sha256:<64 hex>'")
    if LOCAL_ONLY_PACKAGE not in entries:
        raise WheelhouseError(f"Lock has no {LOCAL_ONLY_PACKAGE} entry")
    return entries


def verify_local_wheel(path: Path, version: str, expected_sha256: str) -> None:
    """The local pyarrow wheel must be the exact pinned file for the target (cp314-win_amd64)."""
    expected_name = f"{LOCAL_ONLY_PACKAGE}-{version}-{TARGET_ABI}-{TARGET_ABI}-win_amd64.whl"
    if not path.is_file():
        raise WheelhouseError(f"{LOCAL_ONLY_PACKAGE} wheel not found: {path}")
    if path.name != expected_name:
        raise WheelhouseError(f"Expected {expected_name}, got {path.name}")
    actual = compute_sha256(path)
    if actual != expected_sha256:
        raise WheelhouseError(
            f"{path.name} SHA-256 {actual} does not match the lock pin {expected_sha256}"
        )


def crt_members(wheel: Path) -> List[str]:
    """Names of Microsoft CRT files bundled inside a wheel."""
    hits = []
    with zipfile.ZipFile(wheel) as zf:
        for member in zf.namelist():
            base = member.rsplit("/", 1)[-1].lower()
            if (
                base in CRT_DLL_NAMES
                or base.startswith(CRT_PREFIXES)
                or (base.endswith(".dll") and base.startswith(EXTRA_CRT_PREFIXES))
            ):
                hits.append(member)
    return hits


def _pip(args: List[str]) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise WheelhouseError(f"pip failed ({result.returncode}): {' '.join(args)}\n{result.stdout}{result.stderr}")


def prepare_wheelhouse(pyarrow_wheel: Path, output_dir: Path, lock_path: Path = LOCK_PATH) -> Dict[str, object]:
    lock_text = lock_path.read_text(encoding="utf-8")
    entries = parse_lock(lock_text)
    pa_version, pa_sha = entries[LOCAL_ONLY_PACKAGE]
    verify_local_wheel(pyarrow_wheel, pa_version, pa_sha)

    if output_dir.exists() and any(output_dir.iterdir()):
        raise WheelhouseError(f"Output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dil-wheelhouse-") as tmp:
        tmp_path = Path(tmp)
        # 1. Every package except pyarrow, from PyPI, hash-checked.
        remote = tmp_path / "remote-requirements.txt"
        remote.write_text(
            "".join(
                f"{name}=={ver} --hash=sha256:{sha}\n"
                for name, (ver, sha) in sorted(entries.items())
                if name != LOCAL_ONLY_PACKAGE
            ),
            encoding="utf-8",
        )
        _pip(["download", "--require-hashes", "--no-deps", *TARGET_PIP_ARGS,
              "-r", str(remote), "-d", str(output_dir)])

        # 2. pyarrow only from the verified local file.
        shutil.copy2(pyarrow_wheel, output_dir / pyarrow_wheel.name)

        # 3. Resolve the whole lock offline from the wheelhouse alone.
        _pip(["download", "--no-index", "--find-links", str(output_dir), "--require-hashes",
              *TARGET_PIP_ARGS, "-r", str(lock_path), "-d", str(tmp_path / "offline-check")])

    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != len(entries):
        raise WheelhouseError(f"Expected {len(entries)} wheels, found {len(wheels)}")
    extra = [p.name for p in output_dir.iterdir() if p.suffix != ".whl"]
    if extra:
        raise WheelhouseError(f"Unexpected non-wheel files in wheelhouse: {extra}")

    # 4. No bundled Microsoft CRT anywhere in the closure.
    crt = {w.name: crt_members(w) for w in wheels}
    crt = {k: v for k, v in crt.items() if v}
    if crt:
        raise WheelhouseError(f"Microsoft CRT files bundled in wheels: {crt}")

    # 5. Manifest.
    manifest = {
        "target": TARGET,
        "lock": lock_path.name,
        "lock_sha256": compute_sha256(lock_path),
        "wheel_count": len(wheels),
        "wheels": [{"file": w.name, "sha256": compute_sha256(w), "size": w.stat().st_size} for w in wheels],
    }
    (output_dir / "wheelhouse-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pyarrow-wheel", required=True, type=Path,
                        help="Local CRT-free pyarrow wheel (from the build-input pre-release)")
    parser.add_argument("--output-dir", required=True, type=Path, help="Empty directory for the wheelhouse")
    args = parser.parse_args()
    try:
        manifest = prepare_wheelhouse(args.pyarrow_wheel.resolve(), args.output_dir.resolve())
    except WheelhouseError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print(f"Wheelhouse verified: {manifest['wheel_count']} wheels, no bundled Microsoft CRT, "
          f"manifest at {args.output_dir / 'wheelhouse-manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
