#!/usr/bin/env python3
"""Validate Headroom update channels and create bounded static manifests."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
TAURI = ROOT / "integrations/menubar/src-tauri/tauri.conf.json"
STABLE_OVERLAY = ROOT / "integrations/menubar/src-tauri/tauri.release.conf.json"
PRERELEASE_OVERLAY = ROOT / "integrations/menubar/src-tauri/tauri.prerelease.conf.json"
VERSION_FILE = ROOT / "VERSION"
REPOSITORY = "tonyflo79/headroom"
CHANNEL_ENDPOINTS = {
    "stable": f"https://github.com/{REPOSITORY}/releases/latest/download/latest.json",
    "prerelease": f"https://github.com/{REPOSITORY}/releases/download/prerelease/latest.json",
}
TARGETS = {"darwin-aarch64", "darwin-x86_64"}
SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def public_key(config: dict) -> str:
    value = config["plugins"]["updater"]["pubkey"]
    decoded = base64.b64decode(value, validate=True).decode("utf-8")
    if not decoded.startswith("untrusted comment: minisign public key:") or "\nRW" not in decoded:
        raise ValueError("updater public key is not a Minisign public key")
    return value


def check_configs() -> None:
    stable = load(TAURI)
    stable_overlay = load(STABLE_OVERLAY)
    prerelease = load(PRERELEASE_OVERLAY)
    stable_updater = stable["plugins"]["updater"]
    prerelease_updater = prerelease["plugins"]["updater"]
    if stable_updater.get("endpoints") != [CHANNEL_ENDPOINTS["stable"]]:
        raise ValueError("stable updater endpoint changed")
    if prerelease_updater.get("endpoints") != [CHANNEL_ENDPOINTS["prerelease"]]:
        raise ValueError("prerelease updater endpoint changed")
    if public_key(stable) != public_key(prerelease):
        raise ValueError("stable and prerelease public keys differ")
    if stable["bundle"].get("createUpdaterArtifacts") is not False:
        raise ValueError("ordinary unsigned builds must not create updater artifacts")
    if stable_overlay["bundle"].get("createUpdaterArtifacts") is not True:
        raise ValueError("stable release overlay must create updater artifacts")
    if prerelease["bundle"].get("createUpdaterArtifacts") is not True:
        raise ValueError("prerelease overlay must create updater artifacts")


def release_url(version: str, artifact_name: str) -> str:
    if Path(artifact_name).name != artifact_name or not artifact_name.endswith(".tar.gz"):
        raise ValueError("updater artifact name must be a plain .tar.gz filename")
    return f"https://github.com/{REPOSITORY}/releases/download/v{version}/{artifact_name}"


def validate_release_url(url: str, version: str, artifact_name: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        raise ValueError("updater artifact URL must be an exact GitHub HTTPS release URL")
    if url != release_url(version, artifact_name):
        raise ValueError("updater artifact URL does not match the versioned release")


def create_manifest(
    *, version: str, target: str, artifact: Path, signature: Path,
    notes: str, published_at: str,
) -> dict:
    if SEMVER.fullmatch(version) is None or version != VERSION_FILE.read_text().strip():
        raise ValueError("manifest version must equal VERSION")
    if target not in TARGETS:
        raise ValueError("unsupported updater target")
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise ValueError("updater artifact is missing or empty")
    signature_value = signature.read_text(encoding="utf-8").strip()
    if len(signature_value) < 64 or len(signature_value) > 4096:
        raise ValueError("updater signature is missing or unbounded")
    base64.b64decode(signature_value, validate=True)
    if len(notes) > 2_000 or any(ord(char) < 32 and char not in "\n\t" for char in notes):
        raise ValueError("release notes are unbounded or contain control characters")
    url = release_url(version, artifact.name)
    validate_release_url(url, version, artifact.name)
    datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return {
        "version": version,
        "notes": notes.strip(),
        "pub_date": published_at,
        "platforms": {
            target: {"signature": signature_value, "url": url},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--target", choices=sorted(TARGETS))
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--signature", type=Path)
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    check_configs()
    if args.check_config:
        print("Headroom stable and prerelease update channels verified")
        return 0
    required = [args.target, args.artifact, args.signature, args.notes_file, args.output]
    if any(value is None for value in required):
        parser.error("manifest generation requires target, artifact, signature, notes-file, and output")
    manifest = create_manifest(
        version=VERSION_FILE.read_text().strip(), target=args.target,
        artifact=args.artifact, signature=args.signature,
        notes=args.notes_file.read_text(encoding="utf-8"),
        published_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote signed updater manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
