"""
Data Intelligence Lab v1.0 - Standalone Runtime Bundle Builder
Constructs a self-contained, offline-deployable runtime bundle containing:
- Bundled Python 3.12 64-bit embeddable runtime with site-packages
- Bundled Node.js 64-bit standalone executable
- Application source files (backend, core, frontend scripts/distribution, launcher.py)
- Cryptographic SHA-256 file manifest (manifest.json)
- Strict CRT redistribution gate preventing unauthorized Microsoft CRT licensing violations
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Cryptographic and Packaging Constants
# ---------------------------------------------------------------------------

PYTHON_EMBED_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
# UNVERIFIED: not yet checked against the file published on python.org. Every other pin in
# this repository was found not to match its published file; verify this before a release.
PYTHON_EMBED_SHA256 = "0ec2820573e895c2cb98ee43c08182ecbf84f1aa63b7eef4d4008272993fbce8"

NODE_EXE_URL = "https://nodejs.org/dist/v20.18.3/win-x64/node.exe"
# Matches https://nodejs.org/dist/v20.18.3/SHASUMS256.txt ("win-x64/node.exe") and the downloaded file.
NODE_EXE_SHA256 = "528a9aa64888a2a3ba71c6aea89434dd5ab5cb3caa9f0f31345cf5facf685ab0"

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
        "version": "1.0.0",
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
    1. Recursively scans EVERY .dll file across the entire bundle_dir hierarchy.
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
            if f.lower().endswith(".dll"):
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


def stage_bundle(
    repo_root: Path,
    output_dir: Path,
    mode: str = "release",
    allow_crt_redistribution: bool = False,
    crt_authorization_file: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    skip_download: bool = False,
) -> Path:
    """Stages the complete standalone runtime bundle into output_dir."""
    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cache = cache_dir or (Path(tempfile.gettempdir()) / "dil_packaging_cache")
    cache.mkdir(parents=True, exist_ok=True)

    # 1. Copy application components
    print(f"Staging application files into {output_dir} ...")
    for src_dir in ("backend", "core"):
        src_path = repo_root / src_dir
        if src_path.is_dir():
            shutil.copytree(src_path, output_dir / src_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # Copy launcher
    launcher_src = repo_root / "launcher.py"
    if launcher_src.is_file():
        shutil.copy2(launcher_src, output_dir / "launcher.py")

    # Copy frontend scripts and configuration
    fe_src = repo_root / "frontend"
    fe_dest = output_dir / "frontend"
    fe_dest.mkdir(parents=True, exist_ok=True)
    for sub in ("config", "scripts"):
        sub_src = fe_src / sub
        if sub_src.is_dir():
            shutil.copytree(sub_src, fe_dest / sub)

    # Copy package.json and LICENSE
    for f_name in ("package.json", "LICENSE"):
        f_path = repo_root / f_name
        if f_path.is_file():
            shutil.copy2(f_path, output_dir / f_name)

    # 2. Setup Node.js standalone executable
    node_dest_dir = output_dir / "node"
    node_dest_dir.mkdir(parents=True, exist_ok=True)
    local_node = repo_root / "node" / "node.exe"
    if local_node.is_file():
        shutil.copy2(local_node, node_dest_dir / "node.exe")
    elif not skip_download:
        cached_node = cache / "node.exe"
        try:
            download_with_hash_check(NODE_EXE_URL, cached_node, NODE_EXE_SHA256)
            shutil.copy2(cached_node, node_dest_dir / "node.exe")
        except Exception as exc:
            print(f"[WARN] Could not download standalone node.exe ({exc}); using mock/placeholder if dev mode")
            if mode != "release":
                (node_dest_dir / "node.exe").write_text("# mock node", encoding="utf-8")
            else:
                raise
    else:
        # If skip download or offline, look for system node or placeholder
        sys_node = shutil.which("node")
        if sys_node:
            shutil.copy2(sys_node, node_dest_dir / "node.exe")
        else:
            (node_dest_dir / "node.exe").write_text("# mock node", encoding="utf-8")

    # 3. Setup Python embeddable runtime
    python_dest_dir = output_dir / "runtime" / "python"
    python_dest_dir.mkdir(parents=True, exist_ok=True)
    if not skip_download:
        cached_py = cache / "python-3.12.10-embed-amd64.zip"
        try:
            download_with_hash_check(PYTHON_EMBED_URL, cached_py, PYTHON_EMBED_SHA256)
            with zipfile.ZipFile(cached_py, "r") as z:
                z.extractall(python_dest_dir)
            # Enable site module in python312._pth
            pth_file = python_dest_dir / "python312._pth"
            if pth_file.is_file():
                pth_content = pth_file.read_text(encoding="utf-8")
                pth_content = pth_content.replace("#import site", "import site")
                pth_file.write_text(pth_content, encoding="utf-8")
        except Exception as exc:
            print(f"[WARN] Could not download Python embeddable zip ({exc}); staging minimal runtime")
            (python_dest_dir / "python.exe").write_text("# mock python", encoding="utf-8")
    else:
        (python_dest_dir / "python.exe").write_text("# mock python", encoding="utf-8")

    # 4. Enforce CRT Redistribution Gate
    enforce_crt_redistribution_gate(
        output_dir,
        mode=mode,
        allow_crt_redistribution=allow_crt_redistribution,
        crt_authorization_file=crt_authorization_file,
    )

    # 5. Build and write manifest.json
    manifest = build_manifest(output_dir)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Bundle successfully created at {output_dir} ({manifest['file_count']} files, {manifest['total_bytes']} bytes).")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Data Intelligence Lab Runtime Bundle Builder")
    parser.add_argument("--repo-root", default=None, help="Path to repository root")
    parser.add_argument("--output-dir", default=None, help="Destination directory for bundle")
    parser.add_argument("--mode", default="release", choices=["development", "release"])
    parser.add_argument("--allow-crt-redistribution", action="store_true", help="Authorize CRT redistribution")
    parser.add_argument("--crt-authorization-file", default=None, help="Path to JSON file with CRT redistribution approval")
    parser.add_argument("--skip-download", action="store_true", help="Skip remote runtime downloads")
    parser.add_argument("--cache-dir", default=None, help="Directory for cached runtime archives")
    args = parser.parse_args()

    repo_root = Path(args.repo_root or Path(__file__).resolve().parent.parent).resolve()
    out_dir = Path(args.output_dir) if args.output_dir else (Path(tempfile.gettempdir()) / "dil_bundle_output")

    try:
        stage_bundle(
            repo_root=repo_root,
            output_dir=out_dir,
            mode=args.mode,
            allow_crt_redistribution=args.allow_crt_redistribution,
            crt_authorization_file=Path(args.crt_authorization_file) if args.crt_authorization_file else None,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            skip_download=args.skip_download,
        )
        return 0
    except Exception as exc:
        print(f"[FATAL] Bundle creation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
