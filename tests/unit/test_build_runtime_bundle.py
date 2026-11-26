"""
Offline tests for the release staging path of packaging/build_runtime_bundle.py:
fail-closed release rules, CPython embed staging without Microsoft CRT DLLs,
wheelhouse verification, frontend staging, the extended CRT scan, and
independent manifest re-verification.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

_BUILDER = Path(__file__).resolve().parents[2] / "packaging" / "build_runtime_bundle.py"
if "build_runtime_bundle" in sys.modules:
    b = sys.modules["build_runtime_bundle"]
else:
    _spec = importlib.util.spec_from_file_location("build_runtime_bundle", _BUILDER)
    assert _spec and _spec.loader
    b = importlib.util.module_from_spec(_spec)
    sys.modules["build_runtime_bundle"] = b
    _spec.loader.exec_module(b)

MSFT = "Microsoft Corporation".encode("utf-16-le")
VS = "Visual Studio".encode("utf-16-le")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _embed_zip(path: Path, extra: dict[str, bytes] | None = None) -> Path:
    members = {
        "python.exe": b"MZ python",
        "python312.dll": b"MZ dll",
        "python312.zip": b"PK stdlib",
        "python312._pth": b"python312.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n",
        "_socket.pyd": b"MZ pyd",
        "vcruntime140.dll": b"MZ" + MSFT + VS,
        "vcruntime140_1.dll": b"MZ" + MSFT + VS,
    }
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _wheelhouse(tmp_path: Path, lock: Path) -> Path:
    wh = tmp_path / "wheelhouse"
    wh.mkdir()
    wheels = []
    for name in ("a-1.0-py3-none-any.whl", "b-2.0-cp312-cp312-win_amd64.whl"):
        (wh / name).write_bytes(name.encode())
        wheels.append({"file": name, "sha256": _sha(wh / name), "size": len(name)})
    manifest = {
        "target": "cp312-win_amd64",
        "lock": lock.name,
        "lock_sha256": _sha(lock),
        "wheel_count": len(wheels),
        "wheels": wheels,
    }
    (wh / "wheelhouse-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return wh


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for pkg in ("backend", "core"):
        (repo / pkg).mkdir(parents=True)
        (repo / pkg / "__init__.py").write_text("", encoding="utf-8")
        (repo / pkg / "__pycache__").mkdir()
        (repo / pkg / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"junk")
    (repo / "launcher.py").write_text("# launcher", encoding="utf-8")
    (repo / "packaging").mkdir()
    (repo / "packaging" / "requirements-vendor.txt").write_text("a==1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    fe = repo / "frontend"
    for d in (".next/static", ".next/server", ".next/cache/webpack", "config", "scripts"):
        (fe / d).mkdir(parents=True, exist_ok=True)
    (fe / ".next" / "BUILD_ID").write_text("build-123", encoding="utf-8")
    (fe / ".next" / "server" / "page.js").write_text("//", encoding="utf-8")
    (fe / ".next" / "cache" / "webpack" / "big.pack").write_bytes(b"cache")
    (fe / ".next" / "trace").write_text("trace", encoding="utf-8")
    (fe / "config" / "proxy.mjs").write_text("//", encoding="utf-8")
    (fe / "scripts" / "server.mjs").write_text("//", encoding="utf-8")
    for f in ("next.config.mjs", "package.json", "package-lock.json"):
        (fe / f).write_text("{}", encoding="utf-8")
    (fe / "src").mkdir()
    (fe / "src" / "page.tsx").write_text("source", encoding="utf-8")
    return repo


def _fake_npm(tmp_path: Path, fail: bool = False) -> list[str]:
    """A stand-in for `npm` that records its arguments and creates production packages."""
    script = tmp_path / "fake_npm.py"
    script.write_text(
        "import json, sys, pathlib\n"
        f"if {fail!r}:\n"
        "    print('npm ERR! simulated', file=sys.stderr); sys.exit(1)\n"
        "root = pathlib.Path.cwd()\n"
        "(root / 'npm-args.json').write_text(json.dumps(sys.argv[1:]))\n"
        "for pkg in ('next', 'react', 'react-dom', 'lossless-json'):\n"
        "    d = root / 'node_modules' / pkg; d.mkdir(parents=True, exist_ok=True)\n"
        "    (d / 'package.json').write_text('{}')\n"
        "(root / 'node_modules' / '.bin').mkdir(exist_ok=True)\n"
        "(root / 'node_modules' / '.bin' / 'next').write_text('shim')\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


# --- Release-mode preconditions -------------------------------------------------

def test_release_mode_rejects_skip_download(tmp_path: Path) -> None:
    with pytest.raises(b.BundleError, match="skip-download"):
        b.stage_bundle(_fake_repo(tmp_path), tmp_path / "out", mode="release", skip_download=True, wheelhouse=tmp_path)


def test_release_mode_requires_wheelhouse(tmp_path: Path) -> None:
    with pytest.raises(b.BundleError, match="wheelhouse"):
        b.stage_bundle(_fake_repo(tmp_path), tmp_path / "out", mode="release")


def test_release_mode_rejects_unpinned_local_node(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "node").mkdir()
    (repo / "node" / "node.exe").write_bytes(b"MZ not the pinned node")
    lock = repo / "packaging" / "requirements-vendor.txt"
    with pytest.raises(b.BundleError, match="pinned Node.js"):
        b.stage_bundle(repo, tmp_path / "out", mode="release", wheelhouse=_wheelhouse(tmp_path, lock),
                       npm_command=_fake_npm(tmp_path), cache_dir=tmp_path / "cache")


def test_release_mode_requires_built_frontend(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "frontend" / ".next" / "BUILD_ID").unlink()
    lock = repo / "packaging" / "requirements-vendor.txt"
    with pytest.raises(b.BundleError, match="not built"):
        b.stage_bundle(repo, tmp_path / "out", mode="release", wheelhouse=_wheelhouse(tmp_path, lock),
                       npm_command=_fake_npm(tmp_path))


def test_node_pin_is_node_22_lts() -> None:
    assert b.NODE_VERSION.startswith("22.")
    assert b.NODE_EXE_URL == f"https://nodejs.org/dist/v{b.NODE_VERSION}/win-x64/node.exe"
    assert len(b.NODE_EXE_SHA256) == 64


# --- CPython embed ----------------------------------------------------------------

def test_python_runtime_excludes_microsoft_crt_and_writes_pth(tmp_path: Path) -> None:
    dest = tmp_path / "py"
    dest.mkdir()
    excluded = b.stage_python_runtime(dest, _embed_zip(tmp_path / "embed.zip"))
    assert sorted(excluded) == ["vcruntime140.dll", "vcruntime140_1.dll"]
    assert not (dest / "vcruntime140.dll").exists()
    assert not (dest / "vcruntime140_1.dll").exists()
    assert (dest / "python.exe").is_file() and (dest / "_socket.pyd").is_file()
    pth = (dest / "python312._pth").read_text(encoding="utf-8").splitlines()
    assert pth == ["python312.zip", ".", "Lib\\site-packages", "..\\..", "import site"]


def test_python_runtime_rejects_nested_members(tmp_path: Path) -> None:
    dest = tmp_path / "py"
    dest.mkdir()
    with pytest.raises(b.BundleError, match="nested path"):
        b.stage_python_runtime(dest, _embed_zip(tmp_path / "e.zip", {"../evil.dll": b"MZ"}))


def test_python_runtime_detects_layout_change(tmp_path: Path) -> None:
    path = tmp_path / "e.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("python.exe", b"MZ")
        z.writestr("python312._pth", b"python312.zip\n")
    dest = tmp_path / "py"
    dest.mkdir()
    with pytest.raises(b.BundleError, match="layout changed"):
        b.stage_python_runtime(dest, path)


# --- Wheelhouse --------------------------------------------------------------------

def test_verify_wheelhouse_accepts_matching_manifest(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-vendor.txt"
    lock.write_text("lock", encoding="utf-8")
    manifest = b.verify_wheelhouse(_wheelhouse(tmp_path, lock), lock)
    assert manifest["wheel_count"] == 2


def test_verify_wheelhouse_rejects_other_lock(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-vendor.txt"
    lock.write_text("lock", encoding="utf-8")
    wh = _wheelhouse(tmp_path, lock)
    lock.write_text("changed", encoding="utf-8")
    with pytest.raises(b.BundleError, match="different requirements"):
        b.verify_wheelhouse(wh, lock)


def test_verify_wheelhouse_rejects_tampered_or_extra_wheels(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-vendor.txt"
    lock.write_text("lock", encoding="utf-8")
    wh = _wheelhouse(tmp_path, lock)
    (wh / "a-1.0-py3-none-any.whl").write_bytes(b"tampered")
    with pytest.raises(b.BundleError, match="hash mismatch"):
        b.verify_wheelhouse(wh, lock)
    (wh / "a-1.0-py3-none-any.whl").write_bytes(b"a-1.0-py3-none-any.whl")
    (wh / "evil-9.9-py3-none-any.whl").write_bytes(b"x")
    with pytest.raises(b.BundleError, match="differ from its manifest"):
        b.verify_wheelhouse(wh, lock)


def test_verify_wheelhouse_requires_windows_target(tmp_path: Path) -> None:
    lock = tmp_path / "requirements-vendor.txt"
    lock.write_text("lock", encoding="utf-8")
    wh = _wheelhouse(tmp_path, lock)
    m = json.loads((wh / "wheelhouse-manifest.json").read_text(encoding="utf-8"))
    m["target"] = "cp312-manylinux_x86_64"
    (wh / "wheelhouse-manifest.json").write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(b.BundleError, match="targets"):
        b.verify_wheelhouse(wh, lock)


# --- Frontend ----------------------------------------------------------------------

def test_stage_frontend_copies_runtime_files_only(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    dest = tmp_path / "fe"
    build_id = b.stage_frontend(repo / "frontend", dest, _fake_npm(tmp_path))
    assert build_id == "build-123"
    assert (dest / ".next" / "server" / "page.js").is_file()
    assert not (dest / ".next" / "cache").exists()
    assert not (dest / ".next" / "trace").exists()
    assert not (dest / "src").exists(), "TypeScript sources are not part of the runtime"
    assert (dest / "scripts" / "server.mjs").is_file() and (dest / "config" / "proxy.mjs").is_file()
    assert not (dest / "node_modules" / ".bin").exists()
    args = json.loads((dest / "npm-args.json").read_text(encoding="utf-8"))
    assert args[0] == "ci"
    for flag in ("--omit=dev", "--ignore-scripts", "--os=win32", "--cpu=x64"):
        assert flag in args


def test_stage_frontend_surfaces_npm_failure(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    with pytest.raises(b.BundleError, match="simulated"):
        b.stage_frontend(repo / "frontend", tmp_path / "fe", _fake_npm(tmp_path, fail=True))


def test_stage_frontend_requires_production_dependencies(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    noop = tmp_path / "noop.py"
    noop.write_text("", encoding="utf-8")
    with pytest.raises(b.BundleError, match="Production dependency missing"):
        b.stage_frontend(repo / "frontend", tmp_path / "fe", [sys.executable, str(noop)])


# --- CRT gate coverage -------------------------------------------------------------

@pytest.mark.parametrize("name", ["addon.node", "_ext.pyd", "tool.exe"])
def test_crt_gate_inspects_non_dll_pe_images(tmp_path: Path, name: str) -> None:
    nested = tmp_path / "frontend" / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / name).write_bytes(b"not a PE image")
    with pytest.raises(RuntimeError, match="Unclassifiable"):
        b.enforce_crt_redistribution_gate(tmp_path, mode="release")


def test_crt_gate_blocks_embed_vcruntime_if_staged(tmp_path: Path) -> None:
    py = tmp_path / "runtime" / "python"
    py.mkdir(parents=True)
    (py / "vcruntime140.dll").write_bytes(b"MZ" + MSFT + VS)
    with pytest.raises(RuntimeError, match="Microsoft CRT binary"):
        b.enforce_crt_redistribution_gate(tmp_path, mode="release")


# --- End-to-end staging and independent verification ------------------------------

@pytest.fixture
def release_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = _fake_repo(tmp_path)
    lock = repo / "packaging" / "requirements-vendor.txt"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    node = fixtures / "node.exe"
    node.write_bytes(b"MZ fake node")
    embed = _embed_zip(fixtures / "embed.zip")
    monkeypatch.setattr(b, "NODE_EXE_SHA256", _sha(node))
    monkeypatch.setattr(b, "PYTHON_EMBED_SHA256", _sha(embed))
    sources = {b.NODE_EXE_URL: node, b.PYTHON_EMBED_URL: embed}

    def fake_download(url: str, dest: Path, expected: str) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(sources[url].read_bytes())
        assert _sha(dest) == expected
        return dest

    installed = {}

    def fake_install(wheelhouse: Path, lock_path: Path, site_packages: Path) -> None:
        installed["args"] = (wheelhouse, lock_path)
        (site_packages / "fastapi").mkdir(parents=True)
        (site_packages / "fastapi" / "__init__.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(b, "download_with_hash_check", fake_download)
    monkeypatch.setattr(b, "install_vendor_packages", fake_install)
    compiled = {}

    def fake_precompile(bundle_dir: Path, roots: list[Path]) -> int:
        compiled["roots"] = [r.relative_to(bundle_dir).as_posix() for r in roots]
        return 0

    monkeypatch.setattr(b, "precompile_bytecode", fake_precompile)
    out = b.stage_bundle(repo, tmp_path / "bundle", mode="release", wheelhouse=_wheelhouse(tmp_path, lock),
                         npm_command=_fake_npm(tmp_path), cache_dir=tmp_path / "cache")
    assert installed["args"][1] == lock
    assert compiled["roots"] == ["backend", "core", "runtime/python/Lib/site-packages"]
    return out


def test_release_bundle_layout_and_manifest(release_bundle: Path) -> None:
    out = release_bundle
    for rel in ("launcher.py", "backend/__init__.py", "core/__init__.py", "node/node.exe",
                "runtime/python/python.exe", "runtime/python/python312._pth",
                "runtime/python/Lib/site-packages/fastapi/__init__.py",
                "frontend/.next/BUILD_ID", "frontend/scripts/server.mjs", "frontend/package-lock.json"):
        assert (out / rel).is_file(), rel
    assert not list(out.rglob("__pycache__"))
    assert not list(out.rglob("vcruntime140*.dll"))
    assert not (out / "package.json").exists(), "no repository-root package.json exists to stage"

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    runtime = manifest["runtime"]
    assert runtime["mode"] == "release"
    assert runtime["frontend_build_id"] == "build-123"
    assert runtime["node"]["version"] == b.NODE_VERSION
    assert runtime["python_excluded_files"] == ["vcruntime140.dll", "vcruntime140_1.dll"]
    assert runtime["crt_gate"]["status"] == "approved" and runtime["crt_gate"]["crt_binaries"] == 0
    assert len(runtime["vendor"]["wheels"]) == 2
    assert b.verify_bundle(out)["file_count"] == manifest["file_count"]


def test_verify_bundle_detects_tampering_and_extra_files(release_bundle: Path) -> None:
    out = release_bundle
    target = out / "backend" / "__init__.py"
    target.write_text("tampered", encoding="utf-8")
    with pytest.raises(b.BundleError, match="does not match manifest"):
        b.verify_bundle(out)
    target.write_text("", encoding="utf-8")
    b.verify_bundle(out)
    (out / "backend" / "injected.py").write_text("x", encoding="utf-8")
    with pytest.raises(b.BundleError, match="unlisted"):
        b.verify_bundle(out)


def test_development_mode_offline_still_stages_layout(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "frontend" / ".next" / "BUILD_ID").unlink()
    out = b.stage_bundle(repo, tmp_path / "dev", mode="development", skip_download=True)
    assert (out / "frontend" / "scripts" / "server.mjs").is_file()
    assert "placeholder" in (out / "node" / "node.exe").read_text(encoding="utf-8")


# --- Precompiled bytecode -----------------------------------------------------------

def test_precompile_requires_cpython_312(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b.sys, "version_info", (3, 13, 0, "final", 0))
    with pytest.raises(b.BundleError, match="CPython 3.12"):
        b.precompile_bytecode(tmp_path, [tmp_path])


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="bundle bytecode is compiled by CPython 3.12")
def test_precompile_writes_unchecked_hash_pyc_without_build_paths(tmp_path: Path) -> None:
    pkg = tmp_path / "backend"
    pkg.mkdir()
    (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert b.precompile_bytecode(tmp_path, [pkg]) == 1
    pyc = pkg / "__pycache__" / "mod.cpython-312.pyc"
    data = pyc.read_bytes()
    flags = int.from_bytes(data[4:8], "little")
    assert flags == 0b01, "unchecked-hash pyc (hash-based, check_source off)"
    assert str(tmp_path).encode() not in data, "build-machine path must not be recorded"


@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="bundle bytecode is compiled by CPython 3.12")
def test_precompile_fails_closed_on_syntax_error(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(b.BundleError, match="compilation failed"):
        b.precompile_bytecode(tmp_path, [tmp_path])


def test_verify_bundle_allows_only_accepted_unlisted_files(release_bundle: Path) -> None:
    out = release_bundle
    (out / "unins000.exe").write_bytes(b"MZ")
    with pytest.raises(b.BundleError, match="unlisted"):
        b.verify_bundle(out)
    assert b.verify_bundle(out, allow_unlisted=lambda rel: rel == "unins000.exe")["file_count"] > 0
    (out / "backend" / "__pycache__").mkdir()
    (out / "backend" / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"x")
    with pytest.raises(b.BundleError, match="unlisted"):
        b.verify_bundle(out, allow_unlisted=lambda rel: rel == "unins000.exe")
