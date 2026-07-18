import hashlib
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from headroom import compatibility, desktop_bridge, paths, registry, route
from headroom.__main__ import main


class CompatibilityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(route.release_slot_leases)
        self.home = os.path.join(paths.homes_dir(), "one")
        os.makedirs(self.home)

    def config(self, schema=1):
        return {
            "schema_version": schema,
            "accounts": [{
                "name": "one", "provider": "claude", "home": self.home,
            }],
        }

    def write_raw(self, value):
        os.makedirs(paths.base_dir(), exist_ok=True)
        encoded = (json.dumps(value, indent=2) + "\n").encode()
        with open(paths.config_path(), "wb") as handle:
            handle.write(encoded)
        return encoded

    def test_contract_is_exact_redacted_and_reports_missing_state(self):
        value = compatibility.contract(["refresh", "refresh", "discover", 7])
        self.assertEqual(set(value), {
            "schema", "product", "engine", "bridge", "state", "platform",
            "architecture", "capabilities",
        })
        self.assertEqual(value["schema"], "headroom_compatibility@1")
        self.assertEqual(value["bridge"]["compatible_schemas"], {
            "minimum": 1, "maximum": 1})
        self.assertEqual(value["state"]["status"], "missing")
        self.assertEqual(value["state"]["remediation"], "run_setup")
        self.assertEqual(value["capabilities"], ["refresh", "discover"])
        encoded = json.dumps(value)
        self.assertNotIn(self.temp.name, encoded)
        self.assertNotIn(self.home, encoded)

    def test_current_state_is_validated_and_migration_is_idempotent(self):
        registry.save(self.config())
        before = Path(paths.config_path()).read_bytes()
        state = compatibility.inspect_state()
        self.assertEqual(state["status"], "compatible")
        self.assertFalse(state["migration"]["required"])
        first = compatibility.migrate_registry()
        second = compatibility.migrate_registry()
        self.assertFalse(first["changed"])
        self.assertFalse(second["changed"])
        self.assertFalse(first["backup_created"])
        self.assertEqual(Path(paths.config_path()).read_bytes(), before)
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "migrations")))

    def test_newer_state_fails_closed_without_downgrade_or_home_changes(self):
        credential = os.path.join(self.home, ".credentials.json")
        with open(credential, "w", encoding="utf-8") as handle:
            handle.write("secret-sentinel")
        before = self.write_raw(self.config(schema=2))
        state = compatibility.inspect_state()
        self.assertEqual(state["status"], "incompatible_newer")
        self.assertEqual(state["code"], "state_schema_too_new")
        with self.assertRaises(compatibility.CompatibilityError) as raised:
            compatibility.migrate_registry()
        self.assertEqual(raised.exception.code, "downgrade_refused")
        self.assertEqual(Path(paths.config_path()).read_bytes(), before)
        self.assertEqual(Path(credential).read_text(encoding="utf-8"),
                         "secret-sentinel")
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "migrations")))

    def test_unreleased_older_and_invalid_state_have_stable_read_only_guidance(self):
        older = self.write_raw(self.config(schema=0))
        state = compatibility.inspect_state()
        self.assertEqual(state["status"], "incompatible_older")
        self.assertEqual(state["code"], "state_schema_too_old")
        self.assertFalse(state["migration"]["supported"])
        self.assertEqual(Path(paths.config_path()).read_bytes(), older)
        with open(paths.config_path(), "w", encoding="utf-8") as handle:
            handle.write("not json")
        invalid = compatibility.inspect_state()
        self.assertEqual(invalid["status"], "unreadable")
        self.assertEqual(invalid["code"], "state_invalid")

    def test_synthetic_future_migration_is_private_atomic_and_idempotent(self):
        credential = os.path.join(self.home, ".credentials.json")
        with open(credential, "w", encoding="utf-8") as handle:
            handle.write("secret-sentinel")
        original = self.write_raw(self.config(schema=0))

        def zero_to_one(value):
            value["schema_version"] = 1
            return value

        result = compatibility.migrate_registry(migrations={0: zero_to_one})
        self.assertTrue(result["changed"])
        self.assertTrue(result["backup_created"])
        self.assertEqual(registry.load()["schema_version"], 1)
        backup_dir = os.path.join(paths.state_dir(), "migrations")
        backups = os.listdir(backup_dir)
        self.assertEqual(len(backups), 1)
        backup = os.path.join(backup_dir, backups[0])
        self.assertEqual(Path(backup).read_bytes(), original)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(backup_dir).st_mode), 0o700)
        self.assertEqual(Path(credential).read_text(encoding="utf-8"),
                         "secret-sentinel")
        again = compatibility.migrate_registry(migrations={0: zero_to_one})
        self.assertFalse(again["changed"])
        self.assertEqual(os.listdir(backup_dir), backups)

    def test_invalid_transform_and_write_failure_leave_original_readable(self):
        original = self.write_raw(self.config(schema=0))

        def invalid(value):
            value["schema_version"] = 99
            return value

        with self.assertRaises(compatibility.CompatibilityError) as raised:
            compatibility.migrate_registry(migrations={0: invalid})
        self.assertEqual(raised.exception.code, "migration_invalid")
        self.assertEqual(Path(paths.config_path()).read_bytes(), original)

        def valid(value):
            value["schema_version"] = 1
            return value

        with mock.patch.object(paths, "write_json_atomic",
                               side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                compatibility.migrate_registry(migrations={0: valid})
        self.assertEqual(Path(paths.config_path()).read_bytes(), original)

    def test_symlink_and_oversized_registry_are_never_followed_or_backed_up(self):
        outside = os.path.join(self.temp.name, "outside.json")
        with open(outside, "wb") as handle:
            handle.write(b'{"schema_version": 2}')
        os.symlink(outside, paths.config_path())
        self.assertEqual(compatibility.inspect_state()["code"], "state_unreadable")
        with self.assertRaises(compatibility.CompatibilityError) as raised:
            compatibility.migrate_registry()
        self.assertEqual(raised.exception.code, "state_unreadable")
        self.assertEqual(Path(outside).read_bytes(), b'{"schema_version": 2}')
        os.unlink(paths.config_path())
        with open(paths.config_path(), "wb") as handle:
            handle.truncate(compatibility.MAX_CONFIG_BYTES + 1)
        self.assertEqual(compatibility.inspect_state()["code"], "state_oversized")
        self.assertFalse(os.path.exists(os.path.join(paths.state_dir(), "migrations")))

    def test_migration_backup_symlink_is_refused_without_touching_target(self):
        original = self.write_raw(self.config(schema=0))
        backup_dir = os.path.join(paths.state_dir(), "migrations")
        os.makedirs(backup_dir)
        outside = os.path.join(self.temp.name, "outside-backup")
        Path(outside).write_bytes(b"outside-sentinel")
        digest = hashlib.sha256(original).hexdigest()[:16]
        backup = os.path.join(backup_dir, f"config-v0-to-v1-{digest}.json")
        os.symlink(outside, backup)

        def zero_to_one(value):
            value["schema_version"] = 1
            return value

        with self.assertRaises(compatibility.CompatibilityError) as raised:
            compatibility.migrate_registry(migrations={0: zero_to_one})
        self.assertEqual(raised.exception.code, "migration_backup_unsafe")
        self.assertEqual(Path(paths.config_path()).read_bytes(), original)
        self.assertEqual(Path(outside).read_bytes(), b"outside-sentinel")

    def test_migration_and_cli_mutation_share_the_registry_lock(self):
        registry.save(self.config())
        entered = threading.Event()
        release = threading.Event()
        original_read = compatibility._read_config_bytes

        def blocked_read():
            entered.set()
            self.assertTrue(release.wait(2))
            return original_read()

        migration = threading.Thread(target=compatibility.migrate_registry)
        with mock.patch.object(compatibility, "_read_config_bytes",
                               side_effect=blocked_read):
            migration.start()
            self.assertTrue(entered.wait(2))
            mutation = threading.Thread(target=lambda: registry.mutate(
                lambda value: value.setdefault("dashboard", {}).update(
                    {"title": "concurrent"})))
            mutation.start()
            self.assertTrue(mutation.is_alive())
            release.set()
            migration.join(2)
            mutation.join(2)
        self.assertFalse(migration.is_alive())
        self.assertFalse(mutation.is_alive())
        self.assertEqual(registry.load()["dashboard"]["title"], "concurrent")

    def test_concurrent_cooldown_and_quarantine_writers_preserve_both_updates(self):
        first_read = threading.Event()
        release = threading.Event()
        original_read = route._read_cooldowns
        reads = 0

        def blocked_cooldown_read():
            nonlocal reads
            reads += 1
            if reads == 1:
                first_read.set()
                self.assertTrue(release.wait(2))
            return original_read()

        with mock.patch.object(route, "_read_cooldowns",
                               side_effect=blocked_cooldown_read):
            one = threading.Thread(target=lambda: route.mark("one", "sonnet"))
            two = threading.Thread(target=lambda: route.mark("two", "codex"))
            one.start()
            self.assertTrue(first_read.wait(2))
            two.start()
            self.assertTrue(two.is_alive())
            release.set()
            one.join(2)
            two.join(2)
        self.assertEqual(set(route.cooldowns()), {"one:sonnet", "two:codex"})

        first_read.clear()
        release.clear()
        reads = 0
        original_quarantine = route._read_quarantine

        def blocked_quarantine_read():
            nonlocal reads
            reads += 1
            if reads == 1:
                first_read.set()
                self.assertTrue(release.wait(2))
            return original_quarantine()

        with mock.patch.object(route, "_read_quarantine",
                               side_effect=blocked_quarantine_read):
            one = threading.Thread(
                target=lambda: route.quarantine_mark("one", "auth"))
            two = threading.Thread(
                target=lambda: route.quarantine_mark("two", "auth"))
            one.start()
            self.assertTrue(first_read.wait(2))
            two.start()
            self.assertTrue(two.is_alive())
            release.set()
            one.join(2)
            two.join(2)
        self.assertEqual(set(route.quarantines()), {"one", "two"})

    def test_bridge_observes_external_compatible_changes_without_restart(self):
        registry.save(self.config())
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=[]):
            first_view = desktop_bridge.discover_desktop(now=1_800_000_000)
        self.assertFalse(first_view["accounts"][0]["reserved"])
        first, _ = desktop_bridge._handle("compatibility", {})
        self.assertEqual(first["state"]["status"], "compatible")
        registry.mutate(lambda value: (
            value["accounts"][0].update({"reserved": True}),
            value.setdefault("dashboard", {}).update({"title": "CLI change"})))
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=[]):
            changed_view = desktop_bridge.discover_desktop(now=1_800_000_001)
        self.assertTrue(changed_view["accounts"][0]["reserved"])
        self.assertEqual(changed_view["settings"]["title"], "CLI change")
        with open(paths.config_path(), "w", encoding="utf-8") as handle:
            json.dump(self.config(schema=2), handle)
        second, _ = desktop_bridge._handle("compatibility", {})
        self.assertEqual(second["state"]["status"], "incompatible_newer")
        handshake, _ = desktop_bridge._handle("handshake", {
            "accepted_schemas": [desktop_bridge.SCHEMA]})
        self.assertIn("schema_compatibility", handshake["capabilities"])
        self.assertEqual(handshake["compatibility"]["state"]["status"],
                         "incompatible_newer")
        with self.assertRaises(desktop_bridge.BridgeError) as raised:
            desktop_bridge._handle("compatibility", {"path": "/private"})
        self.assertEqual(raised.exception.code, "invalid_args")

    def test_cli_contract_remains_available_for_incompatible_state(self):
        self.write_raw(self.config(schema=2))
        with mock.patch("builtins.print") as output:
            self.assertEqual(main(["compatibility"]), 0)
        value = json.loads(output.call_args.args[0])
        self.assertEqual(value["state"]["status"], "incompatible_newer")
        self.assertIn("schema_compatibility", value["capabilities"])
        self.assertNotIn(self.temp.name, json.dumps(value))


if __name__ == "__main__":
    unittest.main()
