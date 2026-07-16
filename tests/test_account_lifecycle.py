import copy
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from headroom import account_lifecycle, paths, registry, route


class AccountLifecycleCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(route.release_slot_leases)
        self.home_a = os.path.join(paths.homes_dir(), "a")
        self.home_b = os.path.join(paths.homes_dir(), "b")
        os.makedirs(self.home_a)
        os.makedirs(self.home_b)
        registry.save({
            "schema_version": 1,
            "dashboard": {"title": "keep"},
            "accounts": [
                {"name": "a", "provider": "claude", "home": self.home_a,
                 "expected_email": "a@example.test"},
                {"name": "b", "provider": "codex", "home": self.home_b},
            ],
        })

    def write_state(self):
        private = {
            "schema_version": 1, "run_id": "run", "generated": 100,
            "accounts": [
                {"name": "a", "provider": "claude", "ok": True},
                {"name": "b", "provider": "codex", "ok": True},
            ],
            "integrity_warnings": [
                "duplicate claude identity: a and b are the same login; routing held",
                "unrelated warning",
            ],
        }
        public = copy.deepcopy(private)
        paths.write_json_atomic(paths.private_snapshot_path(), private)
        paths.write_json_atomic(paths.public_snapshot_path(), public, mode=0o644)
        paths.write_json_atomic(paths.cooldowns_path(), {
            "a:*": 200, "a:sonnet": 300, "b:*": 400})
        paths.write_json_atomic(paths.quarantine_path(), {
            "a": {"reason": "auth"}, "b": {"reason": "other"}})

    def test_policy_distinguishes_headroom_and_adopted_homes(self):
        owned = registry.accounts()[0]
        adopted = dict(owned, home=os.path.join(self.temp.name, "external"))
        missing = dict(owned, home=os.path.join(paths.homes_dir(), "missing"))
        self.assertEqual(account_lifecycle.home_kind(owned), "headroom")
        self.assertEqual(account_lifecycle.home_kind(adopted), "adopted")
        self.assertEqual(account_lifecycle.home_kind(missing), "adopted")
        policy = account_lifecycle.account_policy(owned, 0, 2)
        self.assertTrue(policy["home_retained_on_remove"])
        self.assertTrue(policy["rename_keeps_home"])
        self.assertTrue(policy["can_move_down"])
        self.assertTrue(policy["can_remove"])

    def test_reserve_and_reorder_preserve_concurrent_compatible_mutation(self):
        locked = threading.Event()
        release = threading.Event()

        def cli_change():
            def mutate(config):
                locked.set()
                self.assertTrue(release.wait(2))
                config["dashboard"]["title"] = "changed concurrently"
            registry.mutate(mutate)

        thread = threading.Thread(target=cli_change)
        thread.start()
        self.assertTrue(locked.wait(2))
        mover = threading.Thread(
            target=lambda: account_lifecycle.move_account("b", "up"))
        mover.start()
        self.assertTrue(mover.is_alive())
        release.set()
        thread.join(2)
        mover.join(2)
        account_lifecycle.set_reserved("b", True)
        config = registry.load()
        self.assertEqual(config["dashboard"]["title"], "changed concurrently")
        self.assertEqual([row["name"] for row in config["accounts"]], ["b", "a"])
        self.assertTrue(config["accounts"][0]["reserved"])

    def test_rename_preserves_home_and_moves_every_protective_reference(self):
        self.write_state()
        account_lifecycle.rename_account("a", "renamed")
        config = registry.load()
        renamed = config["accounts"][0]
        self.assertEqual(renamed["name"], "renamed")
        self.assertEqual(renamed["home"], self.home_a)
        self.assertEqual(renamed["expected_email"], "a@example.test")
        self.assertEqual(account_lifecycle.home_kind(renamed), "headroom")
        for path in (paths.private_snapshot_path(), paths.public_snapshot_path()):
            snapshot = paths.load_json(path)
            self.assertEqual([row["name"] for row in snapshot["accounts"]],
                             ["renamed", "b"])
            self.assertIn("renamed and b", snapshot["integrity_warnings"][0])
        self.assertEqual(route.cooldowns(), {
            "renamed:*": 200, "renamed:sonnet": 300, "b:*": 400})
        self.assertEqual(route.quarantines(), {
            "renamed": {"reason": "auth"}, "b": {"reason": "other"}})
        self.assertFalse(os.path.exists(account_lifecycle._journal_path()))

    def test_remove_unregisters_only_slot_and_retains_provider_home(self):
        self.write_state()
        credential = os.path.join(self.home_a, ".credentials.json")
        with open(credential, "w", encoding="utf-8") as handle:
            json.dump({"kept": True}, handle)
        account_lifecycle.remove_account("a")
        self.assertEqual([row["name"] for row in registry.load()["accounts"]], ["b"])
        self.assertTrue(os.path.exists(credential))
        self.assertEqual(route.cooldowns(), {"b:*": 400})
        self.assertEqual(route.quarantines(), {"b": {"reason": "other"}})
        for path in (paths.private_snapshot_path(), paths.public_snapshot_path()):
            self.assertEqual([row["name"] for row in
                              paths.load_json(path)["accounts"]], ["b"])

    def test_final_account_and_active_lease_refuse_without_changes(self):
        before = registry.load()
        registry.save({"schema_version": 1, "accounts": [before["accounts"][0]]})
        with self.assertRaisesRegex(account_lifecycle.LifecycleError, "final"):
            account_lifecycle.remove_account("a")
        registry.save(before)
        with mock.patch.dict(os.environ, {"HEADROOM_SLOT_LEASE": "1"}):
            self.assertTrue(route.acquire_slot_lease(registry.accounts()[0], "sonnet"))
            with self.assertRaisesRegex(account_lifecycle.LifecycleError, "live"):
                account_lifecycle.rename_account("a", "renamed")
        self.assertEqual(registry.load(), before)

    def test_incomplete_handoff_refuses_rename(self):
        os.makedirs(paths.state_dir(), exist_ok=True)
        with open(os.path.join(paths.state_dir(), "handoffs.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({
                "schema": "headroom_handoff@1", "ts": time.time(),
                "handoff_id": str(uuid.uuid4()), "action": "cap_confirmed",
                "automatic": True, "source_slot": "a", "target_slot": "b",
            }) + "\n")
        with self.assertRaisesRegex(account_lifecycle.LifecycleError, "handoff"):
            account_lifecycle.rename_account("a", "renamed")
        self.assertEqual(registry.load()["accounts"][0]["name"], "a")

    def test_write_failure_rolls_back_every_document(self):
        self.write_state()
        before = account_lifecycle._read_documents()
        real_write = paths.write_json_atomic
        failed = False

        def fail_config_once(path, value, mode=0o600):
            nonlocal failed
            if path == paths.config_path() and not failed:
                failed = True
                raise OSError("fixture failure")
            return real_write(path, value, mode=mode)

        with mock.patch.object(paths, "write_json_atomic", side_effect=fail_config_once):
            with self.assertRaisesRegex(account_lifecycle.LifecycleError, "rolled back"):
                account_lifecycle.rename_account("a", "renamed")
        self.assertEqual(account_lifecycle._read_documents(), before)
        self.assertFalse(os.path.exists(account_lifecycle._journal_path()))

    def test_prepared_crash_journal_rolls_mixed_state_back(self):
        self.write_state()
        before = account_lifecycle._read_documents()
        after = copy.deepcopy(before)
        after["config"]["value"]["accounts"][0]["name"] = "renamed"
        after["private_snapshot"]["value"]["accounts"][0]["name"] = "renamed"
        paths.write_json_atomic(paths.private_snapshot_path(),
                                after["private_snapshot"]["value"])
        paths.write_json_atomic(account_lifecycle._journal_path(), {
            "schema": account_lifecycle.JOURNAL_SCHEMA, "phase": "prepared",
            "operation": "rename", "account": "a", "new_name": "renamed",
            "created_at": int(time.time()), "before": before, "after": after,
        })
        self.assertTrue(account_lifecycle.recover())
        self.assertEqual(account_lifecycle._read_documents(), before)
        self.assertFalse(os.path.exists(account_lifecycle._journal_path()))

    def test_broken_symlink_journal_fails_closed(self):
        os.makedirs(paths.state_dir(), exist_ok=True)
        os.symlink(os.path.join(self.temp.name, "missing-outside"),
                   account_lifecycle._journal_path())
        with self.assertRaisesRegex(account_lifecycle.LifecycleError, "unsafe"):
            account_lifecycle.recover()
        self.assertTrue(os.path.islink(account_lifecycle._journal_path()))

    def test_recovery_never_overwrites_unknown_concurrent_state(self):
        self.write_state()
        before = account_lifecycle._read_documents()
        after = copy.deepcopy(before)
        after["config"]["value"]["accounts"][0]["name"] = "renamed"
        paths.write_json_atomic(account_lifecycle._journal_path(), {
            "schema": account_lifecycle.JOURNAL_SCHEMA, "phase": "prepared",
            "operation": "rename", "account": "a", "new_name": "renamed",
            "created_at": int(time.time()), "before": before, "after": after,
        })
        registry.mutate(lambda config: config["dashboard"].update(
            {"title": "concurrent writer"}))
        with self.assertRaisesRegex(account_lifecycle.LifecycleError,
                                    "changed during"):
            account_lifecycle.recover()
        self.assertEqual(registry.load()["dashboard"]["title"],
                         "concurrent writer")
        self.assertTrue(os.path.exists(account_lifecycle._journal_path()))


if __name__ == "__main__":
    unittest.main()
