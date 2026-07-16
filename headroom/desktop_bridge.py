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
import sys
import time

from . import __version__, widget


SCHEMA = "headroom_desktop_bridge@1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_REQUEST_ID = 128


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
            "capabilities": ["fixture_snapshot", "shutdown"],
            "runtime": "frozen" if getattr(sys, "frozen", False) else "python",
            "pid": os.getpid(),
        }, False
    if command == "fixture_snapshot":
        return fixture_snapshot(args.get("now")), False
    if command == "shutdown":
        if args:
            raise BridgeError("invalid_args", "shutdown accepts no arguments")
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
