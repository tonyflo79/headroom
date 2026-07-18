"""Exact, private token-burn indexing for the desktop app.

The index stores only numeric usage, dates, opaque hashes, and private source
paths in a mode-0600 SQLite file. Raw transcripts and prompts are never copied.
Only bounded, normalized daily totals cross the desktop bridge.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import paths, registry


SCHEMA = "headroom_daily_burn@1"
STATE_SCHEMA = "headroom_activity_index@2"
WINDOWS = ("today", "7d", "30d")
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
MAX_TOKEN_VALUE = 10**15
MAX_DAILY_ROWS = 800
REFRESH_SECONDS = 60
UNATTRIBUTED = "unattributed"

_WORKER_LOCK = threading.Lock()
_WORKER = None
_WORKER_SIGNATURE = None


def _state_path():
    return os.path.join(paths.state_dir(), "activity-v2.sqlite")


def _timezone_name():
    configured = os.environ.get("TZ")
    if configured and not configured.startswith(":"):
        try:
            ZoneInfo(configured)
            return configured
        except ZoneInfoNotFoundError:
            pass
    try:
        resolved = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in resolved:
            candidate = resolved.split(marker, 1)[1]
            ZoneInfo(candidate)
            return candidate
    except (OSError, ZoneInfoNotFoundError):
        pass
    return "UTC"


def _database(filename=None):
    filename = filename or _state_path()
    paths.ensure_private(os.path.dirname(filename))
    connection = sqlite3.connect(filename, timeout=5)
    try:
        os.chmod(filename, 0o600)
    except OSError:
        connection.close()
        raise
    connection.execute("pragma trusted_schema=off")
    connection.execute("pragma journal_mode=delete")
    connection.execute("pragma synchronous=normal")
    connection.executescript("""
        create table if not exists meta (
            key text primary key,
            value text not null
        );
        create table if not exists sources (
            path text primary key,
            provider text not null,
            scope text not null,
            device integer not null,
            inode integer not null,
            offset integer not null,
            size integer not null,
            has_events integer not null,
            session_key text,
            session_date text,
            updated_at integer not null
        );
        create table if not exists events (
            provider text not null,
            event_key text not null,
            scope text not null,
            local_date text not null,
            tokens integer not null,
            session_key text not null,
            primary key (provider, event_key)
        );
        create index if not exists events_daily
            on events(local_date, provider, scope);
    """)
    return connection


def _read_database(filename=None):
    filename = filename or _state_path()
    if not os.path.isfile(filename):
        raise OSError("activity index is unavailable")
    uri = Path(filename).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    connection.execute("pragma query_only=on")
    connection.execute("pragma trusted_schema=off")
    return connection


def _meta(connection, key, default=None):
    row = connection.execute(
        "select value from meta where key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def _set_meta(connection, key, value):
    connection.execute(
        "insert into meta(key, value) values (?, ?) "
        "on conflict(key) do update set value=excluded.value",
        (key, str(value)),
    )


def _timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = parsed.timestamp()
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, OverflowError):
        return None


def _local_date(timestamp, timezone):
    return dt.datetime.fromtimestamp(timestamp, timezone).date().isoformat()


def _bounded_integer(value):
    return (value if isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= MAX_TOKEN_VALUE else None)


def _hash(*values):
    digest = hashlib.sha256()
    for value in values:
        if isinstance(value, bytes):
            digest.update(value)
        else:
            digest.update(str(value).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def _codex_event(raw, timezone, session_key):
    if b'"token_count"' not in raw or b'"last_token_usage"' not in raw:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    payload = value.get("payload") if isinstance(value, dict) else None
    if value.get("type") != "event_msg" or not isinstance(payload, dict) \
            or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("last_token_usage") if isinstance(info, dict) else None
    tokens = (_bounded_integer(usage.get("total_tokens"))
              if isinstance(usage, dict) else None)
    stamp = _timestamp(value.get("timestamp"))
    if not tokens or stamp is None:
        return None
    # Hashing the complete event line deduplicates transcript forks/copies
    # without retaining an event ID or any transcript content.
    return {
        "event_key": _hash(raw.rstrip(b"\r\n")),
        "local_date": _local_date(stamp, timezone),
        "tokens": tokens,
        "session_key": session_key,
    }


def _codex_session(raw):
    if b'"session_meta"' not in raw:
        return None, None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, None
    payload = value.get("payload") if isinstance(value, dict) else None
    if value.get("type") != "session_meta" or not isinstance(payload, dict):
        return None, None
    identity = payload.get("id")
    stamp = _timestamp(payload.get("timestamp") or value.get("timestamp"))
    return (identity if isinstance(identity, str) and identity else None), stamp


def _claude_tokens(usage):
    if not isinstance(usage, dict):
        return 0
    total = 0
    for field in TOKEN_FIELDS:
        count = _bounded_integer(usage.get(field, 0))
        if count is not None:
            total += count
    return min(total, MAX_TOKEN_VALUE)


def _claude_event(raw, timezone):
    if b'"usage"' not in raw or b'"assistant"' not in raw:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("type") != "assistant":
        return None
    message = value.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    tokens = _claude_tokens(usage)
    stamp = _timestamp(value.get("timestamp"))
    session = value.get("sessionId") or value.get("session_id")
    message_id = message.get("id") if isinstance(message, dict) else None
    call = message_id or value.get("requestId") or value.get("request_id") \
        or value.get("uuid")
    if not tokens or stamp is None or not isinstance(session, str) \
            or not session or not isinstance(call, str) or not call:
        return None
    # Claude repeats one API call across assistant transcript rows. The stable
    # session/message pair is one call; its maximum observed usage is exact.
    return {
        "event_key": _hash(session, call),
        "local_date": _local_date(stamp, timezone),
        "tokens": tokens,
        "session_key": _hash(session),
    }


def _owned_claude_home(home):
    try:
        root = os.path.realpath(paths.homes_dir())
        resolved = os.path.realpath(home)
        return os.path.commonpath((root, resolved)) == root \
            and resolved != root and not os.path.islink(home)
    except (OSError, ValueError):
        return False


def _source_specs(config, global_claude_home=None):
    specs = {}
    accounts = (config or {}).get("accounts", [])
    for account in accounts:
        if not isinstance(account, dict) or not isinstance(account.get("home"), str):
            continue
        provider = account.get("provider")
        name = account.get("name")
        if provider not in {"codex", "claude"} or not isinstance(name, str):
            continue
        home = registry.expand(account["home"])
        if provider == "codex":
            root, scope = os.path.join(home, "sessions"), name
        else:
            root = os.path.join(home, "projects")
            scope = name if _owned_claude_home(home) else UNATTRIBUTED
        resolved = os.path.realpath(root)
        candidate = (provider, scope, resolved)
        current = specs.get(resolved)
        # Attributable, Headroom-owned roots win over an unattributed duplicate.
        if current is None or current[1] == UNATTRIBUTED and scope != UNATTRIBUTED:
            specs[resolved] = candidate
    if global_claude_home is None:
        global_claude_home = os.path.expanduser("~/.claude")
    if global_claude_home:
        root = os.path.realpath(os.path.join(global_claude_home, "projects"))
        specs.setdefault(root, ("claude", UNATTRIBUTED, root))
    return sorted(specs.values(), key=lambda row: (row[0], row[1], row[2]))


def _jsonl_files(root):
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    found = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories
            if not os.path.islink(os.path.join(current, name)))
        for filename in sorted(filenames):
            candidate = os.path.join(current, filename)
            if filename.endswith(".jsonl") and not os.path.islink(candidate):
                found.append(candidate)
    return found


def _source_row(connection, filename):
    return connection.execute(
        "select device, inode, offset, has_events, session_key, session_date "
        "from sources where path = ?", (filename,)).fetchone()


def _upsert_event(connection, provider, scope, event):
    if provider == "claude":
        connection.execute(
            "insert into events(provider,event_key,scope,local_date,tokens,session_key) "
            "values(?,?,?,?,?,?) on conflict(provider,event_key) do update set "
            "tokens=max(events.tokens,excluded.tokens), "
            "local_date=min(events.local_date,excluded.local_date)",
            (provider, event["event_key"], scope, event["local_date"],
             event["tokens"], event["session_key"]),
        )
    else:
        connection.execute(
            "insert or ignore into events"
            "(provider,event_key,scope,local_date,tokens,session_key) "
            "values(?,?,?,?,?,?)",
            (provider, event["event_key"], scope, event["local_date"],
             event["tokens"], event["session_key"]),
        )


def _scan_file(connection, provider, scope, filename, timezone, now):
    try:
        stat = os.stat(filename)
    except OSError:
        return False
    previous = _source_row(connection, filename)
    same = previous is not None and previous[0] == stat.st_dev \
        and previous[1] == stat.st_ino and 0 <= previous[2] <= stat.st_size
    offset = previous[2] if same else 0
    has_events = bool(previous[3]) if same else False
    session_key = previous[4] if same else None
    session_date = previous[5] if same else None
    try:
        with open(filename, "rb") as handle:
            handle.seek(offset)
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(start)
                    break
                if provider == "codex":
                    identity, stamp = _codex_session(raw)
                    if identity:
                        session_key = _hash(identity)
                    if stamp is not None:
                        session_date = _local_date(stamp, timezone)
                    effective_session = session_key or _hash(filename)
                    event = _codex_event(raw, timezone, effective_session)
                else:
                    event = _claude_event(raw, timezone)
                if event is not None:
                    _upsert_event(connection, provider, scope, event)
                    has_events = True
            offset = handle.tell()
    except OSError:
        return False
    connection.execute(
        "insert into sources(path,provider,scope,device,inode,offset,size,"
        "has_events,session_key,session_date,updated_at) values(?,?,?,?,?,?,?,?,?,?,?) "
        "on conflict(path) do update set provider=excluded.provider,scope=excluded.scope,"
        "device=excluded.device,inode=excluded.inode,offset=excluded.offset,"
        "size=excluded.size,has_events=excluded.has_events,"
        "session_key=excluded.session_key,session_date=excluded.session_date,"
        "updated_at=excluded.updated_at",
        (filename, provider, scope, stat.st_dev, stat.st_ino, offset,
         stat.st_size, int(has_events), session_key, session_date, int(now)),
    )
    return True


def _configuration_signature(config):
    material = []
    for account in (config or {}).get("accounts", []):
        if isinstance(account, dict):
            material.append((account.get("name"), account.get("provider"),
                             account.get("home")))
    return _hash(json.dumps(material, sort_keys=True))


def _index_sync(config, *, now=None, timezone_name=None,
                global_claude_home=None, filename=None):
    """Synchronously refresh the private index; exposed for reconciliation tests."""
    now = time.time() if now is None else float(now)
    timezone_name = timezone_name or _timezone_name()
    timezone = ZoneInfo(timezone_name)
    configuration = _configuration_signature(config)
    connection = _database(filename)
    failures = 0
    try:
        prior_timezone = _meta(connection, "timezone")
        prior_configuration = _meta(connection, "configuration")
        rebuild = prior_timezone not in (None, timezone_name) \
            or prior_configuration not in (None, configuration)
        if rebuild:
            # Event rows intentionally retain no raw source path. Rebuild the
            # private index when slot names/homes/providers change so removed
            # scopes cannot remain in totals and renamed homes are attributed
            # to their current slot. Keep the rebuild in one transaction so a
            # desktop reader sees either the complete old or complete new
            # attribution, never an in-progress mixture.
            connection.execute("delete from events")
            connection.execute("delete from sources")
        _set_meta(connection, "schema", STATE_SCHEMA)
        _set_meta(connection, "timezone", timezone_name)
        count = 0
        for provider, scope, root in _source_specs(
                config, global_claude_home=global_claude_home):
            for candidate in _jsonl_files(root):
                if not _scan_file(
                        connection, provider, scope, candidate, timezone, now):
                    failures += 1
                count += 1
                if count % 100 == 0 and not rebuild:
                    connection.commit()
        _set_meta(connection, "indexed_at", int(now))
        _set_meta(connection, "configuration", configuration)
        _set_meta(connection, "failures", failures)
        connection.commit()
    finally:
        connection.close()
    return failures == 0


def _metric(value, coverage):
    return {"value": value, "coverage": coverage}


def _unavailable_periods(coverage="unavailable"):
    return {window: _metric(None, coverage) for window in WINDOWS}


def _account_rows(config):
    return [account for account in (config or {}).get("accounts", [])
            if isinstance(account, dict)
            and isinstance(account.get("name"), str)
            and account.get("provider") in {"codex", "claude"}]


def unavailable(config, status="unavailable", timezone_name=None):
    accounts = [{
        "name": row["name"], "provider": row["provider"],
        "attribution": "unavailable", "tokens": _unavailable_periods(),
        "sessions": _unavailable_periods(),
    } for row in _account_rows(config)]
    return {
        "schema": SCHEMA,
        "timezone": timezone_name or _timezone_name(),
        "status": status,
        "indexed_at": None,
        "accounts": accounts,
        "unattributed": {
            "claude_code": {
                "tokens": _unavailable_periods(),
                "sessions": _unavailable_periods(),
                "calls": _unavailable_periods(),
            },
        },
        "totals": {
            "tokens": _unavailable_periods(),
            "sessions": _unavailable_periods(),
            "calls": _unavailable_periods(),
        },
        "daily": [],
        "warnings": [],
    }


def _window_dates(now, timezone):
    today = dt.datetime.fromtimestamp(now, timezone).date()
    return {
        "today": today.isoformat(),
        "7d": (today - dt.timedelta(days=6)).isoformat(),
        "30d": (today - dt.timedelta(days=29)).isoformat(),
    }


def _periods(connection, *, provider=None, scope=None, field="tokens",
             now, timezone, coverage="exact"):
    column = "sum(tokens)" if field == "tokens" else (
        "count(distinct session_key)" if field == "sessions" else "count(*)")
    clauses = []
    values = []
    if provider is not None:
        clauses.append("provider = ?")
        values.append(provider)
    if scope is not None:
        clauses.append("scope = ?")
        values.append(scope)
    prefix = " and ".join(clauses)
    prefix = f"{prefix} and " if prefix else ""
    periods = {}
    for window, cutoff in _window_dates(now, timezone).items():
        row = connection.execute(
            f"select {column} from events where {prefix}local_date >= ?",  # noqa: S608
            (*values, cutoff),
        ).fetchone()
        periods[window] = _metric(int(row[0] or 0), coverage)
    return periods


def _codex_coverage(connection, scope, now, timezone, failures):
    if failures:
        return "partial"
    cutoffs = _window_dates(now, timezone)
    # The broadest window governs the compact account label. Missing Codex
    # token events are never silently converted into zero usage.
    gap = connection.execute(
        "select 1 from sources where provider='codex' and scope=? "
        "and has_events=0 and (session_date is null or session_date>=?) limit 1",
        (scope, cutoffs["30d"]),
    ).fetchone()
    return "partial" if gap else "exact"


def _project(config, *, now=None, filename=None, worker_running=False):
    now = time.time() if now is None else float(now)
    try:
        connection = _read_database(filename)
    except (OSError, sqlite3.Error):
        return unavailable(config)
    try:
        indexed = _meta(connection, "indexed_at")
        timezone_name = _meta(connection, "timezone", _timezone_name())
        if indexed is None:
            return unavailable(config, status="indexing", timezone_name=timezone_name)
        timezone = ZoneInfo(timezone_name)
        indexed_at = int(indexed)
        failures = int(_meta(connection, "failures", "0"))
        accounts = []
        for account in _account_rows(config):
            name, provider = account["name"], account["provider"]
            home = registry.expand(account.get("home", ""))
            attributable = provider == "codex" or _owned_claude_home(home)
            has_source = connection.execute(
                "select 1 from sources where provider=? and scope=? limit 1",
                (provider, name),
            ).fetchone() is not None
            if not attributable or not has_source:
                accounts.append({
                    "name": name, "provider": provider,
                    "attribution": "unavailable",
                    "tokens": _unavailable_periods(),
                    "sessions": _unavailable_periods(),
                })
                continue
            coverage = (_codex_coverage(connection, name, now, timezone, failures)
                        if provider == "codex"
                        else ("partial" if failures else "exact"))
            accounts.append({
                "name": name, "provider": provider, "attribution": "exact",
                "tokens": _periods(connection, provider=provider, scope=name,
                                   now=now, timezone=timezone, coverage=coverage),
                "sessions": _periods(connection, provider=provider, scope=name,
                                     field="sessions", now=now,
                                     timezone=timezone, coverage=coverage),
            })
        source_coverage = "partial" if failures else "exact"
        legacy_gap = connection.execute(
            "select 1 from sources where provider='codex' and has_events=0 "
            "limit 1").fetchone() is not None
        total_coverage = "partial" if failures or legacy_gap else "exact"
        unattributed = {
            "tokens": _periods(
                connection, provider="claude", scope=UNATTRIBUTED,
                now=now, timezone=timezone, coverage=source_coverage),
            "sessions": _periods(
                connection, provider="claude", scope=UNATTRIBUTED,
                field="sessions", now=now, timezone=timezone,
                coverage=source_coverage),
            "calls": _periods(
                connection, provider="claude", scope=UNATTRIBUTED,
                field="calls", now=now, timezone=timezone,
                coverage=source_coverage),
        }
        totals = {
            field: _periods(connection, field=field, now=now,
                            timezone=timezone, coverage=total_coverage)
            for field in ("tokens", "sessions", "calls")
        }
        minimum = (dt.datetime.fromtimestamp(now, timezone).date()
                   - dt.timedelta(days=MAX_DAILY_ROWS - 1)).isoformat()
        rows = connection.execute(
            "select local_date,"
            "sum(case when provider='codex' then tokens else 0 end),"
            "sum(case when provider='claude' then tokens else 0 end),"
            "sum(case when provider='claude' then 1 else 0 end),"
            "sum(tokens) from events where local_date>=? group by local_date "
            "order by local_date", (minimum,),
        ).fetchall()
        observed = [{
            "date": row[0], "codex_tokens": int(row[1]),
            "claude_code_tokens": int(row[2]),
            "claude_code_calls": int(row[3]), "total": int(row[4]),
            "driver": "unlabeled", "evidence": "",
        } for row in rows]
        daily = []
        if observed:
            by_date = {row["date"]: row for row in observed}
            current = dt.date.fromisoformat(observed[0]["date"])
            last = dt.datetime.fromtimestamp(now, timezone).date()
            while current <= last and len(daily) < MAX_DAILY_ROWS:
                key = current.isoformat()
                daily.append(by_date.get(key, {
                    "date": key, "codex_tokens": 0,
                    "claude_code_tokens": 0, "claude_code_calls": 0,
                    "total": 0, "driver": "unlabeled", "evidence": "",
                }))
                current += dt.timedelta(days=1)
        warnings = []
        if unattributed["tokens"]["30d"]["value"]:
            warnings.append("claude_history_unattributed")
        if legacy_gap:
            warnings.append("codex_legacy_usage_unavailable")
        if failures:
            warnings.append("source_read_incomplete")
        return {
            "schema": SCHEMA, "timezone": timezone_name,
            "status": "refreshing" if worker_running else "ready",
            "indexed_at": indexed_at, "accounts": accounts,
            "unattributed": {"claude_code": unattributed},
            "totals": totals, "daily": daily, "warnings": warnings,
        }
    except (OSError, sqlite3.Error, ValueError, ZoneInfoNotFoundError):
        return unavailable(config)
    finally:
        connection.close()


def _run_worker(config, signature):
    global _WORKER, _WORKER_SIGNATURE
    try:
        _index_sync(config)
    except Exception:  # noqa: BLE001 - raw source errors stay private
        pass
    finally:
        with _WORKER_LOCK:
            if _WORKER_SIGNATURE == signature:
                _WORKER = None


def _start_worker(config, now):
    global _WORKER, _WORKER_SIGNATURE
    signature = _configuration_signature(config)
    try:
        connection = _database()
        indexed = _meta(connection, "indexed_at")
        configured = _meta(connection, "configuration")
        connection.close()
        due = indexed is None or int(indexed) <= int(now) - REFRESH_SECONDS \
            or configured != signature
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return True
        if not due:
            return False
        frozen = json.loads(json.dumps(config or {}))
        _WORKER_SIGNATURE = signature
        _WORKER = threading.Thread(
            target=_run_worker, args=(frozen, signature),
            name="headroom-activity-index", daemon=True)
        _WORKER.start()
        return True


def snapshot(config, now=None):
    """Start an incremental refresh and return the last reconciled projection."""
    now = time.time() if now is None else float(now)
    running = _start_worker(config, now)
    return _project(config, now=now, worker_running=running)
