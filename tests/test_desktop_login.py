"""Rollback and diagnostic contract for GUI-owned provider login."""
import json
import os
import tempfile
import unittest
from unittest import mock

from headroom import connect, paths, registry


class FinishedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.pid = 999_999

    def poll(self):
        return self.returncode


class RunningProcess(FinishedProcess):
    def __init__(self):
        super().__init__(None)


class SequenceCancel:
    def __init__(self, *values):
        self.values = iter(values)

    def is_set(self):
        return next(self.values, True)


class DesktopFreshLogin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()
        self.config = {
            "schema_version": 1,
            "dashboard": dict(registry.DEFAULT_DASHBOARD),
            "accounts": [],
        }
        self.home = os.path.join(paths.homes_dir(), "claude-1")
        self.credential = os.path.join(self.home, ".credentials.json")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_login(self, keychain_exists=None, identity=None, **overrides):
        options = {
            "prerequisite": lambda provider, binary: True,
            "popen": lambda *args, **kwargs: FinishedProcess(),
        }
        options.update(overrides)
        if identity is None:
            identity = {
                "email": "owner@example.test", "account_fingerprint": "one"}
        if keychain_exists is None:
            values = iter([False, True])
            keychain_exists = lambda _home: next(values)
        keychain_side_effect = keychain_exists if callable(keychain_exists) else None
        keychain_return = None if callable(keychain_exists) else keychain_exists
        with mock.patch.object(connect, "provider_binary", return_value="/bin/claude"), \
                mock.patch.object(connect, "darwin_keychain_guard", return_value=True), \
                mock.patch.object(connect.collector, "claude_keychain_item_exists",
                                  return_value=keychain_return,
                                  side_effect=keychain_side_effect), \
                mock.patch.object(connect, "existing_fingerprints", return_value={}), \
                mock.patch.object(connect, "slot_identity", return_value=identity):
            return connect.desktop_connect_fresh(
                self.config, "claude-1", "claude", **options)

    def test_success_creates_an_isolated_verified_slot(self):
        progress = []
        value = self.run_login(progress=progress.append)
        self.assertEqual(value["code"], "connected")
        self.assertEqual(progress, ["preflight", "browser_login",
                                    "verifying_identity"])
        saved = registry.load()
        self.assertEqual(saved["accounts"][0]["name"], "claude-1")
        self.assertEqual(saved["accounts"][0]["expected_email"],
                         "owner@example.test")

    def test_wrong_identity_restores_existing_credentials(self):
        os.makedirs(self.home)
        with open(self.credential, "w", encoding="utf-8") as handle:
            json.dump({"token": "old"}, handle)

        def login(*_args, **_kwargs):
            with open(self.credential, "w", encoding="utf-8") as handle:
                json.dump({"token": "new"}, handle)
            return FinishedProcess()

        value = self.run_login(popen=login, expected_email="other@example.test")
        self.assertEqual(value["code"], "wrong_identity")
        with open(self.credential, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"token": "old"})
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_duplicate_identity_removes_new_credentials(self):
        def login(*_args, **_kwargs):
            os.makedirs(self.home, exist_ok=True)
            with open(self.credential, "w", encoding="utf-8") as handle:
                json.dump({"token": "new"}, handle)
            return FinishedProcess()

        with mock.patch.object(connect, "provider_binary", return_value="/bin/claude"), \
                mock.patch.object(connect, "darwin_keychain_guard", return_value=True), \
                mock.patch.object(connect, "existing_fingerprints",
                                  return_value={"one": "existing"}), \
                mock.patch.object(connect, "slot_identity", return_value={
                    "email": "owner@example.test", "account_fingerprint": "one"}):
            value = connect.desktop_connect_fresh(
                self.config, "claude-1", "claude", popen=login,
                prerequisite=lambda provider, binary: True)
        self.assertEqual(value["code"], "duplicate_identity")
        self.assertFalse(os.path.exists(self.credential))

    def test_cancel_terminates_provider_and_rolls_back(self):
        def login(*_args, **_kwargs):
            with open(self.credential, "w", encoding="utf-8") as handle:
                json.dump({"token": "new"}, handle)
            return RunningProcess()

        with mock.patch.object(connect, "_stop_login_process") as stop:
            value = self.run_login(
                popen=login, cancel_event=SequenceCancel(False, True))
        self.assertEqual(value["code"], "cancelled")
        stop.assert_called_once()
        self.assertFalse(os.path.exists(self.credential))

    def test_missing_and_unsupported_cli_are_actionable_before_mutation(self):
        with mock.patch.object(connect, "provider_binary", return_value=None):
            missing = connect.desktop_connect_fresh(
                self.config, "claude-1", "claude")
        self.assertEqual(missing["code"], "claude_cli_missing")
        unsupported = self.run_login(prerequisite=lambda provider, binary: False)
        self.assertEqual(unsupported["code"], "claude_upgrade_required")
        self.assertFalse(os.path.exists(self.home))

    def test_cli_failure_and_unreadable_identity_have_distinct_codes(self):
        failed = self.run_login(popen=lambda *args, **kwargs: FinishedProcess(7))
        unreadable = self.run_login(identity={})
        self.assertEqual(failed["code"], "provider_login_failed")
        self.assertEqual(unreadable["code"], "identity_unreadable")

    def test_timeout_terminates_provider_and_rolls_back(self):
        with mock.patch.object(connect.time, "monotonic", side_effect=[0, 2]), \
                mock.patch.object(connect, "_stop_login_process") as stop:
            value = self.run_login(
                popen=lambda *args, **kwargs: RunningProcess(), timeout=1)
        self.assertEqual(value["code"], "login_timed_out")
        stop.assert_called_once()

    def test_shared_keychain_guard_has_no_force_bypass(self):
        with mock.patch.object(connect, "provider_binary", return_value="/bin/claude"), \
                mock.patch.object(connect, "darwin_keychain_guard", return_value=False), \
                mock.patch.object(connect.subprocess, "Popen") as process:
            value = connect.desktop_connect_fresh(
                self.config, "claude-1", "claude",
                prerequisite=lambda provider, binary: True)
        self.assertEqual(value["code"], "claude_shared_keychain_conflict")
        process.assert_not_called()

    def test_failed_macos_login_removes_only_new_namespaced_keychain_item(self):
        with mock.patch.object(connect.sys, "platform", "darwin"), \
                mock.patch.object(connect, "delete_claude_keychain_item") as delete:
            exists = iter([False, True])
            value = self.run_login(
                keychain_exists=lambda _home: next(exists),
                expected_email="wrong@example.test")
        self.assertEqual(value["code"], "wrong_identity")
        delete.assert_called_once_with(self.home)

    def test_verified_macos_version_floor_precedes_auth_capability_probe(self):
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            result = completed()
            result.stdout = ("2.1.206 (Claude Code)" if argv[-1] == "--version"
                             else "login")
            return result

        with mock.patch.object(connect.sys, "platform", "darwin"):
            self.assertFalse(connect.desktop_login_prerequisite(
                "claude", "/bin/claude", runner=runner))
        self.assertEqual(calls, [["/bin/claude", "--version"]])

    def test_macos_refuses_success_without_an_isolated_credential(self):
        with mock.patch.object(connect.sys, "platform", "darwin"):
            missing = iter([False, False])
            value = self.run_login(keychain_exists=lambda _home: next(missing))
        self.assertEqual(value["code"], "claude_keychain_isolation_missing")
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_macos_refuses_to_overwrite_a_leftover_slot_keychain_item(self):
        with mock.patch.object(connect.sys, "platform", "darwin"):
            value = self.run_login(keychain_exists=True)
        self.assertEqual(value["code"], "claude_slot_keychain_occupied")
        self.assertFalse(os.path.exists(self.home))


if __name__ == "__main__":
    unittest.main()
