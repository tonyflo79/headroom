"""Private, incremental account activity accounting.

Only numeric token usage and hashed session membership leave this module. Raw
transcripts, provider payloads, paths, thread IDs, and event IDs remain in the
0600 private state file and never cross the desktop bridge.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

from . import paths, registry


SCHEMA = "headroom_activity@1"
STATE_SCHEMA = "headroom_activity_state@1"
PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
CODEX_TARGET = "codex_core::session::turn"
CODEX_TOTAL_RE = re.compile(r"(?:^|\s)total_usage_tokens=(\d+)(?:\s|$)")
MAX_TOKEN_VALUE = 10**15
MAX_CLAUDE_READ_BYTES = 64 * 1024 * 1024
RETENTION_SECONDS = 32 * 86400


def _state_path():
    return os.path.join(paths.state_dir(), "activity.json")


def _new_state(now):
    return {
        "schema": STATE_SCHEMA,
        "started_at": int(now),
        "accounts": {},
    }


def _load_state(now):
    value = paths.load_json(_state_path())
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA \
            or not isinstance(value.get("accounts"), dict):
        return _new_state(now)
    started = value.get("started_at")
    if not isinstance(started, int) or isinstance(started, bool) or started <= 0:
        return _new_state(now)
    return value


def _account_state(state, account):
    name = account["name"]
    provider = account["provider"]
    current = state["accounts"].get(name)
    if not isinstance(current, dict) or current.get("provider") != provider:
        current = {
            "provider": provider,
            "coverage_start": None,
            "source_complete": False,
            "buckets": {},
            "source": {},
        }
        state["accounts"][name] = current
    if not isinstance(current.get("buckets"), dict):
        current["buckets"] = {}
    if not isinstance(current.get("source"), dict):
        current["source"] = {}
    return current


def _session_hash(account_name, provider, session_id):
    material = f"{provider}\0{account_name}\0{session_id}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def _add_event(account, timestamp, tokens, session_id):
    if not isinstance(tokens, int) or isinstance(tokens, bool) \
            or not 0 < tokens <= MAX_TOKEN_VALUE:
        return
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) \
            or not math.isfinite(timestamp) or timestamp <= 0:
        return
    minute = str(int(timestamp) // 60 * 60)
    bucket = account["buckets"].setdefault(
        minute, {"tokens": 0, "sessions": []})
    if not isinstance(bucket, dict):
        bucket = {"tokens": 0, "sessions": []}
        account["buckets"][minute] = bucket
    current_tokens = bucket.get("tokens")
    if not isinstance(current_tokens, int) or isinstance(current_tokens, bool) \
            or current_tokens < 0:
        current_tokens = 0
    bucket["tokens"] = min(
        MAX_TOKEN_VALUE, current_tokens + tokens)
    hashed = _session_hash(
        account.get("name", "unknown"), account["provider"], str(session_id))
    sessions = bucket.setdefault("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
        bucket["sessions"] = sessions
    if hashed not in sessions:
        sessions.append(hashed)


def _prune(account, now):
    cutoff = int(now) - RETENTION_SECONDS
    kept = {}
    for key, value in account.get("buckets", {}).items():
        try:
            stamp = int(key)
        except (TypeError, ValueError):
            continue
        if stamp >= cutoff and isinstance(value, dict):
            kept[str(stamp)] = value
    account["buckets"] = kept


def _codex_database(home):
    for candidate in (
            os.path.join(home, "logs_2.sqlite"),
            os.path.join(home, "sqlite", "logs_2.sqlite")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _update_codex(account, configured, now):
    home = registry.expand(configured["home"])
    database = _codex_database(home)
    if database is None:
        return False
    source = account["source"]
    try:
        stat = os.stat(database)
    except OSError:
        return False
    identity = f"{stat.st_dev}:{stat.st_ino}"
    if source.get("database_identity") not in (None, identity):
        # A rotated telemetry database breaks continuity. Preserve already
        # counted buckets but restart coverage and cumulative baselines.
        source.clear()
        account["coverage_start"] = int(now)
        account["source_complete"] = False
    source["database_identity"] = identity
    cursor = source.get("last_id", 0)
    cursor = cursor if isinstance(cursor, int) and cursor >= 0 else 0
    totals = source.get("thread_totals")
    totals = totals if isinstance(totals, dict) else {}
    initial_import = source.get("initialized") is not True
    minimum = None
    maximum_id = cursor
    try:
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            rows = connection.execute(
                "select id, ts, thread_id, feedback_log_body from logs "
                "where id > ? and target = ? and "
                "feedback_log_body like '%total_usage_tokens=%' order by id",
                (cursor, CODEX_TARGET),
            )
            for event_id, timestamp, thread_id, body in rows:
                if isinstance(event_id, int):
                    maximum_id = max(maximum_id, event_id)
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                if not isinstance(body, str):
                    continue
                match = CODEX_TOTAL_RE.search(body)
                if match is None:
                    continue
                total = int(match.group(1))
                if total > MAX_TOKEN_VALUE:
                    continue
                try:
                    stamp = int(timestamp)
                except (TypeError, ValueError, OverflowError):
                    continue
                minimum = stamp if minimum is None else min(minimum, stamp)
                previous = totals.get(thread_id)
                delta = 0
                if isinstance(previous, int) and total >= previous:
                    delta = total - previous
                elif previous is None and not initial_import:
                    # A thread first observed after tracking began contributes
                    # its complete cumulative total.
                    delta = total
                totals[thread_id] = total
                if delta:
                    account["name"] = configured["name"]
                    _add_event(account, stamp, delta, thread_id)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return account.get("coverage_start") is not None
    source.update({
        "last_id": maximum_id,
        "thread_totals": totals,
        "initialized": True,
    })
    if account.get("coverage_start") is None:
        account["coverage_start"] = minimum if minimum is not None else int(now)
    account["source_complete"] = True
    return True


def _owned_claude_home(home):
    try:
        root = os.path.realpath(paths.homes_dir())
        resolved = os.path.realpath(home)
        return os.path.commonpath((root, resolved)) == root \
            and resolved != root and not os.path.islink(home)
    except (OSError, ValueError):
        return False


def _claude_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = parsed.timestamp()
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, OverflowError):
        return None


def _claude_tokens(value):
    if not isinstance(value, dict):
        return 0
    total = 0
    for key in TOKEN_FIELDS:
        count = value.get(key, 0)
        if isinstance(count, int) and not isinstance(count, bool) \
                and 0 <= count <= MAX_TOKEN_VALUE:
            total += count
    return min(total, MAX_TOKEN_VALUE)


def _claude_files(home):
    project_root = os.path.join(home, "projects")
    if not os.path.isdir(project_root):
        return []
    found = []
    for root, directories, filenames in os.walk(project_root, followlinks=False):
        directories[:] = [name for name in directories
                          if not os.path.islink(os.path.join(root, name))]
        for filename in filenames:
            candidate = os.path.join(root, filename)
            if filename.endswith(".jsonl") and not os.path.islink(candidate):
                found.append(candidate)
    return sorted(found)


def _read_claude_file(account, configured, filename, cursor, read_budget):
    try:
        stat = os.stat(filename)
        identity = f"{stat.st_dev}:{stat.st_ino}"
        offset = cursor.get("offset", 0) if cursor.get("identity") == identity else 0
        if not isinstance(offset, int) or offset < 0 or offset > stat.st_size:
            offset = 0
        remaining = stat.st_size - offset
        with open(filename, "rb") as handle:
            handle.seek(offset)
            data = handle.read(min(remaining, max(0, read_budget)))
    except OSError:
        return cursor, None, False, 0
    boundary = data.rfind(b"\n")
    if boundary < 0:
        return ({"identity": identity, "offset": offset}, None,
                remaining == 0, len(data))
    complete = data[:boundary + 1]
    minimum = None
    for raw in complete.splitlines():
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        tokens = _claude_tokens(usage)
        stamp = _claude_timestamp(event.get("timestamp"))
        if not tokens or stamp is None:
            continue
        session = event.get("sessionId") or event.get("session_id") or filename
        account["name"] = configured["name"]
        _add_event(account, stamp, tokens, session)
        minimum = stamp if minimum is None else min(minimum, stamp)
    next_offset = offset + boundary + 1
    fully_read = next_offset >= stat.st_size
    return ({"identity": identity, "offset": next_offset}, minimum,
            fully_read, len(data))


def _update_claude(account, configured, now):
    home = registry.expand(configured["home"])
    if not os.path.isdir(home) or not _owned_claude_home(home):
        return False
    source = account["source"]
    cursors = source.get("files")
    cursors = cursors if isinstance(cursors, dict) else {}
    minimum = None
    complete = True
    active = {}
    read_budget = MAX_CLAUDE_READ_BYTES
    for filename in _claude_files(home):
        cursor = cursors.get(filename)
        cursor = cursor if isinstance(cursor, dict) else {}
        updated, observed, fully_read, consumed = _read_claude_file(
            account, configured, filename, cursor, read_budget)
        read_budget = max(0, read_budget - consumed)
        active[filename] = updated
        complete = complete and fully_read
        if observed is not None:
            minimum = observed if minimum is None else min(minimum, observed)
    source.update({"files": active, "initialized": True})
    if account.get("coverage_start") is None:
        account["coverage_start"] = minimum if minimum is not None else int(now)
    account["source_complete"] = complete
    return True


def _metric(account, period, now, field):
    coverage_start = account.get("coverage_start")
    if not isinstance(coverage_start, (int, float)):
        return {"value": None, "coverage": "unavailable"}
    cutoff = int(now) - PERIODS[period]
    tokens = 0
    sessions = set()
    for key, bucket in account.get("buckets", {}).items():
        try:
            stamp = int(key)
        except (TypeError, ValueError):
            continue
        if stamp < cutoff or not isinstance(bucket, dict):
            continue
        count = bucket.get("tokens")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            tokens += count
        bucket_sessions = bucket.get("sessions", [])
        if not isinstance(bucket_sessions, list):
            continue
        for session in bucket_sessions:
            if isinstance(session, str) and len(session) == 16:
                sessions.add(session)
    value = tokens if field == "tokens" else len(sessions)
    if account.get("source_complete") is True and coverage_start <= cutoff:
        coverage = "complete"
    elif value > 0:
        coverage = "partial"
    else:
        coverage = "tracking"
    return {"value": value, "coverage": coverage}


def _combined(metrics):
    available = [row for row in metrics if row["value"] is not None]
    if not available:
        return {"value": None, "coverage": "unavailable"}
    value = sum(row["value"] for row in available)
    coverages = {row["coverage"] for row in metrics}
    if coverages == {"complete"}:
        coverage = "complete"
    elif value > 0:
        coverage = "partial"
    else:
        coverage = "tracking"
    return {"value": value, "coverage": coverage}


def _unavailable_account(account):
    metric = {period: {"value": None, "coverage": "unavailable"}
              for period in PERIODS}
    return {
        "name": account["name"], "provider": account["provider"],
        "tokens": metric,
        "sessions": {period: dict(value) for period, value in metric.items()},
    }


def unavailable(config):
    """Fail-closed projection for non-ready surfaces or collector failures."""
    accounts = [row for row in (config or {}).get("accounts", [])
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and row.get("provider") in {"claude", "codex"}]
    projected = [_unavailable_account(account) for account in accounts]
    metric = {"value": None, "coverage": "unavailable"}
    return {
        "schema": SCHEMA,
        "tracking_started_at": None,
        "accounts": projected,
        "totals": {
            field: {period: dict(metric) for period in PERIODS}
            for field in ("tokens", "sessions")
        },
        "commits": dict(metric),
        "pull_requests": dict(metric),
    }


def snapshot(config, now=None):
    """Update the private cursor state and return one bounded projection."""
    now = time.time() if now is None else float(now)
    accounts = [row for row in (config or {}).get("accounts", [])
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and row.get("provider") in {"claude", "codex"}
                and isinstance(row.get("home"), str)]
    state = _load_state(now)
    projected = []
    active_names = set()
    for configured in accounts:
        active_names.add(configured["name"])
        private = _account_state(state, configured)
        available = (_update_codex(private, configured, now)
                     if configured["provider"] == "codex"
                     else _update_claude(private, configured, now))
        _prune(private, now)
        private.pop("name", None)
        if not available:
            projected.append(_unavailable_account(configured))
            continue
        projected.append({
            "name": configured["name"], "provider": configured["provider"],
            "tokens": {period: _metric(private, period, now, "tokens")
                       for period in PERIODS},
            "sessions": {period: _metric(private, period, now, "sessions")
                         for period in PERIODS},
        })
    state["accounts"] = {
        name: value for name, value in state["accounts"].items()
        if name in active_names
    }
    try:
        paths.ensure_private(paths.state_dir())
        paths.write_json_atomic(_state_path(), state, mode=0o600)
    except (OSError, TypeError, ValueError):
        return unavailable(config)
    totals = {field: {
        period: _combined([row[field][period] for row in projected])
        for period in PERIODS
    } for field in ("tokens", "sessions")}
    missing = {"value": None, "coverage": "unavailable"}
    return {
        "schema": SCHEMA,
        "tracking_started_at": state["started_at"],
        "accounts": projected,
        "totals": totals,
        "commits": dict(missing),
        "pull_requests": dict(missing),
    }
