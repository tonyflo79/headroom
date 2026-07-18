from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest

from scripts.updater_release import check_configs, create_manifest, release_url


class UpdaterReleaseTests(unittest.TestCase):
    def test_release_channels_are_fixed_https_and_share_one_public_key(self) -> None:
        check_configs()

    def test_manifest_uses_versioned_github_artifact_and_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = Path("VERSION").read_text().strip()
            artifact = root / f"Headroom-{version}-aarch64.app.tar.gz"
            artifact.write_bytes(b"signed candidate")
            signature = root / f"{artifact.name}.sig"
            signature.write_text(base64.b64encode(b"s" * 96).decode(), encoding="utf-8")
            manifest = create_manifest(
                version=version, target="darwin-aarch64", artifact=artifact,
                signature=signature, notes="Credential renewal reliability.",
                published_at="2026-07-17T12:00:00Z",
            )
            platform = manifest["platforms"]["darwin-aarch64"]
            self.assertEqual(platform["url"], release_url(version, artifact.name))
            self.assertEqual(platform["signature"], signature.read_text())
            self.assertNotIn("path", platform)

    def test_manifest_refuses_wrong_version_and_unbounded_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "candidate.tar.gz"
            artifact.write_bytes(b"candidate")
            signature = root / "candidate.tar.gz.sig"
            signature.write_text(base64.b64encode(b"s" * 96).decode(), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "VERSION"):
                create_manifest(
                    version="99.0.0", target="darwin-aarch64", artifact=artifact,
                    signature=signature, notes="notes",
                    published_at="2026-07-17T12:00:00Z",
                )
            with self.assertRaisesRegex(ValueError, "release notes"):
                create_manifest(
                    version=Path("VERSION").read_text().strip(),
                    target="darwin-aarch64", artifact=artifact, signature=signature,
                    notes="x" * 2_001, published_at="2026-07-17T12:00:00Z",
                )


if __name__ == "__main__":
    unittest.main()
