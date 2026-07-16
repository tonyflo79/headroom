"""Structured Codex device-auth contract for the native desktop app."""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from headroom import connect, paths, registry


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_BIN = os.path.join(ROOT, "tests", "fixtures", "desktop-bin")
FAKE_CODEX = os.path.join(FIXTURE_BIN, "codex")


class CodexDesktopLogin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "HEADROOM_DIR": self.temp.name,
            "PATH": FIXTURE_BIN + os.pathsep + os.environ.get("PATH", ""),
        })
        self.env.start()
        self.config = {
            "schema_version": 1,
            "dashboard": dict(registry.DEFAULT_DASHBOARD),
            "accounts": [],
        }
        self.home = os.path.join(paths.homes_dir(), "codex-new")
        self.auth = os.path.join(self.home, "auth.json")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def login(self, **options):
        with mock.patch.object(connect, "provider_binary", return_value=FAKE_CODEX):
            return connect.desktop_connect_codex_device(
                self.config, "codex-new", **options)

    def test_prerequisite_requires_known_structured_protocol_floor(self):
        self.assertTrue(connect.desktop_codex_prerequisite(FAKE_CODEX))
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})
        calls = []

        def old_runner(argv, **_kwargs):
            calls.append(argv)
            result = completed()
            result.stdout = "codex-cli 0.143.0"
            return result

        self.assertFalse(connect.desktop_codex_prerequisite(
            FAKE_CODEX, runner=old_runner))
        self.assertEqual(calls, [[FAKE_CODEX, "--version"]])

    def test_device_instructions_are_exactly_allowlisted(self):
        safe = {"type": "chatgptDeviceCode", "loginId": "login",
                "userCode": "ABCD-EFGH",
                "verificationUrl": "https://auth.openai.com/codex/device"}
        self.assertEqual(connect._device_instructions(safe), {
            "login_id": "login", "user_code": "ABCD-EFGH",
            "verification_url": safe["verificationUrl"]})
        for unsafe in (
            "http://auth.openai.com/codex/device",
            "https://auth.openai.com.evil.test/codex/device",
            "https://user@auth.openai.com/codex/device",
            "https://auth.openai.com/other",
            "https://auth.openai.com/codex/device?next=evil",
            "https://auth.openai.com:invalid/codex/device",
        ):
            self.assertIsNone(connect._device_instructions({
                **safe, "verificationUrl": unsafe}), unsafe)

    def test_success_requires_live_identity_and_capacity_before_publish(self):
        progress = []
        value = self.login(progress=lambda code, details=None:
                           progress.append((code, details)))
        self.assertEqual(value["code"], "connected")
        self.assertEqual([row[0] for row in progress],
                         ["preflight", "device_code", "verifying_identity"])
        self.assertEqual(progress[1][1], {
            "verification_url": "https://auth.openai.com/codex/device",
            "user_code": "ABCD-EFGH"})
        self.assertEqual(value["observation"]["email"], "fixture@example.test")
        self.assertEqual(set(value["observation"]["windows"]), {"5h", "7d"})
        saved = registry.load()
        self.assertEqual(saved["accounts"][0]["name"], "codex-new")
        self.assertEqual(saved["accounts"][0]["expected_email"],
                         "fixture@example.test")
        self.assertEqual(os.stat(self.auth).st_mode & 0o777, 0o600)

    def test_api_key_is_refused_as_non_subscription_and_rolled_back(self):
        with mock.patch.dict(os.environ, {"HEADROOM_FAKE_CODEX_AUTH": "apikey"}):
            value = self.login()
        self.assertEqual(value["code"], "api_key_not_subscription")
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_rejected_and_malformed_device_flows_are_distinct_and_rollback(self):
        for mode, code in (("rejected", "device_authorization_rejected"),
                           ("malformed", "device_instructions_malformed")):
            with self.subTest(mode=mode), mock.patch.dict(
                    os.environ, {"HEADROOM_FAKE_CODEX_RESULT": mode}):
                value = self.login()
            self.assertEqual(value["code"], code)
            self.assertFalse(os.path.exists(self.auth))
            self.assertFalse(os.path.exists(paths.config_path()))

    def test_cancel_sends_structured_cancel_and_rolls_back(self):
        cancel = threading.Event()
        cancel_log = os.path.join(self.temp.name, "cancel.log")

        def progress(code, _details=None):
            if code == "device_code":
                cancel.set()

        with mock.patch.dict(os.environ, {
                "HEADROOM_FAKE_CODEX_RESULT": "wait",
                "HEADROOM_FAKE_CODEX_CANCEL_LOG": cancel_log}):
            value = self.login(cancel_event=cancel, progress=progress)
        self.assertEqual(value["code"], "cancelled")
        with open(cancel_log, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "fixture-login")
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_expired_device_code_rolls_back_without_a_registry_slot(self):
        with mock.patch.dict(os.environ,
                             {"HEADROOM_FAKE_CODEX_RESULT": "wait"}):
            value = self.login(timeout=0.01)
        self.assertEqual(value["code"], "device_code_expired")
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_cancel_during_live_verification_prevents_publish(self):
        cancel = threading.Event()

        def live_reader(_home, expected_email=None, now=None):
            cancel.set()
            return ({"verified": True, "email": "fixture@example.test",
                     "account_fingerprint": "fixture"}, "plus", {
                         "7d": {"used_percent": 40, "observed_at": now}})

        value = self.login(cancel_event=cancel, live_reader=live_reader)
        self.assertEqual(value["code"], "cancelled")
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_wrong_identity_restores_exact_prior_auth(self):
        os.makedirs(self.home)
        original = {"auth_mode": "chatgpt", "tokens": {"id_token": "old"}}
        with open(self.auth, "w", encoding="utf-8") as handle:
            json.dump(original, handle)
        value = self.login(expected_email="other@example.test")
        self.assertEqual(value["code"], "identity_rejected")
        with open(self.auth, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), original)
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_duplicate_live_identity_is_refused_before_registry_write(self):
        with mock.patch.object(connect, "existing_fingerprints",
                               return_value={
                                   connect.collector.fingerprint("fixture-account"):
                                   "existing"}):
            value = self.login()
        self.assertEqual(value["code"], "duplicate_identity")
        self.assertFalse(os.path.exists(self.auth))
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_missing_and_unsupported_cli_fail_before_creating_home(self):
        with mock.patch.object(connect, "provider_binary", return_value=None):
            missing = connect.desktop_connect_codex_device(
                self.config, "codex-new")
        unsupported = self.login(prerequisite=lambda _binary: False)
        self.assertEqual(missing["code"], "codex_cli_missing")
        self.assertEqual(unsupported["code"], "codex_upgrade_required")
        self.assertFalse(os.path.exists(self.home))


if __name__ == "__main__":
    unittest.main()
