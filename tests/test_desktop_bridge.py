"""External-behavior tests for the versioned desktop stdio bridge."""

import io
import json
import os
import subprocess
import sys
import tempfile
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

    def test_discovery_is_sanitized_and_does_not_create_state(self):
        found = [{"provider": "codex", "home": "/secret/codex",
                  "email": "person@example.com", "fingerprint": "private"}]
        with mock.patch.object(desktop_bridge.connect, "detect_existing",
                               return_value=found):
            value = desktop_bridge.discover_desktop(now=1_800_000_000)
        self.assertEqual(value["schema"], desktop_bridge.VIEW_SCHEMA)
        self.assertEqual(value["mode"], "empty")
        self.assertEqual(value["candidates"], [{
            "id": "existing-codex", "provider": "codex",
            "identity": "p***@example.com"}])
        self.assertFalse(os.path.exists(paths.config_path()))
        encoded = json.dumps(value)
        self.assertNotIn("/secret", encoded)
        self.assertNotIn("person@example.com", encoded)

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
            {**row("offline"), "ok": False, "note": "provider unavailable"},
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
        self.assertEqual(frames[0]["result"]["mode"], "empty")
        self.assertEqual(frames[0]["result"]["candidates"], [])


if __name__ == "__main__":
    unittest.main()
