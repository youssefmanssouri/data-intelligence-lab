"""
Data Intelligence Lab v1.0 - Standalone Runtime Bundle Builder
Constructs a self-contained, offline-deployable runtime bundle containing:
- Bundled Python 3.14 64-bit embeddable runtime with site-packages
- Bundled Node.js 64-bit standalone executable
- Application source files (backend, core, frontend scripts/distribution, launcher.py)
- Cryptographic SHA-256 file manifest (manifest.json)
- Strict CRT redistribution gate preventing unauthorized Microsoft CRT licensing violations
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Set, Tuple
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Cryptographic and Packaging Constants
# ---------------------------------------------------------------------------

# The bundled interpreter. Everything that depends on its version derives from these.
PYTHON_VERSION = "3.14.10"
PYTHON_MAJOR_MINOR = (3, 14)
PYTHON_TAG = "314"  # python314.dll, python314.zip, *.cpython-314.pyc
PYTHON_TARGET = f"cp{PYTHON_TAG}-win_amd64"  # wheelhouse target (prepare_wheelhouse.py)
PYTHON_EMBED_FILE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_EMBED_FILE}"
# SHA-256 of the file published at PYTHON_EMBED_URL. python.org signs 3.14 releases with Sigstore
# (no MD5 list); the runtime-bundle workflow verifies {PYTHON_EMBED_URL}.sigstore against the
# release manager's identity before building, and this pin then holds the file fixed.
PYTHON_EMBED_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"

NODE_VERSION = "22.23.5"
NODE_EXE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/win-x64/node.exe"
# Matches https://nodejs.org/dist/v22.23.5/SHASUMS256.txt ("win-x64/node.exe") and the downloaded file.
# Node 22 is the major CI tests on and satisfies frontend/package.json "engines" (>=22.14.0);
# node.exe links its C runtime statically and imports no Microsoft CRT DLL.
NODE_EXE_SHA256 = "247e9693d316da9857e44990e5e11709f596f18112251b8026ec6eed80bdf38a"
# Node.js's MIT license (with its bundled dependencies' notices) must travel with node.exe.
# Pinned to the v22.23.5 tag; identical (after CRLF normalization) to the LICENSE inside the
# official node-v22.23.5-win-x64.zip, whose SHA-256 is NODE_ZIP_SHA256 in the CI workflows.
NODE_LICENSE_URL = f"https://raw.githubusercontent.com/nodejs/node/v{NODE_VERSION}/LICENSE"
NODE_LICENSE_SHA256 = "c738ae413cf561f174e34f6961f8ca458aae2369a73640dda6234c629b98bcc4"

# The embeddable package ships these two Microsoft runtime DLLs. They are never staged:
# redistribution is not authorized, and the VC++ 2015-2022 Redistributable (x64) is a
# documented installation prerequisite that provides them.
PYTHON_EMBED_EXCLUDED = frozenset({"vcruntime140.dll", "vcruntime140_1.dll"})

# The embeddable runtime reads sys.path only from this file (PYTHONPATH is ignored).
# Paths are relative to runtime/python/: the stdlib zip, the runtime directory, the
# vendored packages, and the bundle root (so `python -m backend.main` finds `backend`).
PYTHON_PTH_NAME = f"python{PYTHON_TAG}._pth"
PYTHON_PTH_CONTENT = f"python{PYTHON_TAG}.zip\n.\nLib\\site-packages\n..\\..\nimport site\n"

# Files with PE images the CRT gate inspects. .pyd and .node files are DLLs.
PE_SUFFIXES = (".dll", ".pyd", ".node", ".exe")

# Build-machine-only parts of the Next.js output that the production server never reads.
NEXT_EXCLUDED = ("cache", "trace", "trace-build", "diagnostics", "types")
FRONTEND_FILES = ("next.config.mjs", "package.json", "package-lock.json")
FRONTEND_DIRS = ("config", "scripts", "public")

CRT_DLL_NAMES = frozenset({
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcomp140.dll",
})

CRT_PREFIXES = ("msvcp140-", "vcruntime140-", "vcruntime140_1-", "vcomp140-")

MSFT_UTF16 = b"M\x00i\x00c\x00r\x00o\x00s\x00o\x00f\x00t\x00 \x00C\x00o\x00r\x00p\x00o\x00r\x00a\x00t\x00i\x00o\x00n"
MSFT_ASCII = b"Microsoft Corporation"
VS_UTF16 = b"V\x00i\x00s\x00u\x00a\x00l\x00 \x00S\x00t\x00u\x00d\x00i\x00o"


def classify_dll(path: Path) -> Tuple[str, Dict[str, object]]:
    """
    Classifies a DLL by inspecting PE header and binary metadata markers.
    Returns (classification, details_dict):
    - 'msvc_crt_direct': Standard Microsoft CRT binary (e.g. msvcp140.dll, vcruntime140.dll)
    - 'msvc_crt_renamed': Versioned or hash-renamed Microsoft CRT binary (e.g. delvewheel msvcp140-*.dll)
    - 'third_party_non_crt': Non-Microsoft open-source or third-party binary (e.g. arrow.dll, openblas.dll)
    - 'unknown': Ambiguous, unclassifiable, or non-PE DLL (triggers fail-closed gate in release mode)
    """
    if not path.is_file():
        return "unknown", {"path": path.as_posix(), "error": "file_not_found"}

    try:
        data = path.read_bytes()
    except Exception as exc:
        return "unknown", {"path": path.as_posix(), "error": f"read_error: {exc}"}

    if not data.startswith(b"MZ"):
        return "unknown", {"path": path.as_posix(), "error": "invalid_pe_header"}

    name_lower = path.name.lower()
    has_msft_marker = (MSFT_UTF16 in data) or (MSFT_ASCII in data)
    has_vs_marker = VS_UTF16 in data

    is_direct_name = name_lower in CRT_DLL_NAMES
    is_renamed_pattern = any(name_lower.startswith(p) and name_lower.endswith(".dll") for p in CRT_PREFIXES)

    sha256_hash = hashlib.sha256(data).hexdigest()
    details: Dict[str, object] = {
        "path": path.as_posix(),
        "name": path.name,
        "size": len(data),
        "sha256": sha256_hash,
        "has_msft_marker": has_msft_marker,
        "has_vs_marker": has_vs_marker,
    }

    if has_msft_marker and (is_direct_name or is_renamed_pattern or has_vs_marker):
        if is_direct_name:
            return "msvc_crt_direct", details
        elif is_renamed_pattern:
            return "msvc_crt_renamed", details
        else:
            return "unknown", details

    # If it matches CRT name pattern but lacks Microsoft Corporation markers, it is ambiguous -> unknown
    if is_direct_name or is_renamed_pattern:
        return "unknown", details

    # Valid PE that does not have Microsoft CRT signatures
    return "third_party_non_crt", details


def compute_sha256(path: Path) -> str:
    """Computes hexadecimal lowercase SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_file_hash(path: Path, expected_sha256: str) -> bool:
    """Verifies that a file matches an expected SHA-256 hash."""
    if not path.is_file():
        return False
    actual = compute_sha256(path)
    return actual.lower() == expected_sha256.lower()


def download_with_hash_check(url: str, dest_path: Path, expected_sha256: str) -> Path:
    """Downloads a file if absent or invalid, enforcing cryptographic integrity."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.is_file():
        if verify_file_hash(dest_path, expected_sha256):
            return dest_path
        dest_path.unlink()

    print(f"Downloading {url} ...")
    temp_dest = dest_path.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, temp_dest)
        actual = compute_sha256(temp_dest)
        if actual.lower() != expected_sha256.lower():
            raise ValueError(
                f"SHA-256 mismatch for {url}: expected {expected_sha256}, got {actual}"
            )
        temp_dest.replace(dest_path)
    finally:
        if temp_dest.exists():
            temp_dest.unlink()
    return dest_path


def build_manifest(bundle_dir: Path) -> Dict[str, object]:
    """
    Generates a deterministic, sorted, validated file manifest for the bundle.
    Rejects path traversal, absolute paths, and case-folding collisions fail-closed.
    """
    manifest_entries: List[Dict[str, object]] = []
    seen_paths_lower: Set[str] = set()

    for root, _, files in os.walk(bundle_dir):
        for f in files:
            full_path = Path(root) / f
            rel_path = full_path.relative_to(bundle_dir).as_posix()

            # Path validation
            if rel_path.startswith("/") or rel_path.startswith("\\") or ":" in rel_path:
                raise ValueError(f"Absolute path in bundle rejected: {rel_path}")
            if ".." in rel_path.split("/"):
                raise ValueError(f"Path traversal token in bundle rejected: {rel_path}")

            rel_lower = rel_path.lower()
            if rel_lower in seen_paths_lower:
                raise ValueError(f"Case-insensitive collision detected in bundle: {rel_path}")
            seen_paths_lower.add(rel_lower)

            size = full_path.stat().st_size
            sha256_hash = compute_sha256(full_path)
            manifest_entries.append({
                "path": rel_path,
                "size": size,
                "sha256": sha256_hash,
            })

    manifest_entries.sort(key=lambda item: item["path"])
    return {
        "version": "1.1.0",
        "file_count": len(manifest_entries),
        "total_bytes": sum(int(e["size"]) for e in manifest_entries),
        "files": manifest_entries,
    }


def enforce_crt_redistribution_gate(
    bundle_dir: Path,
    mode: str = "release",
    allow_crt_redistribution: bool = False,
    crt_authorization_file: Optional[Path] = None,
) -> Dict[str, object]:
    """
    Enforces recursive inspection and legal redistribution gating for Microsoft CRT binaries.

    In release mode:
    1. Recursively scans EVERY PE image (.dll, .pyd, .node, .exe) across the entire bundle_dir hierarchy.
    2. Identifies and classifies direct CRT DLLs, versioned/privately renamed CRT DLLs,
       and non-CRT binaries via PE metadata and string markers.
    3. Fails closed unconditionally if any root-level CRT DLL is detected (loose root CRT
       is strictly forbidden by architecture, even if authorized).
    4. Fails closed if any DLL cannot be classified ('unknown').
    5. For package-vendored Microsoft CRT binaries (direct or renamed):
       - Fails closed if allow_crt_redistribution is False.
       - If allow_crt_redistribution is True:
         - Requires a valid crt_authorization_file.
         - Verifies that all detected CRT binaries are explicitly listed in approved_hashes
           or approved_paths in the authorization record.
         - Fails closed if authorization file is missing, invalid, or omits any detected CRT binary.
    """
    if mode != "release":
        # In non-release/development modes, scan for informational reporting only
        return {"mode": mode, "status": "development_bypass"}

    all_dlls: List[Path] = []
    for root, _, files in os.walk(bundle_dir):
        for f in files:
            if f.lower().endswith(PE_SUFFIXES):
                all_dlls.append(Path(root) / f)

    classified_crt: List[Tuple[str, Path, Dict[str, object]]] = []
    unknown_dlls: List[Tuple[Path, Dict[str, object]]] = []
    third_party_dlls: List[Tuple[Path, Dict[str, object]]] = []
    root_crt_detected: List[str] = []

    for dll_path in all_dlls:
        rel_path = dll_path.relative_to(bundle_dir).as_posix()
        cls, details = classify_dll(dll_path)
        details["rel_path"] = rel_path

        # Check if root-level CRT DLL (placed in root or immediate python root, not in site-packages/node_modules)
        is_root_level = not ("site-packages" in rel_path or "node_modules" in rel_path)

        if cls in ("msvc_crt_direct", "msvc_crt_renamed"):
            if is_root_level and dll_path.name.lower() in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll"):
                root_crt_detected.append(rel_path)
            classified_crt.append((cls, dll_path, details))
        elif cls == "unknown":
            unknown_dlls.append((dll_path, details))
        else:
            third_party_dlls.append((dll_path, details))

    # 1. Root-level msvcp140.dll is NEVER approved under any condition
    if root_crt_detected:
        raise RuntimeError(
            f"Microsoft CRT redistribution gate blocked release packaging!\n"
            f"Prohibited root-level CRT binary detected: {', '.join(root_crt_detected)}.\n"
            "Architecture rule: Root-level loose CRT DLLs are strictly forbidden. "
            "Data Intelligence Lab relies exclusively on wheel-vendored internal CRT copies."
        )

    # 2. Unknown or unclassifiable DLLs fail closed
    if unknown_dlls:
        unknown_paths = [str(d["rel_path"]) for _, d in unknown_dlls]
        raise RuntimeError(
            f"Microsoft CRT redistribution gate blocked release packaging!\n"
            f"Unclassifiable or ambiguous DLL detected: {', '.join(unknown_paths)}.\n"
            "Release gate requires 100% conclusive identification of all binaries. "
            "Inspect PE headers and version information."
        )

    # 3. If CRT binaries are detected, check authorization
    if classified_crt:
        crt_rel_paths = [str(d["rel_path"]) for _, _, d in classified_crt]
        if not allow_crt_redistribution:
            raise RuntimeError(
                f"Microsoft CRT redistribution gate blocked release packaging!\n"
                f"Detected {len(classified_crt)} Microsoft CRT binary/binaries in staged runtime:\n"
                f"  {', '.join(crt_rel_paths)}\n"
                "Redistribution of Microsoft Visual C++ Runtime binaries requires valid Microsoft licensing "
                "and adherence to REDIST.TXT terms.\n"
                "To authorize redistribution of upstream wheel-vendored CRT binaries, pass "
                "--allow-crt-redistribution alongside --crt-authorization-file=<path_to_json>."
            )

        # allow_crt_redistribution is True: require valid explicit authorization record
        if not crt_authorization_file or not Path(crt_authorization_file).is_file():
            raise RuntimeError(
                "Microsoft CRT redistribution gate blocked release packaging!\n"
                "The bypass flag --allow-crt-redistribution was supplied without a valid explicit authorization record.\n"
                "Please specify --crt-authorization-file=<path_to_record.json> containing Project Owner release approval."
            )

        try:
            auth_data = json.loads(Path(crt_authorization_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Microsoft CRT redistribution gate blocked release packaging!\n"
                f"Failed to parse CRT authorization file '{crt_authorization_file}': {exc}"
            )

        approved_hashes: Set[str] = {str(h).lower() for h in auth_data.get("approved_hashes", [])}
        approved_paths: Set[str] = {str(p).lower() for p in auth_data.get("approved_paths", [])}

        unauthorized_crt: List[str] = []
        for cls, dll_path, details in classified_crt:
            file_hash = str(details["sha256"]).lower()
            rel_path = str(details["rel_path"]).lower()
            if file_hash not in approved_hashes and rel_path not in approved_paths:
                unauthorized_crt.append(str(details["rel_path"]))

        if unauthorized_crt:
            raise RuntimeError(
                f"Microsoft CRT redistribution gate blocked release packaging!\n"
                f"The following detected CRT binary/binaries are NOT authorized in '{crt_authorization_file}':\n"
                f"  {', '.join(unauthorized_crt)}\n"
                "Every redistributable Microsoft CRT binary must be explicitly audited and approved."
            )

    return {
        "mode": mode,
        "scanned_dlls": len(all_dlls),
        "crt_binaries": len(classified_crt),
        "third_party_binaries": len(third_party_dlls),
        "status": "approved",
    }


class BundleError(RuntimeError):
    """Raised when the bundle cannot be staged to release standards."""


def verify_wheelhouse(wheelhouse: Path, lock_path: Path) -> Dict[str, object]:
    """
    Checks a wheelhouse produced by prepare_wheelhouse.py against the current lock:
    the manifest must name this exact lock, and the directory must hold exactly the
    manifested wheels with matching SHA-256 hashes.
    """
    manifest_path = wheelhouse / "wheelhouse-manifest.json"
    if not manifest_path.is_file():
        raise BundleError(f"Wheelhouse manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("target") != PYTHON_TARGET:
        raise BundleError(f"Wheelhouse targets {manifest.get('target')!r}, expected {PYTHON_TARGET!r}")
    if manifest.get("lock_sha256") != compute_sha256(lock_path):
        raise BundleError("Wheelhouse was built from a different requirements-vendor.txt; rebuild it")
    expected = {w["file"]: w["sha256"] for w in manifest.get("wheels", [])}
    present = {p.name for p in wheelhouse.iterdir() if p.name != manifest_path.name}
    if present != set(expected):
        raise BundleError(
            f"Wheelhouse contents differ from its manifest: "
            f"missing {sorted(set(expected) - present)}, unexpected {sorted(present - set(expected))}"
        )
    for name, sha in expected.items():
        if not verify_file_hash(wheelhouse / name, sha):
            raise BundleError(f"Wheelhouse wheel hash mismatch: {name}")
    return manifest


def install_vendor_packages(wheelhouse: Path, lock_path: Path, site_packages: Path) -> None:
    """Installs the locked closure offline, hash-checked, for the bundled interpreter (PYTHON_TARGET)."""
    site_packages.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-index", "--find-links", str(wheelhouse),
        "--require-hashes", "-r", str(lock_path),
        "--target", str(site_packages),
        "--only-binary=:all:", "--platform", "win_amd64", "--python-version", ".".join(map(str, PYTHON_MAJOR_MINOR)),
        "--implementation", "cp", "--abi", f"cp{PYTHON_TAG}",
        "--no-compile", "--no-warn-script-location", "--disable-pip-version-check",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BundleError(f"Vendored package install failed:\n{result.stdout}{result.stderr}")
    # Console-script launchers embed the build machine's interpreter path; the app never uses them.
    for scripts_dir in ("bin", "Scripts"):
        shutil.rmtree(site_packages / scripts_dir, ignore_errors=True)


def stage_python_runtime(python_dest_dir: Path, embed_zip: Path) -> List[str]:
    """Extracts the embeddable runtime without its Microsoft CRT DLLs and writes the ._pth."""
    excluded: List[str] = []
    with zipfile.ZipFile(embed_zip, "r") as z:
        for member in z.infolist():
            name = member.filename
            if name.endswith("/"):
                continue
            if "/" in name or "\\" in name or name.startswith(".."):
                raise BundleError(f"Unexpected nested path in embeddable package: {name}")
            if name.lower() in PYTHON_EMBED_EXCLUDED:
                excluded.append(name)
                continue
            z.extract(member, python_dest_dir)
    if sorted(n.lower() for n in excluded) != sorted(PYTHON_EMBED_EXCLUDED):
        raise BundleError(f"Embeddable package layout changed; excluded only {excluded}")
    if not (python_dest_dir / PYTHON_PTH_NAME).is_file():
        raise BundleError(f"{PYTHON_PTH_NAME} missing from the embeddable package")
    (python_dest_dir / PYTHON_PTH_NAME).write_text(PYTHON_PTH_CONTENT, encoding="utf-8")
    return excluded


def stage_frontend(fe_src: Path, fe_dest: Path, npm_command: List[str]) -> str:
    """
    Stages the production frontend: the built .next output (without build-machine
    caches), the custom server and its configuration, and production node_modules
    installed from package-lock.json for win32-x64 with install scripts disabled.
    Returns the Next.js BUILD_ID.
    """
    build_id_file = fe_src / ".next" / "BUILD_ID"
    if not build_id_file.is_file():
        raise BundleError(f"Frontend is not built ({build_id_file} missing); run `npm ci && npm run build` first")
    build_id = build_id_file.read_text(encoding="utf-8").strip()

    fe_dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fe_src / ".next", fe_dest / ".next", ignore=shutil.ignore_patterns(*NEXT_EXCLUDED))
    for name in FRONTEND_FILES:
        if not (fe_src / name).is_file():
            raise BundleError(f"Frontend file missing: {fe_src / name}")
        shutil.copy2(fe_src / name, fe_dest / name)
    for name in FRONTEND_DIRS:
        if (fe_src / name).is_dir():
            shutil.copytree(fe_src / name, fe_dest / name)
    if not (fe_dest / "scripts" / "server.mjs").is_file():
        raise BundleError("Frontend entry point scripts/server.mjs missing")

    cmd = [*npm_command, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund",
           "--os=win32", "--cpu=x64"]
    result = subprocess.run(cmd, cwd=fe_dest, capture_output=True, text=True)
    if result.returncode != 0:
        raise BundleError(f"Production npm install failed:\n{result.stdout}{result.stderr}")
    # Command shims and symlinks for CLI tools; the server imports packages directly.
    shutil.rmtree(fe_dest / "node_modules" / ".bin", ignore_errors=True)
    for required in ("next", "react", "react-dom", "lossless-json"):
        if not (fe_dest / "node_modules" / required / "package.json").is_file():
            raise BundleError(f"Production dependency missing after npm ci: {required}")
    return build_id


def precompile_bytecode(bundle_dir: Path, roots: List[Path]) -> int:
    """
    Compiles every .py file under ``roots`` to bytecode for the bundled CPython version with
    unchecked-hash invalidation, so the installed application never writes
    __pycache__ into its install directory and file timestamps do not matter.
    Recorded source paths are made relative to the bundle (no build-machine paths).
    """
    if sys.version_info[:2] != PYTHON_MAJOR_MINOR:
        raise BundleError(
            f"Bytecode must be compiled by CPython {'.'.join(map(str, PYTHON_MAJOR_MINOR))} to match the bundled runtime "
            f"(build interpreter is {sys.version.split()[0]})"
        )
    for root in roots:
        ok = compileall.compile_dir(
            str(root), quiet=1, stripdir=str(bundle_dir), workers=1,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
        if not ok:
            raise BundleError(f"Bytecode compilation failed under {root}")
    return sum(1 for root in roots for _ in root.rglob(f"*.cpython-{PYTHON_TAG}.pyc"))


def _default_npm_command() -> List[str]:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise BundleError("npm is required to stage the frontend's production dependencies")
    return [npm]


def stage_bundle(
    repo_root: Path,
    output_dir: Path,
    mode: str = "release",
    allow_crt_redistribution: bool = False,
    crt_authorization_file: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    skip_download: bool = False,
    wheelhouse: Optional[Path] = None,
    npm_command: Optional[List[str]] = None,
) -> Path:
    """
    Stages the complete standalone runtime bundle into output_dir.

    Release mode fails closed: every runtime is hash-verified, the vendored packages
    and the built frontend are required, and nothing is ever replaced by a placeholder.
    Development mode may stage placeholders so the layout can be inspected offline.
    """
    release = mode == "release"
    if release and skip_download:
        raise BundleError("--skip-download is not allowed in release mode")
    if release and wheelhouse is None:
        raise BundleError("--wheelhouse is required in release mode")

    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache = cache_dir or (Path(tempfile.gettempdir()) / "dil_packaging_cache")
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = repo_root / "packaging" / "requirements-vendor.txt"
    runtime_info: Dict[str, object] = {"mode": mode}

    # 1. Application components
    print(f"Staging application files into {output_dir} ...")
    for src_dir in ("backend", "core"):
        src_path = repo_root / src_dir
        if not src_path.is_dir():
            raise BundleError(f"Application package missing: {src_path}")
        shutil.copytree(src_path, output_dir / src_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f_name in ("launcher.py", "LICENSE", "NOTICE"):
        f_path = repo_root / f_name
        if f_path.is_file():
            shutil.copy2(f_path, output_dir / f_name)
        elif f_name == "launcher.py":
            raise BundleError(f"Launcher missing: {f_path}")

    # 2. Frontend
    fe_src = repo_root / "frontend"
    fe_dest = output_dir / "frontend"
    if release or (fe_src / ".next" / "BUILD_ID").is_file():
        runtime_info["frontend_build_id"] = stage_frontend(fe_src, fe_dest, npm_command or _default_npm_command())
    else:
        for sub in ("config", "scripts"):
            if (fe_src / sub).is_dir():
                shutil.copytree(fe_src / sub, fe_dest / sub)

    # 3. Node.js standalone executable (hash-pinned)
    node_dest = output_dir / "node" / "node.exe"
    node_dest.parent.mkdir(parents=True, exist_ok=True)
    local_node = repo_root / "node" / "node.exe"
    if local_node.is_file() and verify_file_hash(local_node, NODE_EXE_SHA256):
        shutil.copy2(local_node, node_dest)
    elif local_node.is_file() and release:
        raise BundleError(f"{local_node} does not match the pinned Node.js {NODE_VERSION} SHA-256")
    elif not skip_download:
        cached_node = cache / f"node-v{NODE_VERSION}-win-x64.exe"
        download_with_hash_check(NODE_EXE_URL, cached_node, NODE_EXE_SHA256)
        shutil.copy2(cached_node, node_dest)
    else:
        node_dest.write_text("# development placeholder: not a Node.js runtime", encoding="utf-8")
    if release and not verify_file_hash(node_dest, NODE_EXE_SHA256):
        raise BundleError("Staged node.exe does not match the pinned SHA-256")
    if not skip_download:
        cached_license = cache / f"node-v{NODE_VERSION}-LICENSE"
        download_with_hash_check(NODE_LICENSE_URL, cached_license, NODE_LICENSE_SHA256)
        shutil.copy2(cached_license, node_dest.parent / "LICENSE")
    runtime_info["node"] = {"version": NODE_VERSION, "sha256": NODE_EXE_SHA256}

    # 4. Python embeddable runtime (hash-pinned, without Microsoft CRT DLLs)
    python_dest_dir = output_dir / "runtime" / "python"
    python_dest_dir.mkdir(parents=True, exist_ok=True)
    if not skip_download:
        cached_py = cache / PYTHON_EMBED_FILE
        download_with_hash_check(PYTHON_EMBED_URL, cached_py, PYTHON_EMBED_SHA256)
        runtime_info["python_excluded_files"] = stage_python_runtime(python_dest_dir, cached_py)
    else:
        (python_dest_dir / "python.exe").write_text("# development placeholder: not a Python runtime", encoding="utf-8")
    runtime_info["python"] = {"version": PYTHON_VERSION, "embed_sha256": PYTHON_EMBED_SHA256}

    # 5. Vendored backend dependencies
    if wheelhouse is not None:
        wh_manifest = verify_wheelhouse(wheelhouse, lock_path)
        install_vendor_packages(wheelhouse, lock_path, python_dest_dir / "Lib" / "site-packages")
        runtime_info["vendor"] = {
            "lock_sha256": wh_manifest["lock_sha256"],
            "wheels": [{"file": w["file"], "sha256": w["sha256"]} for w in wh_manifest["wheels"]],
        }

    # 6. Precompiled bytecode for the application and vendored packages
    if release:
        compile_roots = [output_dir / "backend", output_dir / "core"]
        site_packages = python_dest_dir / "Lib" / "site-packages"
        if site_packages.is_dir():
            compile_roots.append(site_packages)
        runtime_info["bytecode_files"] = precompile_bytecode(output_dir, compile_roots)

    # 7. Microsoft CRT redistribution gate
    gate = enforce_crt_redistribution_gate(
        output_dir,
        mode=mode,
        allow_crt_redistribution=allow_crt_redistribution,
        crt_authorization_file=crt_authorization_file,
    )
    runtime_info["crt_gate"] = gate

    # 8. Manifest of every staged file
    manifest = build_manifest(output_dir)
    manifest["runtime"] = runtime_info
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Bundle successfully created at {output_dir} ({manifest['file_count']} files, {manifest['total_bytes']} bytes).")
    return output_dir


def verify_bundle(bundle_dir: Path, allow_unlisted: Optional[Callable[[str], bool]] = None) -> Dict[str, object]:
    """
    Re-verifies a staged bundle independently of how it was built: every file on disk
    is in manifest.json with a matching size and SHA-256, and nothing else is present
    (except paths ``allow_unlisted`` accepts, e.g. files an installer adds).
    """
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    listed = {e["path"]: e for e in manifest["files"]}
    on_disk = {
        (Path(root) / f).relative_to(bundle_dir).as_posix()
        for root, _, files in os.walk(bundle_dir)
        for f in files
    } - {"manifest.json"}
    if allow_unlisted is not None:
        on_disk = {p for p in on_disk if p in listed or not allow_unlisted(p)}
    if on_disk != set(listed):
        raise BundleError(
            f"Bundle differs from manifest: unlisted {sorted(on_disk - set(listed))[:10]}, "
            f"missing {sorted(set(listed) - on_disk)[:10]}"
        )
    for rel, entry in listed.items():
        path = bundle_dir / rel
        if path.stat().st_size != entry["size"] or compute_sha256(path) != entry["sha256"]:
            raise BundleError(f"Bundle file does not match manifest: {rel}")
    return {"file_count": len(listed), "total_bytes": manifest["total_bytes"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Data Intelligence Lab Runtime Bundle Builder")
    parser.add_argument("--repo-root", default=None, help="Path to repository root")
    parser.add_argument("--output-dir", default=None, help="Destination directory for bundle")
    parser.add_argument("--mode", default="release", choices=["development", "release"])
    parser.add_argument("--wheelhouse", default=None, help="Verified wheelhouse from prepare_wheelhouse.py (required in release mode)")
    parser.add_argument("--allow-crt-redistribution", action="store_true", help="Authorize CRT redistribution")
    parser.add_argument("--crt-authorization-file", default=None, help="Path to JSON file with CRT redistribution approval")
    parser.add_argument("--skip-download", action="store_true", help="Development only: stage placeholders instead of downloading runtimes")
    parser.add_argument("--cache-dir", default=None, help="Directory for cached runtime archives")
    parser.add_argument("--verify", default=None, metavar="BUNDLE_DIR", help="Only re-verify an existing bundle against its manifest")
    args = parser.parse_args()

    try:
        if args.verify:
            result = verify_bundle(Path(args.verify).resolve())
            print(f"Bundle verified: {result['file_count']} files match manifest.json.")
            return 0
        repo_root = Path(args.repo_root or Path(__file__).resolve().parent.parent).resolve()
        out_dir = Path(args.output_dir) if args.output_dir else (Path(tempfile.gettempdir()) / "dil_bundle_output")
        stage_bundle(
            repo_root=repo_root,
            output_dir=out_dir,
            mode=args.mode,
            allow_crt_redistribution=args.allow_crt_redistribution,
            crt_authorization_file=Path(args.crt_authorization_file) if args.crt_authorization_file else None,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            skip_download=args.skip_download,
            wheelhouse=Path(args.wheelhouse).resolve() if args.wheelhouse else None,
        )
        return 0
    except Exception as exc:
        print(f"[FATAL] {'Bundle verification' if args.verify else 'Bundle creation'} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
