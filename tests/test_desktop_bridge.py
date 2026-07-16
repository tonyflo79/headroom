"""External-behavior tests for the versioned desktop stdio bridge."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from headroom import desktop_bridge, paths, registry


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(request_id, command, args=None):
    return json.dumps({
        "schema": desktop_bridge.SCHEMA, "id": request_id,
        "command": command, "args": {} if args is None else args,
    })


class DesktopBridgeUnit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_fixture_is_sanitized_widget_contract(self):
        value = desktop_bridge.fixture_snapshot(now=1_800_000_000)
        self.assertEqual(value["schema"], "headroom_widget@1")
        self.assertEqual([row["provider"] for row in value["accounts"]],
                         ["claude", "codex"])
        encoded = json.dumps(value)
        for secret_field in ("email", "token", "credential", "home"):
            self.assertNotIn(secret_field, encoded.lower())

    def test_invalid_request_returns_stable_error(self):
        source = io.StringIO('{"id":"bad"}\n')
        target = io.StringIO()
        self.assertEqual(desktop_bridge.main(source, target), 0)
        value = json.loads(target.getvalue())
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"]["code"], "incompatible_schema")

    def test_first_discovery_discloses_before_any_provider_probe(self):
        found = [{"provider": "codex", "home": "/secret/codex",
                  "email": "person@example.com", "fingerprint": "private"}]
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=found) as detect, \
                mock.patch.object(desktop_bridge.connect,
                                  "provider_binary") as binary:
            value = desktop_bridge.discover_desktop(now=1_800_000_000)
        self.assertEqual(value["schema"], desktop_bridge.VIEW_SCHEMA)
        self.assertEqual(value["mode"], "onboarding")
        self.assertEqual(value["onboarding"]["step"], "welcome")
        self.assertEqual(value["candidates"], [])
        detect.assert_not_called()
        binary.assert_not_called()
        self.assertFalse(os.path.exists(paths.config_path()))
        self.assertFalse(os.path.exists(desktop_bridge._onboarding_path()))

    def test_begin_setup_persists_only_safe_progress_then_probes(self):
        found = [{"provider": "codex", "home": "/secret/codex",
                  "email": "person@example.com", "fingerprint": "private"}]
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=found), \
                mock.patch.object(desktop_bridge.connect, "provider_binary",
                                  side_effect=lambda provider: f"/bin/{provider}"), \
                mock.patch.object(desktop_bridge.connect,
                                  "desktop_login_prerequisite",
                                  return_value=True), \
                mock.patch.object(desktop_bridge.connect,
                                  "desktop_codex_prerequisite",
                                  return_value=False):
            value = desktop_bridge.onboarding_desktop(
                "begin", now=1_800_000_000)
        self.assertEqual(value["mode"], "onboarding")
        self.assertEqual(value["onboarding"]["step"], "providers")
        self.assertEqual(value["candidates"], [{
            "id": "existing-codex", "provider": "codex",
            "identity": "p***@example.com"}])
        states = {row["provider"]: row["state"]
                  for row in value["onboarding"]["providers"]}
        self.assertEqual(states, {"claude": "ready",
                                  "codex": "upgrade_required"})
        with open(desktop_bridge._onboarding_path(), encoding="utf-8") as handle:
            progress = json.load(handle)
        self.assertEqual(set(progress), {"schema", "step", "updated_at"})
        self.assertEqual(progress["step"], "providers")
        self.assertEqual(os.stat(desktop_bridge._onboarding_path()).st_mode & 0o777,
                         0o600)
        encoded = json.dumps(value)
        self.assertNotIn("/secret", encoded)
        self.assertNotIn("person@example.com", encoded)

    def test_all_provider_readiness_combinations_are_first_class(self):
        combinations = [
            ({"claude": "ready", "codex": "missing"},
             ("ready", "missing")),
            ({"claude": "missing", "codex": "ready"},
             ("missing", "ready")),
            ({"claude": "ready", "codex": "ready"},
             ("ready", "ready")),
            ({"claude": "missing", "codex": "missing"},
             ("missing", "missing")),
        ]
        for index, (states, expected) in enumerate(combinations):
            with self.subTest(states=states), mock.patch.object(
                    desktop_bridge.connect, "detect_existing", return_value=[]), \
                    mock.patch.object(desktop_bridge, "_provider_state",
                                      side_effect=lambda provider: states[provider]):
                if index:
                    desktop_bridge._save_onboarding("welcome")
                value = desktop_bridge.onboarding_desktop("begin")
            observed = {row["provider"]: row["state"]
                        for row in value["onboarding"]["providers"]}
            self.assertEqual((observed["claude"], observed["codex"]), expected)

    def test_demo_never_probes_or_creates_provider_or_registry_state(self):
        with mock.patch.object(desktop_bridge.connect, "detect_existing") as detect, \
                mock.patch.object(desktop_bridge.connect,
                                  "provider_binary") as binary:
            value = desktop_bridge.onboarding_desktop(
                "demo", now=1_800_000_000)
        self.assertEqual(value["mode"], "demo")
        self.assertEqual(value["onboarding"]["step"], "demo")
        self.assertEqual([row["state"] for row in value["accounts"]],
                         ["current", "current"])
        self.assertEqual({row["provider"] for row in value["accounts"]},
                         {"claude", "codex"})
        detect.assert_not_called()
        binary.assert_not_called()
        self.assertFalse(os.path.exists(paths.config_path()))
        self.assertNotIn("claude-demo@example.invalid", json.dumps(value))

    def test_interrupted_onboarding_resumes_without_a_login_job(self):
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=[]), \
                mock.patch.object(desktop_bridge, "_provider_state",
                                  return_value="missing"):
            desktop_bridge.onboarding_desktop("begin")
            accounts = desktop_bridge.onboarding_desktop("accounts")
            resumed = desktop_bridge.discover_desktop()
        self.assertEqual(accounts["onboarding"]["step"], "accounts")
        self.assertEqual(resumed["onboarding"]["step"], "accounts")
        self.assertTrue(resumed["onboarding"]["resumable"])
        self.assertIsNone(desktop_bridge.LOGIN_MANAGER._job)
        self.assertFalse(os.path.exists(paths.config_path()))

    def test_corrupt_onboarding_progress_restarts_read_only(self):
        paths.ensure_private(paths.state_dir())
        with open(desktop_bridge._onboarding_path(), "w", encoding="utf-8") as handle:
            handle.write("not-json")
        with open(desktop_bridge._onboarding_path(), encoding="utf-8") as handle:
            before = handle.read()
        with mock.patch.object(desktop_bridge.connect, "detect_existing") as detect:
            value = desktop_bridge.discover_desktop()
        self.assertEqual(value["mode"], "onboarding")
        self.assertEqual(value["onboarding"]["step"], "welcome")
        self.assertEqual(value["onboarding"]["recovery_code"],
                         "onboarding_progress_unreadable")
        detect.assert_not_called()
        with open(desktop_bridge._onboarding_path(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_onboarding_progress_never_follows_a_symlink(self):
        outside = os.path.join(self.temp.name, "outside.json")
        paths.ensure_private(paths.state_dir())
        with open(outside, "w", encoding="utf-8") as handle:
            json.dump({"schema": desktop_bridge.ONBOARDING_SCHEMA,
                       "step": "accounts"}, handle)
        os.symlink(outside, desktop_bridge._onboarding_path())
        with mock.patch.object(desktop_bridge.connect, "detect_existing") as detect:
            value = desktop_bridge.discover_desktop()
        self.assertEqual(value["onboarding"]["step"], "welcome")
        self.assertEqual(value["onboarding"]["recovery_code"],
                         "onboarding_progress_unreadable")
        detect.assert_not_called()

    def test_onboarding_rejects_skips_and_unknown_actions(self):
        with self.assertRaises(desktop_bridge.BridgeError) as skipped:
            desktop_bridge.onboarding_desktop("accounts")
        self.assertEqual(skipped.exception.code, "invalid_onboarding_transition")
        with self.assertRaises(desktop_bridge.BridgeError) as unknown:
            desktop_bridge.onboarding_desktop("provider raw command")
        self.assertEqual(unknown.exception.code, "invalid_onboarding_action")
        self.assertFalse(os.path.exists(desktop_bridge._onboarding_path()))

    def test_corrupt_registry_opens_recovery_without_overwrite(self):
        os.makedirs(self.temp.name, exist_ok=True)
        with open(paths.config_path(), "w", encoding="utf-8") as handle:
            handle.write("not-json")
        with open(paths.config_path(), encoding="utf-8") as handle:
            before = handle.read()
        value = desktop_bridge.discover_desktop(now=1_800_000_000)
        self.assertEqual(value["mode"], "recovery")
        self.assertEqual(value["recovery_code"], "registry_unreadable")
        with open(paths.config_path(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_discovery_hides_a_login_already_in_the_registry(self):
        registry.save({
            "schema_version": 1,
            "accounts": [{"name": "codex-main", "provider": "codex",
                          "home": "/existing/codex"}],
        })
        found = [{"provider": "codex", "home": "/existing/codex",
                  "email": "person@example.com"}]
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=found):
            value = desktop_bridge.discover_desktop(now=1_800_000_000)
        self.assertEqual(value["mode"], "ready")
        self.assertEqual(value["candidates"], [])
        self.assertEqual(value["accounts"][0]["state"], "held")
        self.assertEqual(value["accounts"][0]["note"],
                         "No collected reading yet")

    def test_desktop_projection_covers_account_states_and_reservation(self):
        now = 1_800_000_000
        config = {
            "schema_version": 1,
            "accounts": [
                {"name": "current", "provider": "claude", "home": "/a"},
                {"name": "limited", "provider": "codex", "home": "/b",
                 "reserved": True},
                {"name": "held", "provider": "claude", "home": "/c"},
                {"name": "stale", "provider": "claude", "home": "/d"},
                {"name": "offline", "provider": "claude", "home": "/e"},
            ],
        }

        def row(name, provider="claude", *, trust="verified", captured=now,
                used=20):
            windows = {"7d": {"used_percent": used, "observed_at": captured}}
            if provider == "claude":
                windows["5h"] = {"used_percent": used,
                                 "observed_at": captured}
            return {"name": name, "provider": provider, "ok": True,
                    "email": f"{name}@example.com", "plan": "Pro",
                    "trust_state": trust, "captured_at": captured,
                    "windows": windows}

        snapshot = {"generated": now, "accounts": [
            row("current"), row("limited", "codex", used=100),
            row("held", trust="unverified"),
            row("stale", captured=now - 2_000),
            {**row("offline"), "ok": False, "note": "provider unavailable",
             "error_code": "provider_auth_rejected"},
        ]}
        value = desktop_bridge._view(config, snapshot, now=now)
        states = {account["name"]: account["state"]
                  for account in value["accounts"]}
        self.assertEqual(states, {"current": "current", "limited": "limited",
                                  "held": "held", "stale": "stale",
                                  "offline": "held"})
        limited = next(row for row in value["accounts"]
                       if row["name"] == "limited")
        self.assertTrue(limited["reserved"])
        offline = next(row for row in value["accounts"]
                       if row["name"] == "offline")
        self.assertEqual(offline["note"], "provider unavailable")
        self.assertEqual(offline["diagnostic_code"],
                         "provider_auth_rejected")
        self.assertIsNone(desktop_bridge._diagnostic_code("../../raw-output"))
        self.assertEqual(limited["policy"]["position"], 1)
        self.assertTrue(limited["policy"]["home_retained_on_remove"])

    def test_account_actions_reserve_reorder_rename_and_confirm_remove(self):
        home_a = os.path.join(paths.homes_dir(), "a")
        home_b = os.path.join(paths.homes_dir(), "b")
        os.makedirs(home_a)
        os.makedirs(home_b)
        registry.save({"schema_version": 1, "accounts": [
            {"name": "a", "provider": "claude", "home": home_a},
            {"name": "b", "provider": "codex", "home": home_b},
        ]})
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=[]):
            reserved = desktop_bridge.account_action_desktop("reserve", "a")
            moved = desktop_bridge.account_action_desktop("move_up", "b")
            renamed = desktop_bridge.account_action_desktop(
                "rename", "a", new_name="primary")
            with self.assertRaises(desktop_bridge.BridgeError) as unconfirmed:
                desktop_bridge.account_action_desktop(
                    "remove", "primary", confirmation="wrong")
            removed = desktop_bridge.account_action_desktop(
                "remove", "primary", confirmation="primary")
        self.assertTrue(next(row for row in reserved["accounts"]
                             if row["name"] == "a")["reserved"])
        self.assertEqual([row["name"] for row in moved["accounts"]], ["b", "a"])
        primary = next(row for row in renamed["accounts"]
                       if row["name"] == "primary")
        self.assertEqual(primary["policy"]["home_kind"], "headroom")
        self.assertTrue(primary["policy"]["rename_keeps_home"])
        self.assertEqual(unconfirmed.exception.code,
                         "removal_confirmation_required")
        self.assertEqual([row["name"] for row in removed["accounts"]], ["b"])
        self.assertTrue(os.path.isdir(home_a))

    def test_reauthentication_job_is_available_only_for_safe_owned_home(self):
        manager = desktop_bridge.DesktopLoginManager()
        home = os.path.join(paths.homes_dir(), "codex-main")
        os.makedirs(home)
        config = {"schema_version": 1, "accounts": [{
            "name": "codex-main", "provider": "codex", "home": home,
            "expected_email": "private@example.test",
        }]}
        registry.save(config)
        finished = {"ok": True, "code": "reauthenticated",
                    "entry": config["accounts"][0], "observation": {
                        "email": "private@example.test", "plan": "plus",
                        "windows": {"7d": {"used_percent": 20}},
                    }}
        with mock.patch.object(
                desktop_bridge.connect, "desktop_connect_codex_device",
                return_value=finished):
            started = manager.start_reauthentication("codex-main")
            manager._job["thread"].join(timeout=2)
            value = manager.status(started["job_id"])
        self.assertEqual(value["mode"], "reauthenticate")
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["result_code"], "reauthenticated")
        self.assertNotIn("private@example.test", json.dumps(value))

    def test_desktop_boundary_always_redacts_identity(self):
        config = {"schema_version": 1,
                  "dashboard": {"redact_emails": False},
                  "accounts": [{"name": "one", "provider": "codex",
                                "home": "/one"}]}
        snapshot = {"generated": 1_800_000_000, "accounts": [{
            "name": "one", "provider": "codex", "ok": True,
            "email": "private@example.com", "trust_state": "verified",
            "captured_at": 1_800_000_000,
            "windows": {"7d": {"used_percent": 10}},
        }]}
        value = desktop_bridge._view(config, snapshot, now=1_800_000_000)
        self.assertFalse(value["settings"]["redact_emails"])
        self.assertEqual(value["accounts"][0]["identity"],
                         "p***@example.com")
        self.assertNotIn("private@example.com", json.dumps(value))

    def test_registry_order_overrides_stale_snapshot_order(self):
        config = {"schema_version": 1, "accounts": [
            {"name": "codex-first", "provider": "codex", "home": "/codex"},
            {"name": "claude-second", "provider": "claude", "home": "/claude"},
        ]}
        snapshot = {"generated": 1_800_000_000, "accounts": [
            {"name": "claude-second", "provider": "claude", "ok": True,
             "captured_at": 1_800_000_000, "windows": {}},
            {"name": "codex-first", "provider": "codex", "ok": True,
             "captured_at": 1_800_000_000, "windows": {}},
        ]}

        value = desktop_bridge._view(config, snapshot, now=1_800_000_000)

        self.assertEqual(
            [row["name"] for row in value["accounts"]],
            ["codex-first", "claude-second"],
        )

    def test_adopt_preserves_settings_and_returns_redacted_live_view(self):
        existing = {
            "schema_version": 1,
            "dashboard": {"title": "Keep Me", "theme": "light",
                          "redact_emails": True, "port": 9000},
            "routing": {"reserve_percent": 12, "auto_handoff": False},
            "accounts": [{"name": "old", "provider": "claude",
                          "home": "/old", "reserved": True}],
        }
        registry.save(existing)
        found = [{"provider": "codex", "home": "/secret/codex",
                  "email": "person@example.com"}]

        def adopt(config, name, provider, home, quiet=False):
            config["accounts"].append({
                "name": name, "provider": provider, "home": home,
                "expected_email": "person@example.com"})
            registry.save(config)
            return config["accounts"][-1]

        snapshot = {
            "schema_version": 1, "run_id": "desktop", "generated": 1_800_000_000,
            "generated_iso": "2027-01-15T08:00:00Z", "integrity_warnings": [],
            "accounts": [{
                "name": "codex-main", "provider": "codex", "ok": True,
                "email": "person@example.com", "plan": "ChatGPT Plus",
                "trust_state": "verified", "identity_verified": True,
                "captured_at": 1_800_000_000, "stale": False,
                "windows": {"7d": {"used_percent": 20,
                                      "resets_at": 1_800_086_400}},
            }],
        }
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=found), \
                mock.patch.object(desktop_bridge.connect, "connect_adopt",
                                  side_effect=adopt), \
                mock.patch.object(desktop_bridge.collector, "run_collect",
                                  return_value=snapshot):
            value = desktop_bridge.adopt_desktop(
                "existing-codex", "codex-main", now=1_800_000_000)
        saved = registry.load()
        self.assertEqual(saved["dashboard"], existing["dashboard"])
        self.assertEqual(saved["routing"], existing["routing"])
        self.assertEqual(value["settings"]["title"], "Keep Me")
        self.assertEqual(value["settings"]["theme"], "light")
        self.assertEqual(value["settings"]["reserve_percent"], 12)
        self.assertFalse(value["settings"]["auto_handoff"])
        account = next(row for row in value["accounts"]
                       if row["name"] == "codex-main")
        self.assertEqual(account["identity"], "p***@example.com")
        self.assertEqual(account["plan"], "ChatGPT Plus")

    def test_adopt_refuses_a_duplicate_name_before_mutation(self):
        registry.save({
            "schema_version": 1,
            "accounts": [{"name": "taken", "provider": "claude",
                          "home": "/old"}],
        })
        with mock.patch.object(desktop_bridge.connect, "detect_existing") as detect:
            with self.assertRaises(desktop_bridge.BridgeError) as raised:
                desktop_bridge.adopt_desktop("existing-codex", "taken")
        self.assertEqual(raised.exception.code, "duplicate_account_name")
        detect.assert_not_called()

    def test_claude_login_job_returns_only_stable_progress_and_sanitized_view(self):
        manager = desktop_bridge.DesktopLoginManager()
        finished = {"ok": True, "code": "connected", "entry": {
            "name": "claude-new", "expected_email": "private@example.com"}}
        safe_view = {"schema": desktop_bridge.VIEW_SCHEMA, "accounts": [{
            "name": "claude-new", "identity": "p***@example.com"}]}
        with mock.patch.object(desktop_bridge.connect, "desktop_connect_fresh",
                               return_value=finished), \
                mock.patch.object(desktop_bridge, "discover_desktop",
                                  return_value=safe_view):
            started = manager.start_claude("claude-new", "private@example.com")
            manager._job["thread"].join(timeout=2)
            value = manager.status(started["job_id"])
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["result_code"], "connected")
        self.assertEqual(value["view"], safe_view)
        self.assertNotIn("private@example.com", json.dumps(value))

    def test_claude_login_job_can_be_cancelled(self):
        manager = desktop_bridge.DesktopLoginManager()
        entered = threading.Event()

        def wait_for_cancel(config, name, provider, **options):
            entered.set()
            self.assertTrue(options["cancel_event"].wait(timeout=2))
            return {"ok": False, "code": "cancelled"}

        with mock.patch.object(desktop_bridge.connect, "desktop_connect_fresh",
                               side_effect=wait_for_cancel):
            started = manager.start_claude("claude-new")
            self.assertTrue(entered.wait(timeout=2))
            cancelling = manager.cancel(started["job_id"])
            self.assertEqual(cancelling["state"], "cancelling")
            manager._job["thread"].join(timeout=2)
            value = manager.status(started["job_id"])
        self.assertEqual(value["state"], "cancelled")
        self.assertEqual(value["result_code"], "cancelled")

    def test_codex_login_job_publishes_only_redacted_live_observation(self):
        manager = desktop_bridge.DesktopLoginManager()
        config = {
            "schema_version": 1,
            "accounts": [{"name": "codex-new", "provider": "codex",
                          "home": "/private/codex",
                          "expected_email": "private@example.com"}],
        }
        finished = {
            "ok": True, "code": "connected", "entry": config["accounts"][0],
            "observation": {
                "email": "private@example.com", "plan": "plus",
                "windows": {"7d": {"used_percent": 20,
                                    "observed_at": 1_800_000_000}},
            },
        }
        with mock.patch.object(
                desktop_bridge.connect, "desktop_connect_codex_device",
                return_value=finished), mock.patch.object(
                    desktop_bridge.registry, "load", return_value=config), \
                mock.patch.object(desktop_bridge.time, "time",
                                  return_value=1_800_000_000):
            started = manager.start_codex("codex-new", "private@example.com")
            manager._job["thread"].join(timeout=2)
            value = manager.status(started["job_id"])
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["result_code"], "connected")
        self.assertEqual(value["view"]["accounts"][0]["identity"],
                         "p***@example.com")
        self.assertEqual(value["view"]["accounts"][0]["plan"], "plus")
        self.assertNotIn("private@example.com", json.dumps(value))

    def test_claude_login_job_refuses_corrupt_registry(self):
        with open(paths.config_path(), "w", encoding="utf-8") as handle:
            handle.write("bad")
        manager = desktop_bridge.DesktopLoginManager()
        with self.assertRaises(desktop_bridge.BridgeError) as raised:
            manager.start_claude("claude-new")
        self.assertEqual(raised.exception.code, "recovery_required")


class DesktopBridgeSubprocess(unittest.TestCase):
    def run_bridge(self, lines, env=None):
        process = subprocess.run(
            [sys.executable, "-m", "headroom.desktop_bridge"], cwd=ROOT,
            input="\n".join(lines) + "\n", text=True, capture_output=True,
            timeout=10, check=False, env=env)
        return process, [json.loads(line) for line in process.stdout.splitlines()]

    def test_handshake_snapshot_shutdown_and_stdout_isolation(self):
        process, frames = self.run_bridge([
            request("1", "handshake", {
                "accepted_schemas": [desktop_bridge.SCHEMA]}),
            request("2", "fixture_snapshot", {"now": 1_800_000_000}),
            request("3", "shutdown"),
        ])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([frame["id"] for frame in frames], ["1", "2", "3"])
        self.assertTrue(all(frame["ok"] for frame in frames))
        self.assertEqual(frames[0]["result"]["bridge_schema"],
                         desktop_bridge.SCHEMA)
        self.assertEqual(frames[1]["result"]["schema"], "headroom_widget@1")
        self.assertIn("prepared sanitized fixture", process.stderr)
        self.assertNotIn("prepared sanitized fixture", process.stdout)

    def test_unknown_command_does_not_exit_bridge(self):
        process, frames = self.run_bridge([
            request("1", "not-a-command"), request("2", "shutdown")])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(frames[0]["error"]["code"], "unknown_command")
        self.assertTrue(frames[1]["ok"])

    def test_discovery_command_crosses_the_subprocess_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update({
                "HEADROOM_DIR": os.path.join(directory, "headroom"),
                "CLAUDE_CONFIG_DIR": os.path.join(directory, "no-claude"),
                "CODEX_HOME": os.path.join(directory, "no-codex"),
            })
            process, frames = self.run_bridge([
                request("1", "discover"), request("2", "shutdown")], env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(frames[0]["result"]["schema"],
                         desktop_bridge.VIEW_SCHEMA)
        self.assertEqual(frames[0]["result"]["mode"], "onboarding")
        self.assertEqual(frames[0]["result"]["onboarding"]["step"], "welcome")
        self.assertEqual(frames[0]["result"]["candidates"], [])

    def test_provider_free_demo_crosses_the_subprocess_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update({
                "HEADROOM_DIR": os.path.join(directory, "headroom"),
                "PATH": os.path.join(directory, "no-provider-bin"),
                "CLAUDE_CONFIG_DIR": os.path.join(directory, "no-claude"),
                "CODEX_HOME": os.path.join(directory, "no-codex"),
            })
            process, frames = self.run_bridge([
                request("1", "onboarding", {
                    "action": "demo", "now": 1_800_000_000}),
                request("2", "shutdown"),
            ], env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        demo = frames[0]["result"]
        self.assertEqual(demo["mode"], "demo")
        self.assertEqual(demo["onboarding"]["step"], "demo")
        self.assertEqual([row["state"] for row in demo["accounts"]],
                         ["current", "current"])
        encoded = json.dumps(demo)
        self.assertNotIn("claude-demo@example.invalid", encoded)
        self.assertNotIn("codex-demo@example.invalid", encoded)

    def test_claude_login_crosses_protocol_without_terminal_or_raw_output(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update({
                "HEADROOM_DIR": os.path.join(directory, "headroom"),
                "PATH": os.path.join(ROOT, "tests", "fixtures", "desktop-bin")
                        + os.pathsep + env.get("PATH", ""),
                "CLAUDE_CONFIG_DIR": os.path.join(directory, "no-claude"),
                "CODEX_HOME": os.path.join(directory, "no-codex"),
            })
            process = subprocess.Popen(
                [sys.executable, "-m", "headroom.desktop_bridge"], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env)
            process.stdin.write(request("1", "start_claude_login", {
                "name": "claude-fixture", "expected_email": "fixture@example.test"}) + "\n")
            process.stdin.flush()
            started = json.loads(process.stdout.readline())["result"]
            value = started
            for index in range(100):
                if value["state"] not in {"running", "cancelling"}:
                    break
                process.stdin.write(request(str(index + 2), "login_status", {
                    "job_id": started["job_id"]}) + "\n")
                process.stdin.flush()
                value = json.loads(process.stdout.readline())["result"]
                time.sleep(0.02)
            process.stdin.write(request("stop", "shutdown") + "\n")
            process.stdin.flush()
            json.loads(process.stdout.readline())
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["result_code"], "connected")
        self.assertEqual(value["view"]["accounts"][0]["identity"],
                         "f***@example.test")
        self.assertNotIn("fixture-org", json.dumps(value))

    def test_codex_device_login_crosses_protocol_with_live_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update({
                "HEADROOM_DIR": os.path.join(directory, "headroom"),
                "PATH": os.path.join(ROOT, "tests", "fixtures", "desktop-bin")
                        + os.pathsep + env.get("PATH", ""),
                "CLAUDE_CONFIG_DIR": os.path.join(directory, "no-claude"),
                "CODEX_HOME": os.path.join(directory, "no-codex"),
            })
            process = subprocess.Popen(
                [sys.executable, "-m", "headroom.desktop_bridge"], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env)
            process.stdin.write(request("1", "start_codex_login", {
                "name": "codex-fixture",
                "expected_email": "fixture@example.test"}) + "\n")
            process.stdin.flush()
            started = json.loads(process.stdout.readline())["result"]
            value = started
            for index in range(100):
                if value["state"] not in {"running", "cancelling"}:
                    break
                process.stdin.write(request(str(index + 2), "login_status", {
                    "job_id": started["job_id"]}) + "\n")
                process.stdin.flush()
                value = json.loads(process.stdout.readline())["result"]
                time.sleep(0.02)
            process.stdin.write(request("stop", "shutdown") + "\n")
            process.stdin.flush()
            json.loads(process.stdout.readline())
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(value["state"], "succeeded")
        self.assertEqual(value["result_code"], "connected")
        account = value["view"]["accounts"][0]
        self.assertEqual(account["identity"], "f***@example.test")
        self.assertEqual(account["state"], "current")
        self.assertEqual(set(account["windows"]), {"5h", "7d"})
        self.assertNotIn("fixture-refresh", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
