"""Redacted desktop health and private corrupt-config recovery."""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from headroom import desktop_bridge, diagnostics, paths


NOW = 1_800_000_000


class DesktopDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _config(self):
        return {
            "schema_version": 1,
            "accounts": [{
                "name": "private-client", "provider": "claude",
                "home": os.path.join(self.temp.name, "private-home"),
                "expected_email": "owner@example.com",
            }],
        }

    def test_healthy_report_contains_only_bounded_component_state(self):
        config = self._config()
        paths.write_json_atomic(paths.config_path(), config)
        paths.write_json_atomic(paths.public_snapshot_path(), {
            "generated": NOW,
            "accounts": [{"name": "private-client", "provider": "claude",
                          "email": "owner@example.com", "token": "secret"}],
        })
        with mock.patch.object(diagnostics.connect, "provider_binary",
                               return_value="/private/provider"), \
                mock.patch.object(diagnostics.activity, "_project",
                                  return_value={"status": "ready"}):
            value = desktop_bridge.diagnostics_desktop(now=NOW)

        self.assertEqual(value["schema"], diagnostics.SCHEMA)
        by_id = {row["id"]: row for row in value["components"]}
        self.assertEqual(by_id["registry"]["code"], "registry_ready")
        self.assertEqual(by_id["snapshot"]["code"], "snapshot_ready")
        self.assertEqual(by_id["activity"]["code"], "activity_ready")
        encoded = json.dumps(value)
        for private in ("private-client", "private-home", "owner@example.com",
                        "/private/provider", "secret", "token"):
            self.assertNotIn(private, encoded)

    def test_corrupt_config_is_backed_up_privately_without_repair(self):
        os.makedirs(os.path.dirname(paths.config_path()), exist_ok=True)
        original = b'{"expected_email":"owner@example.com","token":"sk-private"'
        with open(paths.config_path(), "wb") as handle:
            handle.write(original)

        value = desktop_bridge.diagnostics_desktop(now=NOW)
        backups = os.listdir(diagnostics.recovery_dir())

        self.assertEqual(len(backups), 1)
        backup = os.path.join(diagnostics.recovery_dir(), backups[0])
        with open(backup, "rb") as handle:
            self.assertEqual(handle.read(), original)
        with open(paths.config_path(), "rb") as handle:
            self.assertEqual(handle.read(), original)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        self.assertTrue(value["private_backup"])
        self.assertEqual(
            {row["id"]: row for row in value["components"]}["registry"], {
                "id": "registry", "state": "attention",
                "code": "registry_unreadable",
                "remediation": "restore_private_backup",
            })
        encoded = json.dumps(value)
        self.assertNotIn("owner@example.com", encoded)
        self.assertNotIn("sk-private", encoded)

        repeated = diagnostics.backup_corrupt_config()
        self.assertEqual(repeated, {"state": "available", "created": False})
        self.assertEqual(os.listdir(diagnostics.recovery_dir()), backups)

    def test_symlink_config_is_never_followed_or_backed_up(self):
        target = os.path.join(self.temp.name, "outside.json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(self._config(), handle)
        os.symlink(target, paths.config_path())

        state, config, code = desktop_bridge._registry_discovery()

        self.assertEqual((state, config, code),
                         ("recovery", None, "registry_unreadable"))
        self.assertFalse(os.path.exists(diagnostics.recovery_dir()))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self._config())

    def test_oversized_corrupt_config_is_not_copied(self):
        os.makedirs(os.path.dirname(paths.config_path()), exist_ok=True)
        with open(paths.config_path(), "wb") as handle:
            handle.write(b"{" + b"x" * diagnostics.BACKUP_MAX_BYTES)

        value = desktop_bridge.diagnostics_desktop(now=NOW)

        self.assertFalse(value["private_backup"])
        self.assertEqual(
            {row["id"]: row for row in value["components"]}["registry"]
            ["remediation"], "repair_config_manually")
        self.assertFalse(os.path.exists(diagnostics.recovery_dir()))

    def test_bridge_advertises_and_serves_redacted_diagnostics(self):
        handshake, _ = desktop_bridge._handle("handshake", {})
        self.assertIn("redacted_diagnostics", handshake["capabilities"])

        value, should_exit = desktop_bridge._handle("diagnostics", {"now": NOW})

        self.assertFalse(should_exit)
        self.assertEqual(value["schema"], diagnostics.SCHEMA)

    def test_diagnostic_arguments_fail_closed(self):
        with self.assertRaises(desktop_bridge.BridgeError) as raised:
            desktop_bridge._handle("diagnostics", {"path": "/tmp/private"})
        self.assertEqual(raised.exception.code, "invalid_args")


if __name__ == "__main__":
    unittest.main()
