import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

from headroom import __version__


ROOT = Path(__file__).resolve().parent.parent


class ReleaseVersionCase(unittest.TestCase):
    def test_root_version_drives_all_checked_release_surfaces(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
        self.assertEqual(__version__, version)
        cargo = (ROOT / "integrations/menubar/src-tauri/Cargo.toml").read_text(
            encoding="utf-8")
        self.assertRegex(cargo, re.escape(f'version = "{version}"'))
        cargo_lock = (
            ROOT / "integrations/menubar/src-tauri/Cargo.lock").read_text(
                encoding="utf-8")
        self.assertRegex(
            cargo_lock,
            rf'(?m)^\[\[package\]\]\nname = "headroom-menubar"\n'
            rf'version = "{re.escape(version)}"$',
        )
        tauri = json.loads((
            ROOT / "integrations/menubar/src-tauri/tauri.conf.json").read_text(
                encoding="utf-8"))
        package = json.loads((ROOT / "integrations/menubar/package.json").read_text(
            encoding="utf-8"))
        package_lock = json.loads((
            ROOT / "integrations/menubar/package-lock.json").read_text(
                encoding="utf-8"))
        self.assertEqual(tauri["version"], version)
        self.assertEqual(package["version"], version)
        self.assertEqual(package_lock["version"], version)
        self.assertEqual(package_lock["packages"][""]["version"], version)

    def test_projector_updates_cargo_and_npm_lockfile_versions(self):
        script = ROOT / "scripts/release-version.py"
        spec = importlib.util.spec_from_file_location("release_version", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        projected = module.projected_files("9.8.7")
        cargo_lock = projected[
            ROOT / "integrations/menubar/src-tauri/Cargo.lock"]
        self.assertRegex(
            cargo_lock,
            r'(?m)^\[\[package\]\]\nname = "headroom-menubar"\n'
            r'version = "9\.8\.7"$',
        )
        npm_lock = json.loads(projected[
            ROOT / "integrations/menubar/package-lock.json"])
        self.assertEqual(npm_lock["version"], "9.8.7")
        self.assertEqual(npm_lock["packages"][""]["version"], "9.8.7")

    def test_release_version_checker_accepts_the_committed_tree(self):
        result = subprocess.run(
            [sys.executable, "scripts/release-version.py", "--check"],
            cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(__version__, result.stdout)


if __name__ == "__main__":
    unittest.main()
