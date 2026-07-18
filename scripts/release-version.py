#!/usr/bin/env python3
"""Synchronize and verify generated release-version mirrors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"
CARGO_FILE = ROOT / "integrations/menubar/src-tauri/Cargo.toml"
CARGO_LOCK_FILE = ROOT / "integrations/menubar/src-tauri/Cargo.lock"
TAURI_FILE = ROOT / "integrations/menubar/src-tauri/tauri.conf.json"
PACKAGE_FILE = ROOT / "integrations/menubar/package.json"
PACKAGE_LOCK_FILE = ROOT / "integrations/menubar/package-lock.json"
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def source_version():
    value = VERSION_FILE.read_text(encoding="utf-8").strip()
    if VERSION_RE.fullmatch(value) is None:
        raise ValueError("VERSION must be a SemVer release without a leading v")
    return value


def projected_files(version):
    cargo = CARGO_FILE.read_text(encoding="utf-8")
    cargo, count = re.subn(
        r"(?m)^(\[package\]\nname = \"headroom-menubar\"\nversion = )\"[^\"]+\"",
        rf'\g<1>"{version}"', cargo, count=1)
    if count != 1:
        raise ValueError("desktop Cargo package version is not recognizable")
    cargo_lock = CARGO_LOCK_FILE.read_text(encoding="utf-8")
    cargo_lock, lock_count = re.subn(
        r'(?m)^(\[\[package\]\]\nname = "headroom-menubar"\nversion = )"[^"]+"',
        rf'\g<1>"{version}"', cargo_lock, count=1)
    if lock_count != 1:
        raise ValueError("desktop Cargo lock package version is not recognizable")
    tauri = json.loads(TAURI_FILE.read_text(encoding="utf-8"))
    tauri["version"] = version
    package = json.loads(PACKAGE_FILE.read_text(encoding="utf-8"))
    package["version"] = version
    package_lock = json.loads(PACKAGE_LOCK_FILE.read_text(encoding="utf-8"))
    package_lock["version"] = version
    package_lock["packages"][""]["version"] = version
    return {
        CARGO_FILE: cargo,
        CARGO_LOCK_FILE: cargo_lock,
        TAURI_FILE: json.dumps(tauri, indent=2) + "\n",
        PACKAGE_FILE: json.dumps(package, indent=2) + "\n",
        PACKAGE_LOCK_FILE: json.dumps(package_lock, indent=2) + "\n",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    version = source_version()
    mismatches = []
    for path, expected in projected_files(version).items():
        current = path.read_text(encoding="utf-8")
        if current == expected:
            continue
        mismatches.append(path)
        if args.write:
            path.write_text(expected, encoding="utf-8")
    if mismatches and args.check:
        for path in mismatches:
            print(f"release version mismatch: {path.relative_to(ROOT)}",
                  file=sys.stderr)
        return 1
    if args.write:
        print(f"synchronized Headroom {version}")
    else:
        print(f"Headroom {version} release mirrors verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
