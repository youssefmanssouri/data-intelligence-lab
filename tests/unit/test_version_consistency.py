"""
One release version everywhere a user or a tool can see it: the API (backend/version.py),
the installer (packaging/setup.iss default AppVersion), the bundle manifest
(packaging/build_runtime_bundle.py) and the frontend package (package.json and its lock).
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from backend.version import APP_VERSION

ROOT = Path(__file__).resolve().parents[2]


def test_app_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION), APP_VERSION


def test_installer_default_version_matches() -> None:
    iss = (ROOT / "packaging" / "setup.iss").read_text(encoding="utf-8")
    m = re.search(r'#define AppVersion "([^"]+)"', iss)
    assert m and m.group(1) == APP_VERSION


def test_bundle_manifest_version_matches() -> None:
    bundler = (ROOT / "packaging" / "build_runtime_bundle.py").read_text(encoding="utf-8")
    assert f'"version": "{APP_VERSION}"' in bundler


def test_frontend_package_version_matches() -> None:
    pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert pkg["version"] == APP_VERSION
    assert lock["version"] == APP_VERSION and lock["packages"][""]["version"] == APP_VERSION


def test_api_reports_the_app_version(tmp_path: Path) -> None:
    from backend.app import create_app
    assert create_app(storage_dir=tmp_path).version == APP_VERSION
