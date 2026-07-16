import io
import json
import socket
import subprocess
import time
import unittest
import urllib.error
from unittest import mock

from headroom import collect


class StableFailureTests(unittest.TestCase):
    def test_transport_server_and_payload_failures_have_stable_codes(self):
        cases = [
            (socket.timeout(), ("provider_timeout", True)),
            (subprocess.TimeoutExpired("provider", 1),
             ("provider_timeout", True)),
            (urllib.error.URLError("private network detail"),
             ("provider_offline", True)),
            (urllib.error.HTTPError("https://provider.invalid", 503, "raw",
                                    {}, io.BytesIO()),
             ("provider_server_error", True)),
            (json.JSONDecodeError("raw payload", "x", 0),
             ("malformed_provider_response", False)),
            (ValueError("usage percentage out of range"),
             ("malformed_provider_response", False)),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(collect._stable_collection_failure(error),
                                 expected)
            if isinstance(error, urllib.error.HTTPError):
                error.close()

    def test_transient_carryover_is_identity_bound_aged_and_not_routable(self):
        now = int(time.time())
        identity = {"account_fingerprint": "AAAA",
                    "credential_digest": "BBBB"}
        previous = {"accounts": [{
            "name": "slow", "provider": "claude", "ok": True,
            "routable": True, "trust_state": "verified",
            "identity": dict(identity), "captured_at": now - 30,
            "windows": {"5h": {"used_percent": 20}},
        }]}
        account = {"name": "slow", "provider": "claude"}
        row = collect._transient_carryover(
            previous, account, now, identity, "provider_offline")
        self.assertTrue(row["stale"])
        self.assertFalse(row["routable"])
        self.assertTrue(row["transient_carryover"])
        self.assertEqual(row["error_code"], "provider_offline")
        self.assertIn("last verified", row["note"])

    def test_changed_identity_cannot_carry_a_prior_reading(self):
        now = int(time.time())
        previous = {"accounts": [{
            "name": "slow", "provider": "claude", "ok": True,
            "routable": True, "trust_state": "verified",
            "identity": {"account_fingerprint": "AAAA",
                         "credential_digest": "BBBB"},
            "captured_at": now - 30, "windows": {},
        }]}
        row = collect._transient_carryover(
            previous, {"name": "slow", "provider": "claude"}, now,
            {"account_fingerprint": "CHANGED",
             "credential_digest": "BBBB"}, "provider_offline")
        self.assertIsNone(row)

    def test_offline_claude_read_publishes_only_aged_sanitized_carryover(self):
        now = int(time.time())
        account = {"name": "slow", "provider": "claude", "home": "/owned"}
        previous = {"accounts": [{
            "name": "slow", "provider": "claude", "ok": True,
            "routable": True, "trust_state": "verified",
            "identity": {"account_fingerprint": "AAAA",
                         "credential_digest": "BBBB"},
            "captured_at": now - 45,
            "windows": {
                "5h": {"used_percent": 20, "observed_at": now - 45},
                "7d": {"used_percent": 30, "observed_at": now - 45},
            },
        }]}
        identity = {"verified": False, "email": "safe@example.test",
                    "account_fingerprint": "AAAA", "method": "local"}
        with mock.patch.object(collect, "claude_identity",
                               return_value=identity), \
                mock.patch.object(collect, "credential_digest",
                                  return_value="BBBB"), \
                mock.patch.object(collect, "claude_plan", return_value="Pro"), \
                mock.patch.object(collect, "claude_limits",
                                  side_effect=urllib.error.URLError(
                                      "/private/network/detail")):
            snapshot = collect.collect([account], previous=previous)
        row = snapshot["accounts"][0]
        self.assertTrue(row["stale"])
        self.assertFalse(row["routable"])
        self.assertEqual(row["error_code"], "provider_offline")
        public = collect.public_snapshot(snapshot, redact_emails=True)
        self.assertNotIn("private", json.dumps(public))
        self.assertIn("last verified", public["accounts"][0]["note"])


class ConcurrentCollectionTests(unittest.TestCase):
    def test_slow_account_cannot_block_fast_result_or_change_order(self):
        accounts = [
            {"name": "slow", "provider": "claude"},
            {"name": "fast", "provider": "codex"},
        ]

        def one(rows, *_args, **_kwargs):
            account = rows[0]
            if account["name"] == "slow":
                time.sleep(0.15)
            now = int(time.time())
            return {
                "accounts": [{
                    "name": account["name"],
                    "provider": account["provider"],
                    "ok": True, "trust_state": "verified",
                    "identity": {"account_fingerprint": account["name"]},
                    "captured_at": now, "windows": {},
                }],
            }

        started = time.monotonic()
        with mock.patch.object(collect, "_collect_accounts_sequential",
                               side_effect=one):
            snapshot = collect.collect(
                accounts, deadline=0.03, max_workers=2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.12)
        self.assertEqual([row["name"] for row in snapshot["accounts"]],
                         ["slow", "fast"])
        self.assertEqual(snapshot["accounts"][0]["error_code"],
                         "provider_timeout")
        self.assertTrue(snapshot["accounts"][1]["ok"])


if __name__ == "__main__":
    unittest.main()
