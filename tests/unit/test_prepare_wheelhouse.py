"""
Data Intelligence Lab - Verified wheelhouse preparation unit tests.

Offline: pip is replaced by a fake, so no network is used. The real end-to-end
run (PyPI downloads plus the published CRT-free pyarrow wheel) is documented
in packaging/README.md.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

_PKG = Path(__file__).resolve().parent.parent.parent / "packaging"
_spec = importlib.util.spec_from_file_location("prepare_wheelhouse", _PKG / "prepare_wheelhouse.py")
assert _spec is not None and _spec.loader is not None
pw = importlib.util.module_from_spec(_spec)
sys.modules["prepare_wheelhouse"] = pw
_spec.loader.exec_module(pw)

REAL_LOCK = _PKG / "requirements-vendor.txt"
ACCEPTED_PYARROW_SHA256 = "00d2f53ae4e4e0edae7b73776378ae33d21704fcfbc22fe245538be0202a7840"
PYPI_PYARROW_SHA256 = "0ad4892617e1a6c7a551cfc827e072a633eaff758fa09f21c4ee548c30bcaf99"


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock(path: Path, entries: dict[str, tuple[str, str]]) -> Path:
    path.write_text(
        "".join(f"{n}=={v} \\\n    --hash=sha256:{h}\n\n" for n, (v, h) in entries.items()),
        encoding="utf-8",
    )
    return path


def test_real_lock_pins_the_accepted_crt_free_pyarrow() -> None:
    entries = pw.parse_lock(REAL_LOCK.read_text(encoding="utf-8"))
    assert len(entries) == 18
    assert "tzdata" in entries
    assert entries["pyarrow"] == ("18.1.0", ACCEPTED_PYARROW_SHA256)
    assert entries["pyarrow"][1] != PYPI_PYARROW_SHA256


def test_parse_lock_rejects_duplicates_and_malformed_entries() -> None:
    good = "pyarrow==18.1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    with pytest.raises(pw.WheelhouseError, match="Duplicate"):
        pw.parse_lock(good + "\n" + good)
    with pytest.raises(pw.WheelhouseError, match="not 'name==version"):
        pw.parse_lock(good + "\nfastapi==0.162.0\n")
    with pytest.raises(pw.WheelhouseError, match="no pyarrow"):
        pw.parse_lock("fastapi==0.162.0 \\\n    --hash=sha256:" + "b" * 64 + "\n")


def test_local_wheel_must_have_exact_name_and_pinned_hash(tmp_path: Path) -> None:
    whl = _wheel(tmp_path / "pyarrow-18.1.0-cp312-cp312-win_amd64.whl", {"pyarrow/arrow.dll": b"x"})
    pw.verify_local_wheel(whl, "18.1.0", _sha(whl))
    with pytest.raises(pw.WheelhouseError, match="does not match the lock pin"):
        pw.verify_local_wheel(whl, "18.1.0", PYPI_PYARROW_SHA256)
    renamed = tmp_path / "pyarrow-18.1.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    renamed.write_bytes(whl.read_bytes())
    with pytest.raises(pw.WheelhouseError, match="Expected pyarrow-18.1.0-cp312-cp312-win_amd64.whl"):
        pw.verify_local_wheel(renamed, "18.1.0", _sha(renamed))
    with pytest.raises(pw.WheelhouseError, match="not found"):
        pw.verify_local_wheel(tmp_path / "missing.whl", "18.1.0", "0" * 64)


@pytest.mark.parametrize("member", [
    "pyarrow/msvcp140.dll",
    "numpy.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll",
    "pkg/vcruntime140_1.dll",
    "pkg/api-ms-win-crt-runtime-l1-1-0.dll",
    "pkg/ucrtbase.dll",
    "pkg/concrt140.dll",
])
def test_crt_members_detects_bundled_microsoft_runtime(tmp_path: Path, member: str) -> None:
    whl = _wheel(tmp_path / "x-1-cp312-cp312-win_amd64.whl", {member: b"MZ", "pkg/arrow.dll": b"MZ"})
    assert pw.crt_members(whl) == [member]


def test_crt_members_ignores_non_crt_libraries(tmp_path: Path) -> None:
    whl = _wheel(tmp_path / "x-1-cp312-cp312-win_amd64.whl", {
        "pyarrow/arrow.dll": b"MZ", "pyarrow/arrow_python.dll": b"MZ",
        "pydantic_core/_pydantic_core.cp312-win_amd64.pyd": b"MZ",
    })
    assert pw.crt_members(whl) == []


def _setup(tmp_path: Path, remote_members: dict[str, bytes] | None = None):
    """A lock with one remote package and pyarrow, plus a fake pip that 'downloads' it."""
    src = tmp_path / "src"; src.mkdir()
    remote = _wheel(src / "fastapi-0.162.0-py3-none-any.whl", remote_members or {"fastapi/__init__.py": b""})
    local = _wheel(src / "pyarrow-18.1.0-cp312-cp312-win_amd64.whl", {"pyarrow/arrow.dll": b"MZ"})
    lock = _lock(tmp_path / "lock.txt", {"fastapi": ("0.162.0", _sha(remote)), "pyarrow": ("18.1.0", _sha(local))})
    calls: list[list[str]] = []

    def fake_pip(args: list[str]) -> None:
        calls.append(args)
        if "--no-index" not in args:  # the PyPI download step
            dest = Path(args[args.index("-d") + 1])
            (dest / remote.name).write_bytes(remote.read_bytes())

    return lock, local, calls, fake_pip


def test_prepare_wheelhouse_builds_verified_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock, local, calls, fake_pip = _setup(tmp_path)
    monkeypatch.setattr(pw, "_pip", fake_pip)
    out = tmp_path / "wheelhouse"
    manifest = pw.prepare_wheelhouse(local, out, lock_path=lock)
    assert manifest["wheel_count"] == 2
    assert {w["file"] for w in manifest["wheels"]} == {local.name, "fastapi-0.162.0-py3-none-any.whl"}
    assert json.loads((out / "wheelhouse-manifest.json").read_text())["wheel_count"] == 2
    remote_req = [a for a in calls[0]]
    assert "--require-hashes" in remote_req and "--no-index" not in remote_req
    offline = calls[1]
    assert "--no-index" in offline and "--require-hashes" in offline and str(lock) in offline


def test_prepare_wheelhouse_never_fetches_pyarrow_from_the_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock, local, calls, fake_pip = _setup(tmp_path)
    seen: list[str] = []

    def capture(args: list[str]) -> None:
        if "--no-index" not in args:
            req = Path(args[args.index("-r") + 1]).read_text()
            seen.append(req)
        fake_pip(args)

    monkeypatch.setattr(pw, "_pip", capture)
    pw.prepare_wheelhouse(local, tmp_path / "wh", lock_path=lock)
    assert seen and "pyarrow" not in seen[0] and "fastapi==0.162.0" in seen[0]


def test_prepare_wheelhouse_rejects_wrong_pyarrow_before_any_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock, local, calls, fake_pip = _setup(tmp_path)
    monkeypatch.setattr(pw, "_pip", fake_pip)
    local.write_bytes(local.read_bytes() + b"tampered")
    out = tmp_path / "wh"
    with pytest.raises(pw.WheelhouseError, match="does not match the lock pin"):
        pw.prepare_wheelhouse(local, out, lock_path=lock)
    assert calls == [] and not out.exists()


def test_prepare_wheelhouse_fails_on_bundled_crt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock, local, calls, fake_pip = _setup(tmp_path, {"fastapi/msvcp140.dll": b"MZ"})
    monkeypatch.setattr(pw, "_pip", fake_pip)
    out = tmp_path / "wh"
    with pytest.raises(pw.WheelhouseError, match="Microsoft CRT files bundled"):
        pw.prepare_wheelhouse(local, out, lock_path=lock)
    assert not (out / "wheelhouse-manifest.json").exists()


def test_prepare_wheelhouse_requires_empty_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock, local, calls, fake_pip = _setup(tmp_path)
    monkeypatch.setattr(pw, "_pip", fake_pip)
    out = tmp_path / "wh"; out.mkdir(); (out / "stale.whl").write_bytes(b"x")
    with pytest.raises(pw.WheelhouseError, match="must be empty"):
        pw.prepare_wheelhouse(local, out, lock_path=lock)
