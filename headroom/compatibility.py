"""Fail-closed engine, bridge, CLI, and state compatibility contract.

The registry has one released schema today (v1).  This module deliberately
does not invent an older migration.  It does provide the locked, private,
atomic migration runner that future released transitions must register, and
it exposes a read-only status for unsupported older/newer state so neither a
desktop bundle nor an installed CLI attempts a downgrade write.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import stat

from . import __version__, paths, registry


SCHEMA = "headroom_compatibility@1"
BRIDGE_IDENTIFIER = "headroom_desktop_bridge@1"
BRIDGE_SCHEMA = 1
STATE_SCHEMA = 1
RELEASED_STATE_SCHEMAS = (1,)
MAX_CONFIG_BYTES = 1024 * 1024
BRIDGE_CAPABILITIES = (
    "fixture_snapshot", "discover", "adopt", "refresh",
    "claude_login", "codex_device_login", "onboarding",
    "account_lifecycle", "reauthentication", "resilient_collection",
    "validated_settings", "routing_launch",
    "provider_reauthentication_launch", "handoff_health",
    "redacted_diagnostics", "schema_compatibility", "shutdown",
)

# A future state release adds one transformer keyed by its source schema.
# Keeping this empty is intentional: v1 is the first and only released state.
MIGRATIONS = {}


class CompatibilityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _engine_range(version=__version__):
    """Use the pre-1.0 minor as the explicit compatible engine line."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version)
    if match is None:
        return {"minimum": version, "maximum_exclusive": version}
    major, minor = int(match.group(1)), int(match.group(2))
    if major == 0:
        return {
            "minimum": f"0.{minor}.0",
            "maximum_exclusive": f"0.{minor + 1}.0",
        }
    return {
        "minimum": f"{major}.0.0",
        "maximum_exclusive": f"{major + 1}.0.0",
    }


def _read_config_bytes():
    path = paths.config_path()
    if not os.path.lexists(path):
        return None
    if os.path.islink(path) or not os.path.isfile(path):
        raise CompatibilityError(
            "state_unreadable", "registry is not a safe regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CompatibilityError(
            "state_unreadable", "registry cannot be opened safely") from error
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise CompatibilityError(
                "state_oversized", "registry exceeds the compatibility limit")
        raw = _read_bounded_fd(descriptor)
        if len(raw) > MAX_CONFIG_BYTES:
            raise CompatibilityError(
                "state_oversized", "registry exceeds the compatibility limit")
        return raw
    except OSError as error:
        raise CompatibilityError(
            "state_unreadable", "registry cannot be read safely") from error
    finally:
        os.close(descriptor)


def _read_bounded_fd(descriptor):
    chunks = []
    remaining = MAX_CONFIG_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CompatibilityError(
            "state_invalid", "registry is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise CompatibilityError("state_invalid", "registry must be an object")
    version = value.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise CompatibilityError(
            "state_schema_missing", "registry has no valid schema version")
    return value, version


def inspect_state():
    """Return a sanitized, path-free, read-only state compatibility result."""
    try:
        raw = _read_config_bytes()
        if raw is None:
            return {
                "observed_schema": None,
                "status": "missing",
                "code": "state_not_configured",
                "remediation": "run_setup",
                "migration": {
                    "required": False, "supported": True,
                    "from_schema": None, "to_schema": STATE_SCHEMA,
                },
            }
        value, version = _decode(raw)
        if version < min(RELEASED_STATE_SCHEMAS):
            return _incompatible_state(
                version, "incompatible_older", "state_schema_too_old",
                "upgrade_through_supported_release", required=True)
        if version > STATE_SCHEMA:
            return _incompatible_state(
                version, "incompatible_newer", "state_schema_too_new",
                "upgrade_headroom", required=False)
        if version != STATE_SCHEMA:
            supported = _migration_path(version) is not None
            return {
                "observed_schema": version,
                "status": "migration_required" if supported else "incompatible_older",
                "code": "state_migration_available" if supported
                        else "state_schema_too_old",
                "remediation": "migrate_state" if supported
                               else "upgrade_through_supported_release",
                "migration": {
                    "required": True, "supported": supported,
                    "from_schema": version, "to_schema": STATE_SCHEMA,
                },
            }
        try:
            registry.validate(value)
        except registry.RegistryError:
            return _incompatible_state(
                version, "invalid", "state_validation_failed",
                "inspect_diagnostics", required=False)
        return {
            "observed_schema": version,
            "status": "compatible",
            "code": "state_schema_current",
            "remediation": "none",
            "migration": {
                "required": False, "supported": True,
                "from_schema": version, "to_schema": STATE_SCHEMA,
            },
        }
    except CompatibilityError as error:
        return {
            "observed_schema": None,
            "status": "unreadable",
            "code": error.code,
            "remediation": "inspect_diagnostics",
            "migration": {
                "required": False, "supported": False,
                "from_schema": None, "to_schema": STATE_SCHEMA,
            },
        }


def _incompatible_state(version, status, code, remediation, *, required):
    return {
        "observed_schema": version,
        "status": status,
        "code": code,
        "remediation": remediation,
        "migration": {
            "required": required, "supported": False,
            "from_schema": version, "to_schema": STATE_SCHEMA,
        },
    }


def _migration_path(source, *, target=STATE_SCHEMA, migrations=None):
    migrations = MIGRATIONS if migrations is None else migrations
    if source == target:
        return []
    if source > target:
        return None
    path = []
    version = source
    while version < target:
        transform = migrations.get(version)
        if not callable(transform):
            return None
        path.append((version, transform))
        version += 1
    return path


def contract(capabilities=None):
    """Return the exact sanitized compatibility matrix for CLI and desktop."""
    safe_capabilities = []
    for value in BRIDGE_CAPABILITIES if capabilities is None else capabilities:
        if isinstance(value, str) and value not in safe_capabilities:
            safe_capabilities.append(value)
    state = inspect_state()
    state.update({
        "current_schema": STATE_SCHEMA,
        "compatible_schemas": {
            "minimum": min(RELEASED_STATE_SCHEMAS),
            "maximum": STATE_SCHEMA,
        },
    })
    return {
        "schema": SCHEMA,
        "product": {"name": "headroom", "version": __version__},
        "engine": {
            "version": __version__,
            "compatible_versions": _engine_range(),
        },
        "bridge": {
            "identifier": BRIDGE_IDENTIFIER,
            "current_schema": BRIDGE_SCHEMA,
            "compatible_schemas": {
                "minimum": BRIDGE_SCHEMA, "maximum": BRIDGE_SCHEMA,
            },
        },
        "state": state,
        "platform": platform.system().lower(),
        "architecture": platform.machine(),
        "capabilities": safe_capabilities,
    }


def _backup_bytes(raw, source, target):
    directory = paths.ensure_private(os.path.join(paths.state_dir(), "migrations"))
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"config-v{source}-to-v{target}-{digest[:16]}.json"
    destination = os.path.join(directory, filename)
    if os.path.lexists(destination):
        if os.path.islink(destination) or not os.path.isfile(destination):
            raise CompatibilityError(
                "migration_backup_unsafe", "migration backup destination is unsafe")
        try:
            descriptor = os.open(
                destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise CompatibilityError(
                "migration_backup_unsafe", "migration backup cannot be opened") \
                from error
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CompatibilityError(
                    "migration_backup_unsafe", "migration backup is not regular")
            existing = _read_bounded_fd(descriptor)
        finally:
            os.close(descriptor)
        if existing != raw:
            raise CompatibilityError(
                "migration_backup_conflict", "migration backup conflicts")
        os.chmod(destination, 0o600)
        return filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short migration backup write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    except Exception:
        try:
            os.unlink(destination)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return filename


def migrate_registry(*, target=STATE_SCHEMA, migrations=None, validator=None):
    """Run a registered migration under the registry lock.

    The production call currently performs only an idempotent v1 validation.
    ``target``, ``migrations``, and ``validator`` make the runner testable with
    a synthetic prior schema without claiming that schema was ever released.
    """
    migrations = MIGRATIONS if migrations is None else migrations
    validator = registry.validate if validator is None else validator
    with registry.config_lock():
        raw = _read_config_bytes()
        if raw is None:
            raise CompatibilityError(
                "state_not_configured", "no registry exists to migrate")
        value, source = _decode(raw)
        if source > target:
            raise CompatibilityError(
                "downgrade_refused", "newer state is read-only to this engine")
        path = _migration_path(source, target=target, migrations=migrations)
        if path is None:
            raise CompatibilityError(
                "migration_unavailable", "no complete migration path exists")
        if not path:
            validator(value)
            return {
                "schema": SCHEMA, "changed": False,
                "from_schema": source, "to_schema": target,
                "backup_created": False,
            }
        backup = _backup_bytes(raw, source, target)
        migrated = copy.deepcopy(value)
        for expected, transform in path:
            if migrated.get("schema_version") != expected:
                raise CompatibilityError(
                    "migration_invalid", "migration input schema changed")
            migrated = transform(copy.deepcopy(migrated))
            if not isinstance(migrated, dict) \
                    or migrated.get("schema_version") != expected + 1:
                raise CompatibilityError(
                    "migration_invalid", "migration produced an invalid schema")
        validator(migrated)
        paths.write_json_atomic(paths.config_path(), migrated, mode=0o600)
        return {
            "schema": SCHEMA, "changed": True,
            "from_schema": source, "to_schema": target,
            "backup_created": bool(backup),
        }
