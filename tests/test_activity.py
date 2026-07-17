import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from headroom import activity


NOW = 1_800_000_000


class ActivityMetrics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_codex_uses_cumulative_deltas_without_rescanning_or_overcounting(self):
        home = os.path.join(self.temp.name, "codex")
        os.makedirs(home)
        database = os.path.join(home, "logs_2.sqlite")
        connection = sqlite3.connect(database)
        connection.execute(
            "create table logs (id integer primary key, ts integer, "
            "target text, feedback_log_body text, thread_id text)"
        )
        rows = [
            (1, NOW - 8 * 86400, "total_usage_tokens=100"),
            (2, NOW - 2 * 86400, "total_usage_tokens=400"),
            (3, NOW - 3600, "total_usage_tokens=550"),
        ]
        connection.executemany(
            "insert into logs values (?, ?, 'codex_core::session::turn', ?, 'thread-a')",
            rows,
        )
        connection.commit()
        config = {"accounts": [{
            "name": "codex1", "provider": "codex", "home": home,
        }]}

        first = activity.snapshot(config, now=NOW)
        account = first["accounts"][0]
        self.assertEqual(account["tokens"]["24h"], {
            "value": 150, "coverage": "complete"})
        self.assertEqual(account["tokens"]["7d"], {
            "value": 450, "coverage": "complete"})
        self.assertEqual(account["tokens"]["30d"], {
            "value": 450, "coverage": "partial"})
        self.assertEqual(account["sessions"]["24h"], {
            "value": 1, "coverage": "complete"})
        self.assertEqual(first["totals"]["tokens"]["7d"]["value"], 450)

        # The persisted cursor makes a no-change refresh idempotent, while a
        # later cumulative reading contributes only its positive delta.
        self.assertEqual(activity.snapshot(config, now=NOW), first)
        connection.execute(
            "insert into logs values (?, ?, 'codex_core::session::turn', ?, 'thread-a')",
            (4, NOW - 600, "total_usage_tokens=600"),
        )
        connection.commit()
        updated = activity.snapshot(config, now=NOW)
        self.assertEqual(updated["accounts"][0]["tokens"]["24h"]["value"], 200)
        encoded = json.dumps(updated)
        self.assertNotIn("thread-a", encoded)
        self.assertNotIn(database, encoded)
        connection.close()

    def test_claude_counts_only_attributable_slot_transcripts_incrementally(self):
        home = os.path.join(self.temp.name, "homes", "claude1")
        project = os.path.join(home, "projects", "project-a")
        os.makedirs(project)
        transcript = os.path.join(project, "session.jsonl")

        def event(timestamp, uuid, usage):
            return json.dumps({
                "type": "assistant", "timestamp": timestamp,
                "sessionId": "session-a", "uuid": uuid,
                "message": {"usage": usage},
            }) + "\n"

        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "assistant", "timestamp": "2027-01-15T05:00:00Z",
                "sessionId": "malformed", "message": "not-an-object",
            }) + "\n")
            handle.write(event("2027-01-15T06:00:00Z", "one", {
                "input_tokens": 10, "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            }))
            handle.write(event("2027-01-15T07:00:00Z", "two", {
                "input_tokens": 20, "output_tokens": 30,
            }))
        # A shared/global transcript is deliberately outside the registered
        # slot and must never be attributed to it.
        shared = os.path.join(self.temp.name, "shared.jsonl")
        with open(shared, "w", encoding="utf-8") as handle:
            handle.write(event("2027-01-15T07:30:00Z", "shared", {
                "input_tokens": 999_999,
            }))
        config = {"accounts": [{
            "name": "claude1", "provider": "claude", "home": home,
        }]}

        first = activity.snapshot(config, now=NOW)
        account = first["accounts"][0]
        self.assertEqual(account["tokens"]["24h"], {
            "value": 150, "coverage": "partial"})
        self.assertEqual(account["sessions"]["24h"], {
            "value": 1, "coverage": "partial"})

        with open(transcript, "a", encoding="utf-8") as handle:
            handle.write(event("2027-01-15T07:50:00Z", "three", {
                "input_tokens": 25,
            }))
        updated = activity.snapshot(config, now=NOW)
        self.assertEqual(updated["accounts"][0]["tokens"]["24h"]["value"], 175)
        self.assertEqual(updated["accounts"][0]["sessions"]["24h"]["value"], 1)

    def test_missing_source_is_unavailable_instead_of_zero(self):
        config = {"accounts": [{
            "name": "codex1", "provider": "codex",
            "home": os.path.join(self.temp.name, "missing"),
        }]}
        value = activity.snapshot(config, now=NOW)
        metric = value["accounts"][0]["tokens"]["24h"]
        self.assertEqual(metric, {"value": None, "coverage": "unavailable"})
        self.assertEqual(value["totals"]["tokens"]["24h"], metric)

    def test_private_state_write_failure_returns_bounded_unavailable_view(self):
        config = {"accounts": [{
            "name": "missing", "provider": "codex",
            "home": os.path.join(self.temp.name, "missing"),
        }]}
        with mock.patch.object(activity.paths, "write_json_atomic",
                               side_effect=OSError("disk full")):
            value = activity.snapshot(config, now=NOW)
        self.assertEqual(value, activity.unavailable(config))


if __name__ == "__main__":
    unittest.main()
