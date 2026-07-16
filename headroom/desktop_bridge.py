"""Private stdio bridge used by the self-contained desktop application.

The bridge reserves its original stdout for newline-delimited JSON protocol
frames. Imported engine code and provider children may still print, so normal
``sys.stdout`` is redirected to stderr before any request is handled.
"""

from __future__ import annotations

import json
import math
import os
import platform
import secrets
import sys
import threading
import time

from . import __version__, collect as collector, connect, paths, registry, widget


SCHEMA = "headroom_desktop_bridge@1"
VIEW_SCHEMA = "headroom_desktop_view@1"
LOGIN_SCHEMA = "headroom_desktop_login@1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_REQUEST_ID = 128


def _settings(config=None):
    dashboard = (registry.dashboard_settings(config) if config is not None
                 else dict(registry.DEFAULT_DASHBOARD))
    return {
        "title": dashboard.get("title", "AI Fleet"),
        "theme": dashboard.get("theme", "midnight"),
        "redact_emails": dashboard.get("redact_emails", True) is not False,
        "reserve_percent": (registry.reserve_percent(config)
                            if config is not None else 0.0),
        "auto_handoff": (registry.auto_handoff(config)
                         if config is not None else True),
    }


def _registry_discovery():
    """Read registry state without creating, repairing, or collecting."""
    config_path = paths.config_path()
    if not os.path.exists(config_path):
        return "missing", None, None
    raw = paths.load_json(config_path)
    if raw is None:
        return "recovery", None, "registry_unreadable"
    try:
        return "compatible", registry.validate(raw), None
    except registry.RegistryError:
        return "recovery", None, "registry_incompatible"


def _candidate_projection(rows, config=None):
    registered_homes = {
        registry.expand(row.get("home"))
        for row in (config or {}).get("accounts", [])
        if isinstance(row, dict) and isinstance(row.get("home"), str)
    }
    result = []
    for row in rows:
        provider = row.get("provider") if isinstance(row, dict) else None
        email = row.get("email") if isinstance(row, dict) else None
        if provider not in registry.PROVIDERS or not isinstance(email, str):
            continue
        if isinstance(row.get("home"), str) \
                and registry.expand(row["home"]) in registered_homes:
            continue
        result.append({
            "id": "existing-" + provider,
            "provider": provider,
            "identity": collector.redact_email(email),
        })
    return result


def _redacted_identity(value):
    return collector.redact_email(value) if isinstance(value, str) else None


def _held_account(row):
    """Project a configured slot safely when no public observation exists."""
    return {
        "name": row["name"],
        "provider": row["provider"],
        "identity": _redacted_identity(row.get("expected_email")),
        "plan": "Unknown",
        "note": "No collected reading yet",
        "trust_state": None,
        "reserved": row.get("reserved") is True,
        "state": "held",
        "windows": {},
    }


def _view(config, public_snapshot=None, *, mode="ready", candidates=None,
          recovery_code=None, now=None):
    now = time.time() if now is None else float(now)
    projected = widget.project(public_snapshot or {}, evaluated_at=now)
    public_rows = {
        row.get("name"): row for row in (public_snapshot or {}).get("accounts", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    configured = {
        row.get("name"): row for row in (config or {}).get("accounts", [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    accounts = []
    for row in projected["accounts"]:
        if config is not None and row["name"] not in configured:
            continue
        details = public_rows.get(row["name"], {})
        entry = dict(row)
        entry.update({
            # The desktop boundary always redacts identity, even when the
            # legacy browser dashboard was configured to publish full email.
            "identity": _redacted_identity(details.get("email")),
            "plan": details.get("plan") or "Unknown",
            "note": details.get("note"),
            "trust_state": details.get("trust_state"),
            "reserved": configured.get(row["name"], {}).get("reserved") is True,
        })
        accounts.append(entry)
    projected_names = {row["name"] for row in accounts}
    for row in configured.values():
        if row["name"] not in projected_names:
            accounts.append(_held_account(row))
    return {
        "schema": VIEW_SCHEMA,
        "mode": mode,
        "settings": _settings(config),
        "candidates": list(candidates or []),
        "recovery_code": recovery_code,
        "freshness": projected["freshness"],
        "headline": projected["headline"],
        "accounts": accounts,
    }


def discover_desktop(now=None):
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        return _view(None, mode="recovery", recovery_code=recovery_code, now=now)
    candidates = _candidate_projection(connect.detect_existing(), config)
    public = paths.load_json(paths.public_snapshot_path()) if config else None
    mode = "ready" if config else "empty"
    return _view(config, public, mode=mode, candidates=candidates, now=now)


def refresh_desktop(now=None):
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    if config is None:
        raise BridgeError("no_accounts", "adopt an account before refreshing")
    snapshot = collector.run_collect(quiet=True)
    if not isinstance(snapshot, dict):
        raise BridgeError("collection_busy", "another collection is running")
    # Collection can update registry metadata under its own lock. Re-read the
    # latest compatible state so the view never rolls settings back in memory.
    config = registry.load()
    public = collector.public_snapshot(snapshot, redact_emails=True)
    return _view(config, public, now=now)


def adopt_desktop(candidate_id, name, now=None):
    if candidate_id not in {"existing-claude", "existing-codex"}:
        raise BridgeError("invalid_candidate", "existing account is invalid")
    if not isinstance(name, str) or not registry.NAME_RE.fullmatch(name):
        raise BridgeError("invalid_account_name", "account name is invalid")
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    config = config or {
        "schema_version": 1,
        "dashboard": dict(registry.DEFAULT_DASHBOARD),
        "accounts": [],
    }
    if any(row.get("name") == name for row in config["accounts"]):
        raise BridgeError("duplicate_account_name", "account name is already in use")
    provider = candidate_id.removeprefix("existing-")
    candidate = next((row for row in connect.detect_existing()
                      if row.get("provider") == provider), None)
    if candidate is None:
        raise BridgeError("candidate_missing", "existing account is no longer available")
    entry = connect.connect_adopt(
        config, name, provider, candidate["home"], quiet=True)
    if entry is None:
        raise BridgeError("adoption_refused", "existing account could not be adopted")
    saved = registry.load()
    adopted = next((row for row in saved["accounts"] if row["name"] == name), None)
    if adopted is None or adopted["provider"] != provider \
            or registry.expand(adopted["home"]) != registry.expand(candidate["home"]):
        raise BridgeError("adoption_conflict", "account registry changed during adoption")
    return refresh_desktop(now=now)


class DesktopLoginManager:
    """One cancellable provider-login job, projected without provider output."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job = None

    def _projection(self, job):
        return {
            "schema": LOGIN_SCHEMA,
            "job_id": job["job_id"],
            "provider": job["provider"],
            "name": job["name"],
            "state": job["state"],
            "progress_code": job["progress_code"],
            "result_code": job.get("result_code"),
            "view": job.get("view"),
        }

    def start_claude(self, name, expected_email=None):
        if not isinstance(name, str) or not registry.NAME_RE.fullmatch(name):
            raise BridgeError("invalid_account_name", "account name is invalid")
        if expected_email is not None and (
                not isinstance(expected_email, str)
                or len(expected_email) > 254 or "@" not in expected_email):
            raise BridgeError("invalid_expected_identity",
                              "expected email is invalid")
        state, config, recovery_code = _registry_discovery()
        if state == "recovery":
            raise BridgeError("recovery_required", recovery_code)
        config = config or {
            "schema_version": 1,
            "dashboard": dict(registry.DEFAULT_DASHBOARD),
            "accounts": [],
        }
        if any(row.get("name") == name for row in config["accounts"]):
            raise BridgeError("duplicate_account_name", "account name is already in use")
        with self._lock:
            if self._job and self._job["state"] in {"running", "cancelling"}:
                raise BridgeError("login_in_progress", "another login is in progress")
            job = {
                "job_id": secrets.token_hex(12), "provider": "claude",
                "name": name, "state": "running", "progress_code": "queued",
                "cancel": threading.Event(),
            }
            self._job = job
            thread = threading.Thread(
                target=self._run, args=(job, config, expected_email),
                name="headroom-claude-login", daemon=False)
            job["thread"] = thread
            thread.start()
            return self._projection(job)

    def _run(self, job, config, expected_email):
        def progress(code):
            with self._lock:
                if self._job is job and job["state"] == "running":
                    job["progress_code"] = code
        try:
            outcome = connect.desktop_connect_fresh(
                config, job["name"], "claude", expected_email=expected_email,
                cancel_event=job["cancel"], progress=progress)
            if outcome.get("ok"):
                progress("publishing")
                view = discover_desktop()
                state = "succeeded"
            else:
                view = None
                state = ("cancelled" if outcome.get("code") == "cancelled"
                         else "failed")
            with self._lock:
                if self._job is job:
                    job.update({
                        "state": state, "progress_code": "complete",
                        "result_code": outcome.get("code", "internal_error"),
                        "view": view,
                    })
        except Exception:  # noqa: BLE001 - no detail crosses desktop boundary
            with self._lock:
                if self._job is job:
                    job.update({"state": "failed", "progress_code": "complete",
                                "result_code": "internal_error", "view": None})

    def status(self, job_id):
        with self._lock:
            if not self._job or job_id != self._job["job_id"]:
                raise BridgeError("login_job_missing", "login job is unavailable")
            return self._projection(self._job)

    def cancel(self, job_id):
        with self._lock:
            if not self._job or job_id != self._job["job_id"]:
                raise BridgeError("login_job_missing", "login job is unavailable")
            if self._job["state"] == "running":
                self._job["state"] = "cancelling"
                self._job["progress_code"] = "cancelling"
                self._job["cancel"].set()
            return self._projection(self._job)

    def shutdown(self):
        with self._lock:
            job = self._job
            if job and job["state"] in {"running", "cancelling"}:
                job["cancel"].set()
            thread = job.get("thread") if job else None
        if thread and thread.is_alive():
            thread.join(timeout=5)


LOGIN_MANAGER = DesktopLoginManager()


class BridgeError(ValueError):
    """A stable protocol error safe to return across the desktop boundary."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fixture_snapshot(now=None):
    """Return a deterministic sanitized projection for the first tracer."""
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise BridgeError("invalid_clock", "fixture clock must be finite")
    captured = now - 8
    raw = {
        "schema_version": 1,
        "run_id": "desktop-fixture",
        "generated": captured,
        "accounts": [
            {
                "name": "personal", "provider": "claude", "ok": True,
                "stale": False, "routable": True,
                "trust_state": "verified", "identity_verified": True,
                "captured_at": captured, "source": "desktop_fixture",
                "windows": {
                    "5h": {"used_percent": 24.0, "resets_at": now + 7200,
                           "window_minutes": 300},
                    "7d": {"used_percent": 41.0, "resets_at": now + 259200,
                           "window_minutes": 10080},
                },
            },
            {
                "name": "codex-main", "provider": "codex", "ok": True,
                "stale": False, "routable": True,
                "trust_state": "verified", "identity_verified": True,
                "captured_at": captured, "source": "desktop_fixture",
                "windows": {
                    "5h": {"used_percent": 58.0, "resets_at": now + 5400,
                           "window_minutes": 300},
                    "7d": {"used_percent": 19.0, "resets_at": now + 432000,
                           "window_minutes": 10080},
                    "scoped:Spark": {
                        "used_percent": 32.0, "resets_at": now + 432000,
                        "window_minutes": 10080},
                },
            },
        ],
    }
    # Deliberate legacy-style stdout: main() redirects this to stderr. The
    # subprocess test proves it cannot appear among protocol frames.
    print("[headroom-desktop] prepared sanitized fixture snapshot")
    return widget.project(raw, evaluated_at=now)


def _validate_request(value):
    if not isinstance(value, dict):
        raise BridgeError("invalid_request", "request must be an object")
    if value.get("schema") != SCHEMA:
        raise BridgeError("incompatible_schema", "unsupported bridge schema")
    request_id = value.get("id")
    if not isinstance(request_id, str) or not request_id \
            or len(request_id) > MAX_REQUEST_ID:
        raise BridgeError("invalid_request_id", "request id is invalid")
    command = value.get("command")
    if not isinstance(command, str) or not command:
        raise BridgeError("invalid_command", "command is required")
    args = value.get("args", {})
    if not isinstance(args, dict):
        raise BridgeError("invalid_args", "args must be an object")
    return request_id, command, args


def _handle(command, args):
    if command == "handshake":
        requested = args.get("accepted_schemas") if args else None
        if requested is not None and SCHEMA not in requested:
            raise BridgeError(
                "incompatible_schema", "desktop does not accept this schema")
        return {
            "product": "headroom", "product_version": __version__,
            "bridge_schema": SCHEMA, "bridge_schema_range": [1, 1],
            "state_schema_range": [1, 1], "platform": sys.platform,
            "architecture": platform.machine(),
            "capabilities": [
                "fixture_snapshot", "discover", "adopt", "refresh",
                "claude_login", "shutdown"],
            "runtime": "frozen" if getattr(sys, "frozen", False) else "python",
            "pid": os.getpid(),
        }, False
    if command == "fixture_snapshot":
        if set(args) - {"now"}:
            raise BridgeError("invalid_args", "fixture arguments are invalid")
        return fixture_snapshot(args.get("now")), False
    if command == "discover":
        if set(args) - {"now"}:
            raise BridgeError("invalid_args", "discover arguments are invalid")
        return discover_desktop(args.get("now")), False
    if command == "adopt":
        if set(args) - {"candidate_id", "name", "now"}:
            raise BridgeError("invalid_args", "adopt arguments are invalid")
        return adopt_desktop(args.get("candidate_id"), args.get("name"),
                             args.get("now")), False
    if command == "refresh":
        if set(args) - {"now"}:
            raise BridgeError("invalid_args", "refresh arguments are invalid")
        return refresh_desktop(args.get("now")), False
    if command == "start_claude_login":
        if set(args) - {"name", "expected_email"}:
            raise BridgeError("invalid_args", "login arguments are invalid")
        return LOGIN_MANAGER.start_claude(
            args.get("name"), args.get("expected_email")), False
    if command == "login_status":
        if set(args) != {"job_id"}:
            raise BridgeError("invalid_args", "login status arguments are invalid")
        return LOGIN_MANAGER.status(args.get("job_id")), False
    if command == "cancel_login":
        if set(args) != {"job_id"}:
            raise BridgeError("invalid_args", "cancel arguments are invalid")
        return LOGIN_MANAGER.cancel(args.get("job_id")), False
    if command == "shutdown":
        if args:
            raise BridgeError("invalid_args", "shutdown accepts no arguments")
        LOGIN_MANAGER.shutdown()
        return {"accepted": True}, True
    raise BridgeError("unknown_command", f"unsupported command: {command}")


def _frame(request_id, *, result=None, error=None):
    value = {"schema": SCHEMA, "id": request_id, "ok": error is None}
    if error is None:
        value["result"] = result
    else:
        value["error"] = {"code": error.code, "message": str(error)}
    return json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n"


def _write_frame(protocol_out, text):
    if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise RuntimeError("desktop bridge attempted to emit an oversized frame")
    protocol_out.write(text)
    protocol_out.flush()


def main(input_stream=None, protocol_out=None):
    input_stream = sys.stdin if input_stream is None else input_stream
    protocol_out = sys.stdout if protocol_out is None else protocol_out
    if protocol_out is sys.stdout:
        sys.stdout = sys.stderr
    for line in input_stream:
        if len(line.encode("utf-8")) > MAX_FRAME_BYTES:
            print("[headroom-desktop] refused oversized request frame",
                  file=sys.stderr)
            return 2
        request_id = "unknown"
        try:
            value = json.loads(line)
            if isinstance(value, dict) and isinstance(value.get("id"), str):
                request_id = value["id"][:MAX_REQUEST_ID] or "unknown"
            request_id, command, args = _validate_request(value)
            result, should_exit = _handle(command, args)
            _write_frame(protocol_out, _frame(request_id, result=result))
            if should_exit:
                return 0
        except json.JSONDecodeError:
            error = BridgeError("invalid_json", "request is not valid JSON")
            _write_frame(protocol_out, _frame(request_id, error=error))
        except BridgeError as error:
            _write_frame(protocol_out, _frame(request_id, error=error))
        except Exception:  # noqa: BLE001 - no internal detail crosses boundary
            print("[headroom-desktop] internal bridge error", file=sys.stderr)
            error = BridgeError("internal_error", "desktop engine request failed")
            _write_frame(protocol_out, _frame(request_id, error=error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
