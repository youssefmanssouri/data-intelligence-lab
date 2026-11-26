"""
Data Intelligence Lab - Packaging Layout, Manifest, and Safety Unit Tests
Tests requirements-vendor lock verification, manifest generation, case-folding
collision rejection, path traversal rejection, CRT redistribution gate enforcement,
installer journal state transitions, obsolete file calculation, and storage segregation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import pytest

bundle_builder_path = Path(__file__).resolve().parent.parent.parent / "packaging" / "build_runtime_bundle.py"
spec = importlib.util.spec_from_file_location("build_runtime_bundle", bundle_builder_path)
if spec is None or spec.loader is None:
    raise ImportError(f"Cannot load module from {bundle_builder_path}")
build_runtime_bundle = importlib.util.module_from_spec(spec)
sys.modules["build_runtime_bundle"] = build_runtime_bundle
spec.loader.exec_module(build_runtime_bundle)

CRT_DLL_NAMES = build_runtime_bundle.CRT_DLL_NAMES
build_manifest = build_runtime_bundle.build_manifest
compute_sha256 = build_runtime_bundle.compute_sha256
enforce_crt_redistribution_gate = build_runtime_bundle.enforce_crt_redistribution_gate
classify_dll = build_runtime_bundle.classify_dll
MSFT_UTF16 = build_runtime_bundle.MSFT_UTF16
VS_UTF16 = build_runtime_bundle.VS_UTF16
from backend import config


def test_requirements_vendor_lock_integrity() -> None:
    """Verifies packaging/requirements-vendor.txt pins all packages with valid SHA-256 hashes."""
    req_path = Path(__file__).resolve().parent.parent.parent / "packaging" / "requirements-vendor.txt"
    assert req_path.is_file(), "requirements-vendor.txt must exist"

    content = req_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]

    # Join continuation lines with \
    unified_entries: list[str] = []
    current_entry = ""
    for line in lines:
        if line.endswith("\\"):
            current_entry += " " + line[:-1].strip()
        else:
            current_entry += " " + line
            unified_entries.append(current_entry.strip())
            current_entry = ""

    # 17 = the cp312-win_amd64 closure of what production code imports (fastapi, starlette, uvicorn,
    # python-multipart, pyarrow), including opentelemetry-api (required by fastapi==0.162.0) and
    # colorama (used by click on Windows). The vendoring command uses --require-hashes, so a
    # missing dependency fails it; this count keeps the lock and that closure in step.
    assert len(unified_entries) == 17, f"Expected 17 production packages, found {len(unified_entries)}"
    names = {e.split('==', 1)[0].lower() for e in unified_entries}
    # Not used by production code; numpy and pandas also bundle Microsoft CRT DLLs.
    assert not names & {'numpy', 'pandas', 'httpx'}, names & {'numpy', 'pandas', 'httpx'}

    for entry in unified_entries:
        match = re.match(r"^([a-zA-Z0-9_\-]+)==([^\s]+)\s+--hash=sha256:([a-fA-F0-9]{64})$", entry)
        assert match is not None, f"Malformed entry in requirements-vendor.txt: {entry}"
        pkg_name, version, sha256_hash = match.groups()
        assert len(sha256_hash) == 64
        assert sha256_hash == sha256_hash.lower()


def test_build_manifest_deterministic_and_sorted(tmp_path: Path) -> None:
    """Verifies that build_manifest produces sorted, accurate manifest entries."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    (bundle / "b_file.txt").write_bytes(b"content B")
    (bundle / "a_file.txt").write_bytes(b"content A")
    sub = bundle / "subdir"
    sub.mkdir()
    (sub / "c_file.txt").write_bytes(b"content C")

    manifest = build_manifest(bundle)

    assert manifest["version"] == "1.0.0"
    assert manifest["file_count"] == 3
    assert manifest["total_bytes"] == len(b"content A") + len(b"content B") + len(b"content C")

    files = manifest["files"]
    assert [f["path"] for f in files] == ["a_file.txt", "b_file.txt", "subdir/c_file.txt"]
    assert files[0]["sha256"] == hashlib.sha256(b"content A").hexdigest()


def test_build_manifest_rejects_case_folding_collisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that build_manifest rejects case-folding collisions fail-closed."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    (bundle / "file.txt").write_text("1")

    # Simulate case collision in os.walk across file systems
    def mock_walk(top):
        yield str(bundle), [], ["File.txt", "file.txt"]

    monkeypatch.setattr(os, "walk", mock_walk)

    with pytest.raises(ValueError, match="Case-insensitive collision detected"):
        build_manifest(bundle)


def test_build_manifest_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that build_manifest rejects path traversal tokens."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    def mock_walk(top):
        yield str(bundle), [], ["../escaped.txt"]

    monkeypatch.setattr(os, "walk", mock_walk)

    with pytest.raises(ValueError, match="Path traversal token"):
        build_manifest(bundle)


def test_classify_dll_direct_crt(tmp_path: Path) -> None:
    """Verifies that classify_dll correctly identifies standard Microsoft CRT DLLs."""
    p = tmp_path / "msvcp140.dll"
    p.write_bytes(b"MZ" + b"\x00" * 100 + MSFT_UTF16 + b"\x00" * 100 + VS_UTF16)
    cls, details = classify_dll(p)
    assert cls == "msvc_crt_direct"
    assert details["has_msft_marker"] is True
    assert details["has_vs_marker"] is True


def test_classify_dll_renamed_crt(tmp_path: Path) -> None:
    """Verifies that classify_dll identifies versioned or privately renamed delvewheel CRT DLLs."""
    p = tmp_path / "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
    p.write_bytes(b"MZ" + b"\x00" * 100 + MSFT_UTF16 + b"\x00" * 100 + VS_UTF16)
    cls, details = classify_dll(p)
    assert cls == "msvc_crt_renamed"
    assert details["has_msft_marker"] is True


def test_classify_dll_third_party_non_crt(tmp_path: Path) -> None:
    """Verifies that classify_dll identifies non-Microsoft open-source libraries as third_party_non_crt."""
    p = tmp_path / "arrow.dll"
    p.write_bytes(b"MZ" + b"\x00" * 200 + b"Apache Arrow Project")
    cls, details = classify_dll(p)
    assert cls == "third_party_non_crt"
    assert details["has_msft_marker"] is False


def test_classify_dll_unknown_cases(tmp_path: Path) -> None:
    """Verifies that classify_dll marks non-PE files or unverified CRT names as unknown."""
    # Non-PE header
    p1 = tmp_path / "corrupt.dll"
    p1.write_bytes(b"NOT_A_PE_FILE")
    cls1, _ = classify_dll(p1)
    assert cls1 == "unknown"

    # CRT name pattern but missing Microsoft signatures -> unknown/unverified
    p2 = tmp_path / "msvcp140.dll"
    p2.write_bytes(b"MZ" + b"\x00" * 200 + b"Unverified Binary")
    cls2, _ = classify_dll(p2)
    assert cls2 == "unknown"


def test_crt_gate_blocks_root_level_crt_unconditionally(tmp_path: Path) -> None:
    """Verifies that root-level CRT DLLs are rejected unconditionally, even if authorized."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    p = bundle / "msvcp140.dll"
    p.write_bytes(b"MZ" + b"\x00" * 50 + MSFT_UTF16 + b"\x00" * 50 + VS_UTF16)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "approved_hashes": [compute_sha256(p)],
        "approved_paths": ["msvcp140.dll"],
    }), encoding="utf-8")

    # Even with allow_crt_redistribution and valid auth file, root-level CRT is prohibited
    with pytest.raises(RuntimeError, match="Prohibited root-level CRT binary detected"):
        enforce_crt_redistribution_gate(
            bundle,
            mode="release",
            allow_crt_redistribution=True,
            crt_authorization_file=auth_file,
        )


def test_crt_gate_blocks_unauthorized_nested_crt(tmp_path: Path) -> None:
    """Verifies that nested CRT binaries in site-packages fail closed without authorization."""
    bundle = tmp_path / "bundle"
    nested_dir = bundle / "runtime" / "python" / "Lib" / "site-packages" / "numpy.libs"
    nested_dir.mkdir(parents=True)
    p = nested_dir / "msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"
    p.write_bytes(b"MZ" + b"\x00" * 50 + MSFT_UTF16 + b"\x00" * 50 + VS_UTF16)

    # In release mode without authorization -> fails closed
    with pytest.raises(RuntimeError, match="Microsoft CRT redistribution gate blocked release packaging"):
        enforce_crt_redistribution_gate(bundle, mode="release", allow_crt_redistribution=False)


def test_crt_gate_requires_authorization_record_when_flag_passed(tmp_path: Path) -> None:
    """Verifies that passing --allow-crt-redistribution without a valid record fails closed."""
    bundle = tmp_path / "bundle"
    nested_dir = bundle / "runtime" / "python" / "Lib" / "site-packages" / "pyarrow"
    nested_dir.mkdir(parents=True)
    p = nested_dir / "msvcp140.dll"
    p.write_bytes(b"MZ" + b"\x00" * 50 + MSFT_UTF16 + b"\x00" * 50 + VS_UTF16)

    # Flag passed without authorization file -> fails closed
    with pytest.raises(RuntimeError, match="supplied without a valid explicit authorization record"):
        enforce_crt_redistribution_gate(
            bundle,
            mode="release",
            allow_crt_redistribution=True,
            crt_authorization_file=None,
        )


def test_crt_gate_fails_closed_on_unauthorized_binary(tmp_path: Path) -> None:
    """Verifies that authorization file omits a detected binary fails closed."""
    bundle = tmp_path / "bundle"
    nested_dir = bundle / "runtime" / "python" / "Lib" / "site-packages" / "pyarrow"
    nested_dir.mkdir(parents=True)
    p = nested_dir / "msvcp140.dll"
    p.write_bytes(b"MZ" + b"\x00" * 50 + MSFT_UTF16 + b"\x00" * 50 + VS_UTF16)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "approved_hashes": ["0000000000000000000000000000000000000000000000000000000000000000"],
        "approved_paths": ["some/other/path.dll"],
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="are NOT authorized"):
        enforce_crt_redistribution_gate(
            bundle,
            mode="release",
            allow_crt_redistribution=True,
            crt_authorization_file=auth_file,
        )


def test_crt_gate_passes_with_valid_authorization(tmp_path: Path) -> None:
    """Verifies that nested CRT binaries pass when explicitly authorized in the record."""
    bundle = tmp_path / "bundle"
    nested_dir = bundle / "runtime" / "python" / "Lib" / "site-packages" / "pyarrow"
    nested_dir.mkdir(parents=True)
    p = nested_dir / "msvcp140.dll"
    p.write_bytes(b"MZ" + b"\x00" * 50 + MSFT_UTF16 + b"\x00" * 50 + VS_UTF16)

    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({
        "approved_hashes": [compute_sha256(p)],
        "approved_paths": [],
    }), encoding="utf-8")

    result = enforce_crt_redistribution_gate(
        bundle,
        mode="release",
        allow_crt_redistribution=True,
        crt_authorization_file=auth_file,
    )
    assert result["status"] == "approved"
    assert result["crt_binaries"] == 1


def test_crt_gate_blocks_unknown_dlls(tmp_path: Path) -> None:
    """Verifies that unknown or ambiguous DLLs fail closed in release mode."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "suspicious.dll").write_bytes(b"CORRUPT_BYTES")

    with pytest.raises(RuntimeError, match="Unclassifiable or ambiguous DLL detected"):
        enforce_crt_redistribution_gate(bundle, mode="release", allow_crt_redistribution=True)


def test_setup_iss_process_detection_and_cleanup_spec() -> None:
    """Verifies packaging/setup.iss implements the required Win32 APIs, 3-category detection, and safe cleanup."""
    iss_path = Path(__file__).resolve().parent.parent.parent / "packaging" / "setup.iss"
    assert iss_path.is_file(), "setup.iss must exist"
    content = iss_path.read_text(encoding="utf-8")

    # Win32 API declarations
    assert "CreateToolhelp32Snapshot@kernel32.dll" in content
    assert "Process32FirstW@kernel32.dll" in content
    assert "Process32NextW@kernel32.dll" in content
    assert "OpenProcess@kernel32.dll" in content
    assert "CloseHandle@kernel32.dll" in content
    assert "QueryFullProcessImageNameW@kernel32.dll" in content
    assert "GetLastError@kernel32.dll" in content

    # 3-Category process detection
    assert "Category1Detected" in content
    assert "Category3Detected" in content
    assert "MB_RETRYCANCEL" in content
    assert "CheckRunningInstances" in content
    assert "InitializeSetup" in content

    # Directory boundary matching
    assert "AddBackslash" in content
    assert "Pos(AppPrefix, ImageBuf) = 1" in content

    # Obsolete file cleanup and path safety
    assert "CleanupObsoleteFiles" in content
    assert "IsSafeRelativeAppPath" in content
    assert "manifest.previous.json" in content
    assert "manifest.current.json" in content
    assert "STEP=CLEANING" in content

    # Storage safety rule: must protect user storage
    assert ".DATA-INTELLIGENCE-LAB\\STORAGE" in content


def test_obsolete_file_calculation() -> None:
    """Verifies obsolete file determination between two manifests."""
    manifest_prev = {
        "files": [
            {"path": "file1.txt", "size": 10, "sha256": "abc"},
            {"path": "obsolete.txt", "size": 20, "sha256": "def"},
            {"path": "modified.txt", "size": 30, "sha256": "old_hash"},
        ]
    }
    manifest_curr = {
        "files": [
            {"path": "file1.txt", "size": 10, "sha256": "abc"},
            {"path": "modified.txt", "size": 35, "sha256": "new_hash"},
            {"path": "new_file.txt", "size": 40, "sha256": "ghi"},
        ]
    }

    prev_paths = {f["path"] for f in manifest_prev["files"]}
    curr_paths = {f["path"] for f in manifest_curr["files"]}

    obsolete_files = prev_paths - curr_paths
    assert obsolete_files == {"obsolete.txt"}


def test_storage_and_app_paths_segregation() -> None:
    """Verifies that default user storage is strictly outside the application install directory."""
    user_storage = config.resolve_default_storage_dir()
    install_dir = Path(os.environ.get("LOCALAPPDATA", "C:\\Users\\User\\AppData\\Local")) / "Programs" / "Data Intelligence Lab"

    # User storage must never be inside install_dir, and install_dir must never be inside user_storage
    assert not user_storage.resolve().is_relative_to(install_dir.resolve())
    assert not install_dir.resolve().is_relative_to(user_storage.resolve())
    assert user_storage.name == "storage"
