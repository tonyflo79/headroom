"""Bounded launch-event notifications for wrapper scripts.

When ``HEADROOM_NOTIFY_CMD`` names a command, headroom invokes it at launch
transitions with a single JSON argument describing the event:

    {"event": "launch", "mode": "supervised"|"exec",
     "account": ..., "model": ..., "note": ...}
    {"event": "downgrade", "account": ..., "reason": ...}
    {"event": "supervision_lost", "account": ..., "reason": ...}
    {"event": "fallback", "reason": ...}

Delivery is best-effort and bounded: the command runs in its own process
group with a hard timeout (default 10s, override with
``HEADROOM_NOTIFY_TIMEOUT``); on timeout the WHOLE group is SIGKILLed so a
``worker & wait`` observer can't leave descendants alive. A broken, missing,
or hung notify command is swallowed with a stderr line — it must never block,
materially delay, or kill the launch. This replaces external marker-polling
with events; it composes with, and is independent of, the
``HEADROOM_LAUNCH_MARKER`` handshake.

SECURITY: ``HEADROOM_NOTIFY_CMD`` is TRUSTED code — it runs as the invoking
user with that user's privileges and environment. The timeout bounds latency
and reaps runaways; it is NOT a sandbox. Only set this to a command you
control, exactly as you would any other command in your launch script.
"""
import fcntl
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time

from . import paths, registry

NOTIFY_TIMEOUT = 10.0
HEALTH_SCHEMA = "headroom_supervision_events@1"
HEALTH_EVENT_SCHEMA = "headroom_supervision_event@1"
HEALTH_EVENT_LIMIT = 64
HEALTH_STATES = {
    "starting", "downgraded", "armed",
    "supervision_lost", "loop_guard", "ended",
}
SUPERVISOR_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}")
HEALTH_ACTIONS = {
    "none", "wait_for_session", "use_compatible_interactive_launch",
    "inspect_handoff_health", "start_new_session",
}


def _timeout():
    raw = os.environ.get("HEADROOM_NOTIFY_TIMEOUT", "").strip()
    if not raw:
        return NOTIFY_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return NOTIFY_TIMEOUT
    # a non-positive or absurd override falls back to the default: the bound
    # must stay a real bound, never "wait forever"
    return value if 0 < value <= 60 else NOTIFY_TIMEOUT


def _health_path():
    return os.path.join(paths.state_dir(), "supervision-events.json")


def _health_lock_path():
    return os.path.join(paths.state_dir(), "supervision-events.lock")


def _stable_code(value, fallback):
    return value if isinstance(value, str) \
        and re.fullmatch(r"[a-z0-9_]{1,64}", value) else fallback


def health_projection(event, *, now=None, pid=None):
    """Project one existing notification event into bounded desktop health."""
    if not isinstance(event, dict):
        return None
    kind = event.get("event")
    reason = str(event.get("reason") or event.get("note") or "").lower()
    if kind == "launch" and event.get("mode") == "supervised":
        state, code, explanation, action = (
            "starting", "awaiting_session_start",
            "A supervised Claude process is waiting for its SessionStart proof.",
            "wait_for_session")
    elif kind == "supervision_armed":
        state, code, explanation, action = (
            "armed", "supervision_armed",
            "The engine bound this live Claude session to its authenticated supervisor.",
            "none")
    elif kind == "downgrade":
        code = "handoff_disabled_for_launch" if "not enabled" in reason else (
            "noninteractive_launch" if "not all tty" in reason else
            "incompatible_launch")
        state, explanation, action = (
            "downgraded",
            "This launch could not safely enable automatic handoff.",
            "use_compatible_interactive_launch")
    elif kind == "supervision_lost":
        code = _stable_code(event.get("code"), "supervision_lost")
        if code == "loop_guard":
            state, explanation, action = (
                "loop_guard",
                "The engine stopped automatic handoff after three recent handoffs.",
                "start_new_session")
        else:
            state, explanation, action = (
                "supervision_lost",
                "The live Claude process continues, but automatic handoff is disarmed.",
                "inspect_handoff_health")
    elif kind == "supervision_ended":
        state, code, explanation, action = (
            "ended", "supervision_ended",
            "The most recent supervised Claude process ended.", "none")
    else:
        return None
    account = event.get("account")
    if not isinstance(account, str) or not registry.NAME_RE.fullmatch(account):
        account = None
    model = event.get("model")
    if not isinstance(model, str) or not re.fullmatch(
            r"[a-z0-9_-]{1,32}", model):
        model = None
    supervisor_id = event.get("supervisor_id")
    if not isinstance(supervisor_id, str) \
            or SUPERVISOR_ID_RE.fullmatch(supervisor_id) is None:
        supervisor_id = None
    if now is None:
        observed = time.time()
    elif isinstance(now, (int, float)) and not isinstance(now, bool) \
            and math.isfinite(now):
        observed = float(now)
    else:
        return None
    process = os.getpid() if pid is None else pid
    if not math.isfinite(observed) or not isinstance(process, int) \
            or isinstance(process, bool) or process <= 0:
        return None
    return {
        "schema": HEALTH_EVENT_SCHEMA,
        "state": state,
        "code": code,
        "explanation": explanation,
        "action": action,
        "account": account,
        "model": model,
        "supervisor_id": supervisor_id,
        "pid": process,
        "observed_at": observed,
    }


def _valid_health_event(value):
    if not isinstance(value, dict) or set(value) != {
            "schema", "state", "code", "explanation", "action", "account",
            "model", "supervisor_id", "pid", "observed_at"}:
        return False
    return (
        value.get("schema") == HEALTH_EVENT_SCHEMA
        and value.get("state") in HEALTH_STATES
        and isinstance(value.get("code"), str)
        and re.fullmatch(r"[a-z0-9_]{1,64}", value["code"]) is not None
        and isinstance(value.get("explanation"), str)
        and 1 <= len(value["explanation"]) <= 256
        and value.get("action") in HEALTH_ACTIONS
        and (value.get("account") is None
             or registry.NAME_RE.fullmatch(value["account"]) is not None)
        and (value.get("model") is None
             or re.fullmatch(r"[a-z0-9_-]{1,32}", value["model"]) is not None)
        and (value.get("supervisor_id") is None
             or SUPERVISOR_ID_RE.fullmatch(value["supervisor_id"]) is not None)
        and isinstance(value.get("pid"), int)
        and not isinstance(value.get("pid"), bool) and value["pid"] > 0
        and isinstance(value.get("observed_at"), (int, float))
        and not isinstance(value.get("observed_at"), bool)
        and math.isfinite(value["observed_at"])
        and value["observed_at"] >= 0
    )


def read_health_events():
    """Read the bounded sanitized notification history or fail diagnostically."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_health_path(), flags)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise RuntimeError("supervision health history is unreadable") from error
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise RuntimeError("supervision health history is unreadable")
            value = json.load(handle)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("supervision health history is unreadable") from error
    events = value.get("events") if isinstance(value, dict) \
        and value.get("schema") == HEALTH_SCHEMA else None
    if not isinstance(events, list) or len(events) > HEALTH_EVENT_LIMIT \
            or not all(_valid_health_event(event) for event in events):
        raise RuntimeError("supervision health history is unreadable")
    return events


def _record_health_event(event):
    projected = health_projection(event)
    if projected is None:
        return False
    paths.ensure_private(paths.state_dir())
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(_health_lock_path(), flags, 0o600)
    metadata = os.fstat(lock_fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(lock_fd)
        raise OSError("supervision health lock is not a regular file")
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events = read_health_events()
        events.append(projected)
        paths.write_json_atomic(_health_path(), {
            "schema": HEALTH_SCHEMA,
            "events": events[-HEALTH_EVENT_LIMIT:],
        })
    return True


def emit(event):
    """Deliver one event to HEADROOM_NOTIFY_CMD; never raises, never unbounded.

    Returns True when the command ran to completion (its exit status is
    deliberately ignored — a failing observer must not fail the launch),
    False when no command is configured or delivery failed/timed out."""
    try:
        _record_health_event(event)
    except Exception:  # noqa: BLE001 - health observation cannot break launch
        print("[headroom] supervision health could not be recorded "
              "(launch continues)", file=sys.stderr)
    raw = os.environ.get("HEADROOM_NOTIFY_CMD", "").strip()
    if not raw:
        return False
    try:
        argv = shlex.split(raw)
        if not argv:
            return False
        payload = json.dumps(event, sort_keys=True, allow_nan=False)
        # start_new_session makes the command its own session/group leader
        # (pgid == pid), so a hung command is killed without touching the
        # launch's terminal/process group; all stdio detached so a chatty
        # observer can never corrupt the CLI's screen
        process = subprocess.Popen(
            argv + [payload],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            process.wait(timeout=_timeout())
        except subprocess.TimeoutExpired:
            # SIGKILL the WHOLE group, not just the direct child: a shell that
            # backgrounded workers (`worker & wait`) would otherwise leave them
            # alive to accumulate. Then reap the leader so it isn't a zombie.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()  # group gone/unavailable — fall back to the pid
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            print("[headroom] notify command timed out; its process group was "
                  "killed (launch continues)", file=sys.stderr)
            return False
        return True
    except Exception as error:  # noqa: BLE001 — an observer can never be fatal
        print(f"[headroom] notify failed: {error} (launch continues)",
              file=sys.stderr)
        return False
