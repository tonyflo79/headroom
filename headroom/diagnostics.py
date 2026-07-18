"""Bounded, redacted diagnostics for the native desktop application.

This module deliberately reports state classes and stable codes rather than
raw errors.  It never serializes provider output, account identities, private
paths, environment variables, credentials, prompts, or conversation content.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time

from . import __version__, activity, connect, paths, registry


SCHEMA = "headroom_engine_health@1"
BACKUP_MAX_BYTES = 1024 * 1024
COMPONENT_STATES = {"ok", "attention", "unavailable"}


def _component(identifier, state, code, remediation="none"):
    if state not in COMPONENT_STATES:
        raise ValueError("diagnostic component state is invalid")
    return {
        "id": identifier,
        "state": state,
        "code": code,
        "remediation": remediation,
    }


def recovery_dir():
    return os.path.join(paths.state_dir(), "recovery")


def _read_regular_bounded(filename, maximum):
    """Read one owned regular file without following a final symlink."""
    try:
        metadata = os.lstat(filename)
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None, "unsafe"
    if metadata.st_size > maximum:
        return None, "oversized"
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(filename, flags)
    except OSError:
        return None, "unreadable"
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_ino != metadata.st_ino \
                or opened.st_dev != metadata.st_dev \
                or opened.st_size > maximum:
            return None, "unsafe"
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            return None, "oversized"
        return data, None
    finally:
        os.close(descriptor)


def backup_corrupt_config():
    """Create one private, content-addressed backup without changing config.

    The backup intentionally preserves the original bytes for operator-led
    recovery, so it stays in the private state tree and is never eligible for
    a support bundle.  Repeated discovery is idempotent.
    """
    source = paths.config_path()
    data, error = _read_regular_bounded(source, BACKUP_MAX_BYTES)
    if data is None:
        return {"state": error, "created": False}
    directory = recovery_dir()
    try:
        paths.ensure_private(paths.state_dir())
        paths.ensure_private(directory)
        digest = hashlib.sha256(data).hexdigest()[:20]
        destination = os.path.join(directory, f"config-corrupt-{digest}.bak")
        if os.path.lexists(destination):
            existing, existing_error = _read_regular_bounded(
                destination, BACKUP_MAX_BYTES)
            if existing_error is None and existing == data:
                os.chmod(destination, 0o600)
                return {"state": "available", "created": False}
            return {"state": "unsafe", "created": False}
        descriptor, temporary = tempfile.mkstemp(
            prefix=".config-corrupt-", suffix=".tmp", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return {"state": "available", "created": True}
    except OSError:
        return {"state": "unreadable", "created": False}


def _registry_health(config, recovery_code, backup_state):
    if config is not None:
        return _component("registry", "ok", "registry_ready")
    if recovery_code:
        remediation = ("restore_private_backup"
                       if backup_state == "available"
                       else "repair_config_manually")
        return _component("registry", "attention", recovery_code, remediation)
    return _component("registry", "attention", "registry_missing",
                      "complete_onboarding")


def _snapshot_health(config):
    filename = paths.public_snapshot_path()
    if config is None:
        return _component("snapshot", "unavailable", "snapshot_unavailable",
                          "repair_registry")
    data, error = _read_regular_bounded(filename, 4 * 1024 * 1024)
    if data is None:
        code = "snapshot_missing" if not os.path.lexists(filename) \
            else f"snapshot_{error}"
        return _component("snapshot", "attention", code, "refresh_capacity")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("accounts"), list):
        return _component("snapshot", "attention", "snapshot_malformed",
                          "refresh_capacity")
    return _component("snapshot", "ok", "snapshot_ready")


def _activity_health(config, now):
    if config is None:
        return _component("activity", "unavailable", "activity_unavailable",
                          "repair_registry")
    try:
        value = activity._project(config, now=now)  # private data never leaves
    except Exception:  # noqa: BLE001 - stable diagnostics only
        value = {"status": "unavailable"}
    status = value.get("status") if isinstance(value, dict) else None
    if status == "ready":
        return _component("activity", "ok", "activity_ready")
    if status in {"indexing", "refreshing"}:
        return _component("activity", "attention", "activity_indexing",
                          "wait_for_index")
    return _component("activity", "attention", "activity_unavailable",
                      "refresh_activity")


def engine_health(*, config=None, recovery_code=None, backup_state=None,
                  now=None):
    """Return the complete engine-owned health projection."""
    now = time.time() if now is None else float(now)
    components = [
        _component("engine", "ok", "engine_ready"),
        _component("bridge", "ok", "bridge_ready"),
        _registry_health(config, recovery_code, backup_state),
        _snapshot_health(config),
        _activity_health(config, now),
    ]
    for provider in registry.PROVIDERS:
        available = connect.provider_binary(provider) is not None
        components.append(_component(
            f"provider_{provider}", "ok" if available else "attention",
            f"{provider}_cli_ready" if available else f"{provider}_cli_missing",
            "none" if available else f"install_{provider}_cli"))
    return {
        "schema": SCHEMA,
        "generated_at": int(now),
        "engine_version": __version__,
        "components": components,
        "private_backup": backup_state == "available",
    }
