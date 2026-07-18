import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from headroom import activity


TIMEZONE = "America/Los_Angeles"
ZONE = ZoneInfo(TIMEZONE)
NOW = dt.datetime(2026, 7, 16, 12, 0, tzinfo=ZONE).timestamp()


def line(value):
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def session_meta(identity="session-a", timestamp="2026-07-16T17:00:00Z"):
    return line({
        "timestamp": timestamp, "type": "session_meta",
        "payload": {"id": identity, "timestamp": timestamp},
    })


def codex_event(timestamp, total):
    return line({
        "timestamp": timestamp, "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"total_tokens": total * 50},
                "last_token_usage": {
                    "input_tokens": total - 10,
                    "cached_input_tokens": total - 20,
                    "output_tokens": 10,
                    "total_tokens": total,
                },
            },
        },
    })


def claude_event(timestamp, message_id, usage, session="claude-session"):
    return line({
        "type": "assistant", "timestamp": timestamp,
        "sessionId": session, "requestId": f"request-{message_id}",
        "message": {"id": message_id, "usage": usage},
    })


class ActivityMetrics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _write(self, filename, *rows):
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, "wb") as handle:
            for row in rows:
                handle.write(row)

    def _index(self, config, global_claude_home=""):
        self.assertTrue(activity._index_sync(
            config, now=NOW, timezone_name=TIMEZONE,
            global_claude_home=global_claude_home))
        return activity._project(config, now=NOW)

    def test_codex_counts_last_usage_events_and_deduplicates_transcript_copies(self):
        home = os.path.join(self.temp.name, "codex")
        original = os.path.join(home, "sessions", "original.jsonl")
        copied = os.path.join(home, "sessions", "copied.jsonl")
        first = codex_event("2026-07-16T17:01:00Z", 100)
        self._write(original, session_meta(), first,
                    codex_event("2026-07-16T18:01:00Z", 50))
        self._write(copied, session_meta(), first)
        config = {"accounts": [{
            "name": "codex1", "provider": "codex", "home": home,
        }]}

        value = self._index(config)

        account = value["accounts"][0]
        self.assertEqual(account["tokens"]["today"], {
            "value": 150, "coverage": "exact"})
        self.assertEqual(account["sessions"]["today"]["value"], 1)
        self.assertEqual(value["daily"], [{
            "date": "2026-07-16", "codex_tokens": 150,
            "claude_code_tokens": 0, "claude_code_calls": 0,
            "total": 150, "driver": "unlabeled", "evidence": "",
        }])

    def test_claude_uses_all_cache_fields_but_one_max_per_message_call(self):
        home = os.path.join(self.temp.name, "homes", "claude1")
        transcript = os.path.join(home, "projects", "one", "session.jsonl")
        self._write(
            transcript,
            claude_event("2026-07-16T17:00:00Z", "message-a", {
                "input_tokens": 10, "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 40,
            }),
            # One call is repeated in Claude transcripts. Keep its maximum,
            # rather than adding both rows.
            claude_event("2026-07-16T17:00:01Z", "message-a", {
                "input_tokens": 20, "output_tokens": 30,
                "cache_creation_input_tokens": 40,
                "cache_read_input_tokens": 60,
            }),
            claude_event("2026-07-16T18:00:00Z", "message-b", {
                "input_tokens": 20, "output_tokens": 30,
            }),
        )
        config = {"accounts": [{
            "name": "claude1", "provider": "claude", "home": home,
        }]}

        value = self._index(config)

        self.assertEqual(value["accounts"][0]["tokens"]["today"], {
            "value": 200, "coverage": "exact"})
        self.assertEqual(value["accounts"][0]["sessions"]["today"]["value"], 1)
        self.assertEqual(value["totals"]["calls"]["today"]["value"], 2)

    def test_shared_claude_history_is_exact_but_never_assigned_to_a_slot(self):
        owned = os.path.join(self.temp.name, "homes", "claude1")
        os.makedirs(os.path.join(owned, "projects"))
        shared = os.path.join(self.temp.name, "global-claude")
        transcript = os.path.join(shared, "projects", "one", "session.jsonl")
        self._write(transcript, claude_event(
            "2026-07-16T17:00:00Z", "global-message",
            {"input_tokens": 250, "output_tokens": 50}))
        config = {"accounts": [{
            "name": "claude1", "provider": "claude", "home": owned,
        }]}

        value = self._index(config, global_claude_home=shared)

        self.assertEqual(value["accounts"][0]["attribution"], "unavailable")
        self.assertEqual(value["accounts"][0]["tokens"]["today"]["value"], None)
        exact = value["unattributed"]["claude_code"]
        self.assertEqual(exact["tokens"]["today"], {
            "value": 300, "coverage": "exact"})
        self.assertEqual(exact["calls"]["today"]["value"], 1)
        self.assertIn("claude_history_unattributed", value["warnings"])

    def test_windows_are_local_calendar_days_not_rolling_hours(self):
        home = os.path.join(self.temp.name, "codex")
        transcript = os.path.join(home, "sessions", "one.jsonl")
        self._write(
            transcript, session_meta(),
            # 23:30 on July 15 in Los Angeles, despite July 16 UTC.
            codex_event("2026-07-16T06:30:00Z", 10),
            codex_event("2026-07-16T07:05:00Z", 20),
            codex_event("2026-07-10T18:00:00Z", 30),
            codex_event("2026-07-09T18:00:00Z", 40),
        )
        config = {"accounts": [{
            "name": "codex1", "provider": "codex", "home": home,
        }]}

        value = self._index(config)
        tokens = value["accounts"][0]["tokens"]

        self.assertEqual(tokens["today"]["value"], 20)
        self.assertEqual(tokens["7d"]["value"], 60)
        self.assertEqual(tokens["30d"]["value"], 100)
        self.assertEqual([row["date"] for row in value["daily"]], [
            "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12",
            "2026-07-13", "2026-07-14", "2026-07-15", "2026-07-16",
        ])

    def test_codex_session_without_token_events_is_a_visible_coverage_gap(self):
        home = os.path.join(self.temp.name, "codex")
        transcript = os.path.join(home, "sessions", "legacy.jsonl")
        self._write(transcript, session_meta())
        config = {"accounts": [{
            "name": "codex1", "provider": "codex", "home": home,
        }]}

        value = self._index(config)

        self.assertEqual(value["accounts"][0]["tokens"]["today"], {
            "value": 0, "coverage": "partial"})
        self.assertEqual(value["totals"]["tokens"]["today"], {
            "value": 0, "coverage": "partial"})
        self.assertIn("codex_legacy_usage_unavailable", value["warnings"])

    def test_incremental_refresh_is_idempotent_and_adds_only_new_events(self):
        home = os.path.join(self.temp.name, "codex")
        transcript = os.path.join(home, "sessions", "one.jsonl")
        first = codex_event("2026-07-16T17:00:00Z", 100)
        self._write(transcript, session_meta(), first)
        config = {"accounts": [{
            "name": "codex1", "provider": "codex", "home": home,
        }]}
        initial = self._index(config)
        self.assertEqual(initial["totals"]["tokens"]["today"]["value"], 100)

        with open(transcript, "ab") as handle:
            handle.write(first)
            handle.write(codex_event("2026-07-16T18:00:00Z", 25))
        updated = self._index(config)

        self.assertEqual(updated["totals"]["tokens"]["today"]["value"], 125)
        encoded = json.dumps(updated)
        self.assertNotIn(transcript, encoded)
        self.assertNotIn("session-a", encoded)
        self.assertEqual(os.stat(activity._state_path()).st_mode & 0o777, 0o600)

    def test_account_rename_rebuilds_historical_attribution(self):
        home = os.path.join(self.temp.name, "codex")
        transcript = os.path.join(home, "sessions", "one.jsonl")
        self._write(
            transcript, session_meta(),
            codex_event("2026-07-16T17:00:00Z", 100),
        )
        original = {"accounts": [{
            "name": "codex-old", "provider": "codex", "home": home,
        }]}
        renamed = {"accounts": [{
            "name": "codex-new", "provider": "codex", "home": home,
        }]}
        self._index(original)

        value = self._index(renamed)

        self.assertEqual(value["accounts"][0]["tokens"]["today"], {
            "value": 100, "coverage": "exact"})
        connection = activity._read_database()
        try:
            scopes = connection.execute(
                "select distinct scope from events").fetchall()
        finally:
            connection.close()
        self.assertEqual(scopes, [("codex-new",)])

    def test_removed_account_history_is_not_left_in_configured_totals(self):
        first_home = os.path.join(self.temp.name, "codex-one")
        second_home = os.path.join(self.temp.name, "codex-two")
        self._write(
            os.path.join(first_home, "sessions", "one.jsonl"),
            session_meta("session-one"),
            codex_event("2026-07-16T17:00:00Z", 100),
        )
        self._write(
            os.path.join(second_home, "sessions", "two.jsonl"),
            session_meta("session-two"),
            codex_event("2026-07-16T18:00:00Z", 200),
        )
        both = {"accounts": [
            {"name": "codex1", "provider": "codex", "home": first_home},
            {"name": "codex2", "provider": "codex", "home": second_home},
        ]}
        remaining = {"accounts": [
            {"name": "codex1", "provider": "codex", "home": first_home},
        ]}
        self._index(both)

        value = self._index(remaining)

        self.assertEqual(value["totals"]["tokens"]["today"], {
            "value": 100, "coverage": "exact"})

    def test_unindexed_projection_fails_closed(self):
        config = {"accounts": [{
            "name": "missing", "provider": "codex",
            "home": os.path.join(self.temp.name, "missing"),
        }]}

        value = activity._project(config, now=NOW)

        self.assertEqual(value["status"], "unavailable")
        self.assertEqual(value["accounts"][0]["tokens"]["today"], {
            "value": None, "coverage": "unavailable"})


if __name__ == "__main__":
    unittest.main()
