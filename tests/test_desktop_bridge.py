"""External-behavior tests for the versioned desktop stdio bridge."""

import io
import json
import os
import subprocess
import sys
import unittest

from headroom import desktop_bridge


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(request_id, command, args=None):
    return json.dumps({
        "schema": desktop_bridge.SCHEMA, "id": request_id,
        "command": command, "args": {} if args is None else args,
    })


class DesktopBridgeUnit(unittest.TestCase):
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


class DesktopBridgeSubprocess(unittest.TestCase):
    def run_bridge(self, lines):
        process = subprocess.run(
            [sys.executable, "-m", "headroom.desktop_bridge"], cwd=ROOT,
            input="\n".join(lines) + "\n", text=True, capture_output=True,
            timeout=10, check=False)
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


if __name__ == "__main__":
    unittest.main()
