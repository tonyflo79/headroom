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
import re
import secrets
import shlex
import sys
import threading
import time

from . import (
    __version__, account_lifecycle, activity, capabilities,
    collect as collector, connect, notify, paths, registry, route, widget,
)


SCHEMA = "headroom_desktop_bridge@1"
VIEW_SCHEMA = "headroom_desktop_view@1"
LOGIN_SCHEMA = "headroom_desktop_login@1"
ONBOARDING_SCHEMA = "headroom_desktop_onboarding@1"
ONBOARDING_STEPS = {"welcome", "providers", "accounts", "demo", "complete"}
ROUTING_SCHEMA = "headroom_desktop_routing@1"
LAUNCH_INTENT_SCHEMA = "headroom_provider_launch_intent@1"
REAUTH_LAUNCH_INTENT_SCHEMA = "headroom_provider_reauthentication_intent@1"
HANDOFF_HEALTH_SCHEMA = "headroom_handoff_health@1"
DESKTOP_FAMILIES = ("claude", "opus", "sonnet", "haiku", "fable", "codex")
MAX_FRAME_BYTES = 1024 * 1024
MAX_REQUEST_ID = 128


def _settings(config=None):
    dashboard = (registry.dashboard_settings(config) if config is not None
                 else dict(registry.DEFAULT_DASHBOARD))
    desktop = (registry.desktop_settings(config) if config is not None
               else registry.desktop_settings({
                   "schema_version": 1,
                   "accounts": [{"name": "defaults", "provider": "claude",
                                 "home": "/defaults"}],
               }))
    return {
        "title": dashboard.get("title", "AI Fleet"),
        "theme": dashboard.get("theme", "midnight"),
        "redact_emails": dashboard.get("redact_emails", True) is not False,
        "reserve_percent": (registry.reserve_percent(config)
                            if config is not None else 0.0),
        "auto_handoff": (registry.auto_handoff(config)
                         if config is not None else True),
        **desktop,
    }


def _pid_alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def handoff_health_desktop(
        config=None, now=None, *, probe_provider=True, observe_events=True):
    """Sanitized projection of CLI capabilities and supervisor notifications."""
    configured = registry.auto_handoff(config) if config is not None else True
    try:
        capability_map = capabilities.contract()
        auto = capability_map.get("auto_handoff")
        supported = isinstance(auto, dict) \
            and auto.get("claude") is True and auto.get("health") is True
    except Exception:  # noqa: BLE001 - an unreadable probe is unavailable
        supported = False
    base = {
        "schema": HANDOFF_HEALTH_SCHEMA,
        "configured": configured,
        "supported": supported,
        "state": "configured",
        "code": "handoff_configured",
        "explanation": (
            "Automatic handoff is enabled for the next compatible Claude launch."),
        "action": "none",
        "active_session": False,
        "account": None,
        "model": None,
        "observed_at": None,
        "preference_effect": "next_launch_only",
    }
    if not supported:
        base.update({
            "state": "unavailable", "code": "handoff_unsupported",
            "explanation": "This engine cannot prove automatic-handoff support.",
            "action": "upgrade_engine",
        })
        return base
    if probe_provider and config is not None \
            and connect.provider_binary("claude") is None:
        base.update({
            "state": "unavailable", "code": "claude_cli_missing",
            "explanation": "Automatic handoff requires a verified Claude CLI.",
            "action": "install_claude_cli",
        })
        return base
    if not observe_events:
        if not configured:
            base.update({
                "state": "disabled", "code": "handoff_disabled",
                "explanation": "Automatic handoff is disabled for future launches.",
                "action": "enable_handoff",
            })
        return base
    try:
        events = notify.read_health_events()
    except (OSError, RuntimeError, ValueError):
        base.update({
            "state": "unavailable", "code": "handoff_health_unreadable",
            "explanation": "Supervisor health history cannot be verified safely.",
            "action": "inspect_diagnostics",
        })
        return base
    latest = events[-1] if events else None
    if latest is not None:
        event_state = latest["state"]
        active = _pid_alive(latest["pid"])
        if event_state in {
                "starting", "downgraded", "armed",
                "supervision_lost", "loop_guard"} \
                and not active:
            event_state = "ended"
        state = "configured" if event_state in {"starting", "ended"} \
            else event_state
        base.update({
            "state": state,
            "code": latest["code"] if event_state != "ended"
            else "no_active_supervisor",
            "explanation": latest["explanation"] if event_state != "ended"
            else "No supervised Claude process is currently active.",
            "action": latest["action"] if event_state != "ended" else "none",
            "active_session": active and event_state != "ended",
            "account": latest["account"] if event_state != "ended" else None,
            "model": latest["model"] if event_state != "ended" else None,
            "observed_at": (
                latest["observed_at"] if event_state != "ended" else None),
        })
    if not configured and not base["active_session"]:
        base.update({
            "state": "disabled", "code": "handoff_disabled",
            "explanation": "Automatic handoff is disabled for future launches.",
            "action": "enable_handoff",
        })
    elif not configured and base["active_session"]:
        base["explanation"] += (
            " The saved preference applies only to the next launch.")
    return base


def _registry_discovery():
    """Read registry state without creating, repairing, or collecting."""
    try:
        account_lifecycle.recover()
    except (account_lifecycle.LifecycleError, registry.RegistryError):
        return "recovery", None, "account_lifecycle_recovery_required"
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


def _diagnostic_code(value):
    return value if isinstance(value, str) \
        and re.fullmatch(r"[a-z0-9_]{1,64}", value) else None


def _observation_age(value, now):
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(value) or value > now:
        return None
    return max(0, int(now - value))


def _onboarding_path():
    return os.path.join(paths.state_dir(), "desktop-onboarding.json")


def _load_onboarding(config=None):
    """Read non-secret first-run progress without creating or repairing it."""
    if (config or {}).get("accounts"):
        return "complete", None
    path = _onboarding_path()
    if not os.path.exists(path):
        return "welcome", None
    if os.path.islink(path) or not os.path.isfile(path):
        return "welcome", "onboarding_progress_unreadable"
    raw = paths.load_json(path)
    if not isinstance(raw, dict) or raw.get("schema") != ONBOARDING_SCHEMA \
            or raw.get("step") not in ONBOARDING_STEPS:
        return "welcome", "onboarding_progress_unreadable"
    step = raw["step"]
    # Completion without an account is meaningful only for the demo. A stale
    # or hand-edited complete marker may never bypass setup into an empty app.
    if step == "complete":
        return "welcome", "onboarding_completion_unbound"
    return step, None


def _save_onboarding(step):
    if step not in ONBOARDING_STEPS:
        raise BridgeError("invalid_onboarding_step", "onboarding step is invalid")
    paths.ensure_private(paths.state_dir())
    paths.write_json_atomic(_onboarding_path(), {
        "schema": ONBOARDING_SCHEMA,
        "step": step,
        "updated_at": int(time.time()),
    })


def _complete_onboarding():
    """Best-effort marker; a registered account independently proves done."""
    try:
        _save_onboarding("complete")
    except OSError:
        pass


def _provider_state(provider):
    """Sanitized capability probe performed only after user disclosure."""
    binary = connect.provider_binary(provider)
    if binary is None:
        return "missing"
    try:
        supported = (connect.desktop_login_prerequisite(provider, binary)
                     if provider == "claude"
                     else connect.desktop_codex_prerequisite(binary))
    except (OSError, ValueError):
        supported = False
    return "ready" if supported else "upgrade_required"


def _onboarding_projection(step, *, candidates=None, config=None,
                           recovery_code=None, probe=False):
    candidates = list(candidates or [])
    connected = {
        provider: sum(1 for row in (config or {}).get("accounts", [])
                      if row.get("provider") == provider)
        for provider in sorted(registry.PROVIDERS)
    }
    providers = []
    for provider in ("claude", "codex"):
        providers.append({
            "provider": provider,
            "state": _provider_state(provider) if probe else "unchecked",
            "candidate_available": any(
                row.get("provider") == provider for row in candidates),
            "connected_count": connected.get(provider, 0),
        })
    return {
        "schema": ONBOARDING_SCHEMA,
        "step": step,
        "resumable": os.path.exists(_onboarding_path()),
        "recovery_code": recovery_code,
        "providers": providers,
    }


def _held_account(row, policy=None):
    """Project a configured slot safely when no public observation exists."""
    return {
        "name": row["name"],
        "provider": row["provider"],
        "identity": _redacted_identity(row.get("expected_email")),
        "plan": "Unknown",
        "note": "No collected reading yet",
        "diagnostic_code": "no_collected_reading",
        "trust_state": None,
        "reserved": row.get("reserved") is True,
        "policy": policy,
        "recovery_action": None,
        "state": "held",
        "windows": {},
    }


def _view(config, public_snapshot=None, *, mode="ready", candidates=None,
          recovery_code=None, onboarding=None, now=None):
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
    policies = {
        row["name"]: account_lifecycle.account_policy(
            row, index, len((config or {}).get("accounts", [])))
        for index, row in enumerate((config or {}).get("accounts", []))
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    projected_accounts = {}
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
            "diagnostic_code": _diagnostic_code(details.get("error_code")),
            "observation_age_seconds": _observation_age(
                details.get("captured_at"), now),
            "trust_state": details.get("trust_state"),
            "reserved": configured.get(row["name"], {}).get("reserved") is True,
            "policy": policies.get(row["name"]),
        })
        policy = entry["policy"] or {}
        reason_action = (_routing_reason_projection(entry["note"])[2]
                         if entry["note"] else None)
        entry["recovery_action"] = (
            "external_reauthentication"
            if entry.get("state") == "held"
            and policy.get("reauthentication") in {
                "keychain_manual", "provider_managed"}
            and reason_action == "reauthenticate_account"
            else None)
        projected_accounts[row["name"]] = entry
    # A snapshot records observation order, not routing preference. Always
    # render configured slots in registry order so a move is reflected in the
    # dashboard immediately, even before the next collection rewrites usage.
    accounts = []
    for row in (config or {}).get("accounts", []):
        account = projected_accounts.get(row["name"])
        accounts.append(account if account is not None else _held_account(
            row, policies.get(row["name"])))
    if config is None:
        accounts.extend(projected_accounts.values())
    if onboarding is None:
        step = "complete" if configured else "welcome"
        onboarding = _onboarding_projection(
            step, candidates=candidates, config=config, probe=False)
    # The first activity collector sampled an incomplete telemetry database and
    # could therefore present precise-looking but materially false totals. Keep
    # the UI fail-closed until the event-level, reconciled collector replaces it.
    account_activity = activity.unavailable(config)
    return {
        "schema": VIEW_SCHEMA,
        "mode": mode,
        "settings": _settings(config),
        "handoff": handoff_health_desktop(
            config, now=now, probe_provider=mode == "ready",
            observe_events=mode == "ready"),
        "candidates": list(candidates or []),
        "onboarding": onboarding,
        "recovery_code": recovery_code,
        "freshness": projected["freshness"],
        "headline": projected["headline"],
        "accounts": accounts,
        "activity": account_activity,
    }


def _demo_view(now=None):
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise BridgeError("invalid_clock", "demo clock must be finite")
    captured = now - 12
    config = {
        "schema_version": 1,
        "dashboard": {"title": "Headroom // Demo", "theme": "midnight",
                      "redact_emails": True},
        "accounts": [
            {"name": "claude-demo", "provider": "claude",
             "home": "/demo/claude"},
            {"name": "codex-demo", "provider": "codex",
             "home": "/demo/codex"},
        ],
    }
    snapshot = {"generated": captured, "accounts": [
        {"name": "claude-demo", "provider": "claude", "ok": True,
         "email": "claude-demo@example.invalid", "plan": "Pro",
         "trust_state": "verified", "captured_at": captured, "stale": False,
         "windows": {
             "5h": {"used_percent": 28, "observed_at": captured,
                    "resets_at": now + 6400},
             "7d": {"used_percent": 43, "observed_at": captured,
                    "resets_at": now + 280000}}},
        {"name": "codex-demo", "provider": "codex", "ok": True,
         "email": "codex-demo@example.invalid", "plan": "ChatGPT Plus",
         "trust_state": "verified", "captured_at": captured, "stale": False,
         "windows": {
             "5h": {"used_percent": 61, "observed_at": captured,
                    "resets_at": now + 4200},
             "7d": {"used_percent": 17, "observed_at": captured,
                    "resets_at": now + 420000}}},
    ]}
    onboarding = _onboarding_projection("demo", config=None, probe=False)
    return _view(config, snapshot, mode="demo", onboarding=onboarding, now=now)


def discover_desktop(now=None):
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        return _view(None, mode="recovery", recovery_code=recovery_code, now=now)
    step, progress_recovery = _load_onboarding(config)
    if step == "demo":
        return _demo_view(now=now)
    if step == "welcome":
        onboarding = _onboarding_projection(
            step, config=config, recovery_code=progress_recovery, probe=False)
        return _view(config, mode="onboarding", onboarding=onboarding, now=now)
    candidates = _candidate_projection(connect.detect_existing(), config)
    if step in {"providers", "accounts"}:
        onboarding = _onboarding_projection(
            step, candidates=candidates, config=config,
            recovery_code=progress_recovery, probe=True)
        return _view(config, mode="onboarding", candidates=candidates,
                     onboarding=onboarding, now=now)
    public = paths.load_json(paths.public_snapshot_path())
    onboarding = _onboarding_projection(
        "complete", candidates=candidates, config=config, probe=False)
    return _view(config, public, mode="ready", candidates=candidates,
                 onboarding=onboarding, now=now)


def onboarding_desktop(action, now=None):
    if action not in {"begin", "accounts", "demo", "back", "restart"}:
        raise BridgeError("invalid_onboarding_action",
                          "onboarding action is invalid")
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    if (config or {}).get("accounts"):
        return discover_desktop(now=now)
    step, _progress_recovery = _load_onboarding(config)
    if action == "begin" and step in {"welcome", "providers"}:
        target = "providers"
    elif action == "accounts" and step in {"providers", "accounts"}:
        target = "accounts"
    elif action == "demo" and step in {
            "welcome", "providers", "accounts", "demo"}:
        target = "demo"
    elif action == "back" and step == "providers":
        target = "welcome"
    elif action == "back" and step in {"accounts", "demo"}:
        target = "providers"
    elif action == "restart":
        target = "welcome"
    else:
        raise BridgeError("invalid_onboarding_transition",
                          "onboarding action is not valid from this step")
    _save_onboarding(target)
    return discover_desktop(now=now)


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


def _validated_settings_patch(patch):
    if not isinstance(patch, dict):
        raise BridgeError("invalid_settings", "settings update must be an object")
    allowed = {
        "theme", "title", "redact_emails", "reserve_percent",
        "auto_handoff", "refresh_interval_seconds", "provider_paths",
        "preferred_terminal", "remember_window", "notifications",
    }
    if set(patch) - allowed:
        raise BridgeError("invalid_settings", "settings update contains unknown fields")
    normalized = dict(patch)
    if "theme" in patch and patch["theme"] not in registry.THEMES:
        raise BridgeError("invalid_setting_theme", "theme is invalid")
    if "title" in patch and not registry._valid_title(patch["title"]):
        raise BridgeError(
            "invalid_setting_title", "title must be 1-80 printable characters")
    for key in ("redact_emails", "auto_handoff", "remember_window"):
        if key in patch and not isinstance(patch[key], bool):
            raise BridgeError(
                "invalid_setting_" + key, f"{key} must be true or false")
    if "reserve_percent" in patch and not registry._valid_number(
            patch["reserve_percent"], 0, 99):
        raise BridgeError(
            "invalid_setting_reserve_percent", "reserve must be 0-99 percent")
    interval = patch.get("refresh_interval_seconds")
    if interval is not None and (
            not isinstance(interval, int) or isinstance(interval, bool)
            or not registry.REFRESH_INTERVAL_MIN <= interval
            <= registry.REFRESH_INTERVAL_MAX):
        raise BridgeError(
            "invalid_setting_refresh_interval",
            f"refresh interval must be {registry.REFRESH_INTERVAL_MIN}-"
            f"{registry.REFRESH_INTERVAL_MAX} seconds")
    if "preferred_terminal" in patch \
            and patch["preferred_terminal"] not in registry.PREFERRED_TERMINALS:
        raise BridgeError(
            "invalid_setting_preferred_terminal", "preferred terminal is invalid")
    if "provider_paths" in patch:
        paths_value = patch["provider_paths"]
        if not isinstance(paths_value, dict) \
                or set(paths_value) - set(registry.PROVIDERS):
            raise BridgeError("invalid_setting_provider_paths",
                              "provider paths are invalid")
        normalized_paths = {}
        for provider, value in paths_value.items():
            if value in (None, ""):
                normalized_paths[provider] = None
                continue
            if not registry._valid_provider_path(value) \
                    or not os.path.isfile(value) or not os.access(value, os.X_OK):
                raise BridgeError(
                    f"invalid_setting_{provider}_path",
                    f"{provider} path must name an executable file")
            normalized_paths[provider] = os.path.realpath(value)
        normalized["provider_paths"] = normalized_paths
    if "notifications" in patch:
        value = patch["notifications"]
        allowed_notifications = {
            "enabled", "reset_enabled", "global_threshold_percent",
            "provider_threshold_percent",
        }
        if not isinstance(value, dict) or set(value) - allowed_notifications:
            raise BridgeError("invalid_setting_notifications",
                              "notification settings are invalid")
        value = dict(value)
        for key in ("enabled", "reset_enabled"):
            if key in value and not isinstance(value[key], bool):
                raise BridgeError(
                    "invalid_setting_notifications", f"{key} must be true or false")
        threshold = value.get("global_threshold_percent")
        if threshold is not None and (
                not isinstance(threshold, int) or isinstance(threshold, bool)
                or not 1 <= threshold <= 99):
            raise BridgeError("invalid_setting_notification_threshold",
                              "notification threshold must be 1-99 percent")
        provider_thresholds = value.get("provider_threshold_percent")
        if provider_thresholds is not None:
            if not isinstance(provider_thresholds, dict) \
                    or set(provider_thresholds) - set(registry.PROVIDERS):
                raise BridgeError("invalid_setting_notification_threshold",
                                  "provider notification thresholds are invalid")
            for provider, threshold in provider_thresholds.items():
                if threshold is not None and (
                        not isinstance(threshold, int) or isinstance(threshold, bool)
                        or not 1 <= threshold <= 99):
                    raise BridgeError(
                        "invalid_setting_notification_threshold",
                        f"{provider} notification threshold must be 1-99 percent")
        normalized["notifications"] = value
    return normalized


def update_settings_desktop(patch, now=None):
    normalized = _validated_settings_patch(patch)
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    if config is None:
        raise BridgeError("no_accounts", "connect an account before saving settings")

    def apply(value):
        dashboard = value.setdefault("dashboard", {})
        routing = value.setdefault("routing", {})
        desktop = value.setdefault("desktop", {})
        for key in ("theme", "title", "redact_emails"):
            if key in normalized:
                dashboard[key] = normalized[key]
        for key in ("reserve_percent", "auto_handoff"):
            if key in normalized:
                routing[key] = normalized[key]
        for key in ("refresh_interval_seconds", "preferred_terminal",
                    "remember_window"):
            if key in normalized:
                desktop[key] = normalized[key]
        if "provider_paths" in normalized:
            provider_paths = desktop.setdefault("provider_paths", {})
            for provider, path in normalized["provider_paths"].items():
                if path is None:
                    provider_paths.pop(provider, None)
                else:
                    provider_paths[provider] = path
        if "notifications" in normalized:
            notifications = desktop.setdefault("notifications", {})
            for key, value in normalized["notifications"].items():
                if key != "provider_threshold_percent":
                    notifications[key] = value
                    continue
                provider_thresholds = notifications.setdefault(
                    "provider_threshold_percent", {})
                for provider, threshold in value.items():
                    if threshold is None:
                        provider_thresholds.pop(provider, None)
                    else:
                        provider_thresholds[provider] = threshold

    try:
        registry.mutate(apply)
    except registry.RegistryError as error:
        raise BridgeError("settings_conflict", "settings could not be committed") from error
    return discover_desktop(now=now)


def _desktop_family(value):
    if value not in DESKTOP_FAMILIES:
        raise BridgeError(
            "invalid_routing_family", "routing family is not supported")
    return value


def _routing_reason_projection(reason):
    """Turn CLI gate text into bounded desktop semantics without leaking it."""
    text = str(reason or "").lower()
    if text.startswith("reserved"):
        return ("reserved", "Reserved accounts are monitored but never routed.",
                "unreserve_account")
    if "routing disabled" in text:
        return ("routing_disabled", "Routing is disabled for this provider.",
                "enable_routing")
    if "leased" in text:
        return ("leased", "Another live launch currently owns this slot.",
                "close_other_session")
    if "quarantined" in text:
        return ("quarantined", "Authentication was rejected for this slot.",
                "reauthenticate_account")
    if "cooldown ledger" in text or "cooldown entry" in text \
            or "quarantine ledger" in text:
        return ("infrastructure_unavailable",
                "Protective routing state cannot be verified safely.",
                "inspect_diagnostics")
    if "cooldown until" in text:
        return ("cooled_down", "This slot is cooling down after a limit.",
                "wait_for_reset")
    if "at 100%" in text or "critical" in text or "below" in text \
            and "reserve" in text:
        return ("capacity_unavailable",
                "Verified capacity is at or below the routing threshold.",
                "wait_for_reset")
    if "auth" in text or "login" in text or "token" in text:
        return ("authentication_required",
                "This slot needs a verified provider login.",
                "reauthenticate_account")
    if "stale" in text or "expired" in text or "clock invalid" in text:
        return ("stale_reading", "The capacity proof is no longer current.",
                "refresh_capacity")
    if "identity" in text or "credential" in text or "trust" in text \
            or "lineage" in text or "bound" in text:
        return ("unverified_reading",
                "The current login cannot be bound to this capacity proof.",
                "refresh_or_reauthenticate")
    if "not found" in text or "cli_missing" in text:
        return ("provider_cli_missing", "The provider CLI is not installed.",
                "install_provider_cli")
    if "no usage" in text or "no fresh" in text or "reading" in text \
            or "window" in text or text.startswith("held:"):
        return ("unverified_reading",
                "No current verified capacity proof is available.",
                "refresh_capacity")
    return ("infrastructure_unavailable",
            "Routing could not prove this slot safe to launch.",
            "inspect_diagnostics")


def _routing_failure(candidates):
    priorities = (
        "infrastructure_unavailable", "authentication_required", "quarantined",
        "leased", "capacity_unavailable", "cooled_down", "stale_reading",
        "unverified_reading", "routing_disabled", "reserved",
    )
    by_code = {row["code"]: row for row in candidates}
    for code in priorities:
        if code in by_code:
            row = by_code[code]
            return {"status": "unavailable", "code": code,
                    "explanation": row["explanation"], "action": row["action"]}
    return {"status": "unavailable", "code": "no_provider_accounts",
            "explanation": "No connected account exists for this provider.",
            "action": "connect_account"}


def routing_preview_desktop(family):
    """Return the CLI router's ordered decision as a sanitized explanation."""
    family = _desktop_family(family)
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("routing_infrastructure_unavailable", recovery_code)
    if config is None:
        raise BridgeError("no_accounts", "connect an account before routing")
    try:
        snapshot = route.ensure_fresh_snapshot()
        ranked = route.candidates(family, snapshot)
    except (OSError, RuntimeError, registry.RegistryError) as error:
        raise BridgeError(
            "routing_infrastructure_unavailable",
            "routing state could not be verified") from error
    selected = next((account for account, reason in ranked if reason is None), None)
    rows = []
    for account, reason in ranked:
        is_selected = selected is not None and account["name"] == selected["name"]
        if reason is None:
            code, explanation, action = (
                ("selected", "This is the engine-selected account.",
                 "copy_or_open") if is_selected else
                ("available", "This account also has proven headroom.",
                 "none"))
        else:
            code, explanation, action = _routing_reason_projection(reason)
        rows.append({
            "name": account["name"], "provider": account["provider"],
            "selected": is_selected, "eligible": reason is None,
            "code": code, "explanation": explanation, "action": action,
        })
    provider = registry.family_provider(family)
    binary = connect.provider_binary(provider)
    if selected is None:
        launch = _routing_failure(rows)
    elif binary is None:
        launch = {
            "status": "unavailable", "code": "provider_cli_missing",
            "explanation": "The selected provider CLI is not installed.",
            "action": "install_provider_cli",
        }
    else:
        launch = {
            "status": "ready", "code": "launch_ready",
            "explanation": "The selected account can be launched safely.",
            "action": "copy_or_open",
        }
    return {
        "schema": ROUTING_SCHEMA, "family": family, "provider": provider,
        "selected": None if selected is None else {
            "name": selected["name"], "provider": selected["provider"]},
        "candidates": rows, "launch": launch,
    }


def _launch_error_from_preview(preview, account_name):
    selected = preview.get("selected")
    if selected is not None and selected.get("name") != account_name:
        return BridgeError(
            "routing_selection_changed",
            "the engine selected a different account; preview again")
    launch = preview["launch"]
    code = launch.get("code")
    mapping = {
        "provider_cli_missing": "provider_cli_missing",
        "authentication_required": "routing_authentication_required",
        "quarantined": "routing_authentication_required",
        "capacity_unavailable": "routing_capacity_unavailable",
        "cooled_down": "routing_capacity_unavailable",
        "leased": "routing_slot_leased",
        "infrastructure_unavailable": "routing_infrastructure_unavailable",
    }
    return BridgeError(mapping.get(code, "routing_no_eligible_account"),
                       launch.get("explanation") or "routing is unavailable")


def routing_launch_intent_desktop(family, account_name):
    family = _desktop_family(family)
    if not isinstance(account_name, str) \
            or not registry.NAME_RE.fullmatch(account_name):
        raise BridgeError("invalid_account_name", "account name is invalid")
    preview = routing_preview_desktop(family)
    selected = preview.get("selected")
    if selected is None or selected.get("name") != account_name \
            or preview["launch"].get("status") != "ready":
        raise _launch_error_from_preview(preview, account_name)
    provider = selected["provider"]
    executable = connect.provider_binary(provider)
    if executable is None:
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    executable = os.path.realpath(executable)
    if not os.path.isabs(executable) or not os.path.isfile(executable) \
            or not os.access(executable, os.X_OK):
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    if getattr(sys, "frozen", False):
        launcher = [os.path.realpath(sys.executable),
                    "--launch-provider", family, account_name]
    else:
        launcher = [os.path.realpath(sys.executable), "-m",
                    "headroom.desktop_bridge", "--launch-provider",
                    family, account_name]
    environment = {
        "HEADROOM_DIR": paths.base_dir(),
        "HEADROOM_SLOT_LEASE": "1",
    }
    command = " ".join(
        [f"{key}={shlex.quote(value)}" for key, value in environment.items()]
        + [shlex.join(launcher)])
    return {
        "schema": LAUNCH_INTENT_SCHEMA, "family": family,
        "provider": provider, "account_name": account_name,
        "preferred_terminal": registry.desktop_settings()["preferred_terminal"],
        "launcher": launcher, "environment": environment,
        "provider_executable": executable, "copy_command": command,
    }


def launch_selected_provider(family, account_name):
    """Frozen launcher entry: re-prove, lease, and exec one provider CLI."""
    intent = routing_launch_intent_desktop(family, account_name)
    os.environ["HEADROOM_SLOT_LEASE"] = "1"
    return route.cmd_exec_selected(
        intent["family"], intent["account_name"],
        [intent["provider_executable"]], launch_note="desktop launch intent")


def _external_reauthentication_target(account_name):
    if not isinstance(account_name, str) \
            or not registry.NAME_RE.fullmatch(account_name):
        raise BridgeError("invalid_account_name", "account name is invalid")
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    accounts = (config or {}).get("accounts", [])
    match = next(((index, row) for index, row in enumerate(accounts)
                  if row.get("name") == account_name), None)
    if match is None:
        raise BridgeError("account_missing", "account no longer exists")
    index, account = match
    policy = account_lifecycle.account_policy(account, index, len(accounts))
    recovery_kind = policy["reauthentication"]
    if recovery_kind == "available":
        raise BridgeError(
            "managed_reauthentication_available",
            "this account has a rollback-safe in-app reauthentication flow")
    if recovery_kind not in {"keychain_manual", "provider_managed"}:
        raise BridgeError(
            "external_reauthentication_unavailable",
            "this account cannot be reauthenticated externally")
    view = discover_desktop()
    projected = next((row for row in view.get("accounts", [])
                      if row.get("name") == account_name), None)
    if projected is None or projected.get("state") != "held" \
            or projected.get("recovery_action") != "external_reauthentication":
        raise BridgeError(
            "reauthentication_not_required",
            "this account does not currently require provider authentication")
    try:
        leased = route.slot_lease_active(account_name)
    except Exception as error:  # noqa: BLE001 - protective state fails closed
        raise BridgeError(
            "routing_infrastructure_unavailable",
            "account lease state could not be verified") from error
    if leased:
        raise BridgeError(
            "account_in_use",
            "close the live provider session before reauthenticating this account")
    executable = connect.provider_binary(account["provider"])
    if executable is None:
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    executable = os.path.realpath(executable)
    if not os.path.isabs(executable) or not os.path.isfile(executable) \
            or not os.access(executable, os.X_OK):
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    return account, recovery_kind, executable


def external_reauthentication_intent_desktop(account_name):
    """Return a bounded provider-login intent for a held external slot.

    The webview receives neither the provider home nor executable. A fresh
    frozen engine process re-proves the account and execs the provider-owned
    login command after the user explicitly opens it in a terminal.
    """
    account, recovery_kind, _executable = _external_reauthentication_target(
        account_name)
    if getattr(sys, "frozen", False):
        launcher = [os.path.realpath(sys.executable),
                    "--launch-reauthentication", account_name]
    else:
        launcher = [os.path.realpath(sys.executable), "-m",
                    "headroom.desktop_bridge", "--launch-reauthentication",
                    account_name]
    return {
        "schema": REAUTH_LAUNCH_INTENT_SCHEMA,
        "provider": account["provider"],
        "account_name": account_name,
        "recovery_kind": recovery_kind,
        "preferred_terminal": registry.desktop_settings()["preferred_terminal"],
        "launcher": launcher,
        "environment": {"HEADROOM_DIR": paths.base_dir()},
    }


def launch_external_reauthentication(account_name):
    """Frozen terminal entry: re-prove and exec one provider login command."""
    intent = external_reauthentication_intent_desktop(account_name)
    config = registry.load()
    account = next((row for row in config["accounts"]
                    if row.get("name") == account_name), None)
    if account is None or account.get("provider") != intent["provider"]:
        raise BridgeError("account_missing", "account no longer exists")
    executable = connect.provider_binary(account["provider"])
    if executable is None:
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    executable = os.path.realpath(executable)
    if not os.path.isabs(executable) or not os.path.isfile(executable) \
            or not os.access(executable, os.X_OK):
        raise BridgeError("provider_cli_missing", "provider CLI is unavailable")
    environment = collector.scrubbed_env()
    environment["HEADROOM_DIR"] = paths.base_dir()
    environment.pop("HEADROOM_SLOT_LEASE", None)
    home_key = "CLAUDE_CONFIG_DIR" if account["provider"] == "claude" \
        else "CODEX_HOME"
    environment[home_key] = registry.expand(account["home"])
    argv = connect.login_argv(
        account["provider"], executable, account.get("expected_email"))
    os.execve(executable, argv, environment)
    return 2


def account_action_desktop(action, name, *, new_name=None,
                           reserved=None, confirmation=None, now=None):
    if action not in {
            "reserve", "unreserve", "move_up", "move_down", "rename", "remove"}:
        raise BridgeError("invalid_account_action", "account action is invalid")
    if not isinstance(name, str) or not registry.NAME_RE.fullmatch(name):
        raise BridgeError("invalid_account_name", "account name is invalid")
    state, config, recovery_code = _registry_discovery()
    if state == "recovery":
        raise BridgeError("recovery_required", recovery_code)
    if config is None:
        raise BridgeError("no_accounts", "no connected accounts are available")
    try:
        if action in {"reserve", "unreserve"}:
            expected = action == "reserve"
            if reserved is not None and reserved is not expected:
                raise BridgeError("invalid_reserved_state",
                                  "reserved action does not match its value")
            account_lifecycle.set_reserved(name, expected)
        elif action in {"move_up", "move_down"}:
            account_lifecycle.move_account(
                name, "up" if action == "move_up" else "down")
        elif action == "rename":
            account_lifecycle.rename_account(name, new_name)
        else:
            if confirmation != name:
                raise BridgeError("removal_confirmation_required",
                                  "type the account name to confirm removal")
            account_lifecycle.remove_account(name)
    except account_lifecycle.LifecycleError as error:
        raise BridgeError(error.code, str(error)) from error
    return discover_desktop(now=now)


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
    _complete_onboarding()
    try:
        return refresh_desktop(now=now)
    except Exception:  # noqa: BLE001 - adoption remains usable while offline
        return discover_desktop(now=now)


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
            "mode": job.get("mode", "connect"),
            "name": job["name"],
            "state": job["state"],
            "progress_code": job["progress_code"],
            "result_code": job.get("result_code"),
            "instructions": job.get("instructions"),
            "view": job.get("view"),
        }

    def start_claude(self, name, expected_email=None):
        return self._start("claude", name, expected_email)

    def start_codex(self, name, expected_email=None):
        return self._start("codex", name, expected_email)

    def start_reauthentication(self, name):
        if not isinstance(name, str) or not registry.NAME_RE.fullmatch(name):
            raise BridgeError("invalid_account_name", "account name is invalid")
        state, config, recovery_code = _registry_discovery()
        if state == "recovery":
            raise BridgeError("recovery_required", recovery_code)
        account = next((row for row in (config or {}).get("accounts", [])
                        if row.get("name") == name), None)
        if account is None:
            raise BridgeError("account_missing", "account no longer exists")
        policy = account_lifecycle.account_policy(
            account, 0, len(config["accounts"]))
        if policy["reauthentication"] != "available":
            raise BridgeError(
                "reauthentication_" + policy["reauthentication"],
                "this account must be reauthenticated in its provider")
        return self._start(
            account["provider"], name, account.get("expected_email"),
            reauthenticate=True)

    def _start(self, provider, name, expected_email=None, reauthenticate=False):
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
        if not reauthenticate and any(
                row.get("name") == name for row in config["accounts"]):
            raise BridgeError("duplicate_account_name", "account name is already in use")
        with self._lock:
            if self._job and self._job["state"] in {"running", "cancelling"}:
                raise BridgeError("login_in_progress", "another login is in progress")
            job = {
                "job_id": secrets.token_hex(12), "provider": provider,
                "mode": "reauthenticate" if reauthenticate else "connect",
                "name": name, "state": "running", "progress_code": "queued",
                "cancel": threading.Event(),
            }
            self._job = job
            thread = threading.Thread(
                target=self._run, args=(job, config, expected_email),
                name=f"headroom-{provider}-login", daemon=False)
            job["thread"] = thread
            thread.start()
            return self._projection(job)

    def _run(self, job, config, expected_email):
        def progress(code, details=None):
            with self._lock:
                if self._job is job and job["state"] == "running":
                    job["progress_code"] = code
                    job["instructions"] = details
        try:
            if job["provider"] == "claude":
                outcome = connect.desktop_connect_fresh(
                    config, job["name"], "claude", expected_email=expected_email,
                    cancel_event=job["cancel"], progress=progress,
                    reauthenticate=job.get("mode") == "reauthenticate")
            else:
                outcome = connect.desktop_connect_codex_device(
                    config, job["name"], expected_email=expected_email,
                    cancel_event=job["cancel"], progress=progress,
                    reauthenticate=job.get("mode") == "reauthenticate")
            if outcome.get("ok"):
                progress("publishing")
                _complete_onboarding()
                if job["provider"] == "codex" and outcome.get("observation"):
                    now = int(time.time())
                    observed = outcome["observation"]
                    public = {"generated": now, "accounts": [{
                        "name": job["name"], "provider": "codex", "ok": True,
                        "email": observed.get("email"),
                        "plan": observed.get("plan"), "trust_state": "verified",
                        "captured_at": now, "stale": False,
                        "windows": observed.get("windows") or {},
                    }]}
                    view = _view(registry.load(), public, now=now)
                else:
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
                        "instructions": None, "view": view,
                    })
        except Exception:  # noqa: BLE001 - no detail crosses desktop boundary
            with self._lock:
                if self._job is job:
                    job.update({"state": "failed", "progress_code": "complete",
                                "result_code": "internal_error",
                                "instructions": None, "view": None})

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
                "claude_login", "codex_device_login", "onboarding",
                "account_lifecycle", "reauthentication",
                "resilient_collection", "validated_settings",
                "routing_launch", "provider_reauthentication_launch",
                "handoff_health", "shutdown"],
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
    if command == "onboarding":
        if set(args) - {"action", "now"}:
            raise BridgeError("invalid_args", "onboarding arguments are invalid")
        return onboarding_desktop(args.get("action"), args.get("now")), False
    if command == "account_action":
        if set(args) - {
                "action", "name", "new_name", "reserved", "confirmation", "now"}:
            raise BridgeError("invalid_args", "account action arguments are invalid")
        return account_action_desktop(
            args.get("action"), args.get("name"),
            new_name=args.get("new_name"), reserved=args.get("reserved"),
            confirmation=args.get("confirmation"), now=args.get("now")), False
    if command == "adopt":
        if set(args) - {"candidate_id", "name", "now"}:
            raise BridgeError("invalid_args", "adopt arguments are invalid")
        return adopt_desktop(args.get("candidate_id"), args.get("name"),
                             args.get("now")), False
    if command == "refresh":
        if set(args) - {"now"}:
            raise BridgeError("invalid_args", "refresh arguments are invalid")
        return refresh_desktop(args.get("now")), False
    if command == "update_settings":
        if set(args) - {"patch", "now"}:
            raise BridgeError("invalid_args", "settings arguments are invalid")
        return update_settings_desktop(
            args.get("patch"), now=args.get("now")), False
    if command == "routing_preview":
        if set(args) != {"family"}:
            raise BridgeError("invalid_args", "routing preview arguments are invalid")
        return routing_preview_desktop(args.get("family")), False
    if command == "routing_launch_intent":
        if set(args) != {"family", "account_name"}:
            raise BridgeError("invalid_args", "launch intent arguments are invalid")
        return routing_launch_intent_desktop(
            args.get("family"), args.get("account_name")), False
    if command == "external_reauthentication_intent":
        if set(args) != {"account_name"}:
            raise BridgeError(
                "invalid_args", "reauthentication intent arguments are invalid")
        return external_reauthentication_intent_desktop(
            args.get("account_name")), False
    if command == "start_claude_login":
        if set(args) - {"name", "expected_email"}:
            raise BridgeError("invalid_args", "login arguments are invalid")
        return LOGIN_MANAGER.start_claude(
            args.get("name"), args.get("expected_email")), False
    if command == "start_codex_login":
        if set(args) - {"name", "expected_email"}:
            raise BridgeError("invalid_args", "login arguments are invalid")
        return LOGIN_MANAGER.start_codex(
            args.get("name"), args.get("expected_email")), False
    if command == "start_reauthentication":
        if set(args) != {"name"}:
            raise BridgeError("invalid_args", "reauthentication arguments are invalid")
        return LOGIN_MANAGER.start_reauthentication(args.get("name")), False
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


def cli_main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv:
        if len(argv) == 3 and argv[0] == "--launch-provider":
            try:
                return launch_selected_provider(argv[1], argv[2])
            except BridgeError as error:
                print(f"headroom: provider launch refused ({error.code})",
                      file=sys.stderr)
                return 2
            except Exception:  # noqa: BLE001 - terminal sees stable diagnostics only
                print("headroom: provider launch failed safely", file=sys.stderr)
                return 2
        if len(argv) == 2 and argv[0] == "--launch-reauthentication":
            try:
                return launch_external_reauthentication(argv[1])
            except BridgeError as error:
                print(f"headroom: reauthentication refused ({error.code})",
                      file=sys.stderr)
                return 2
            except Exception:  # noqa: BLE001 - stable terminal diagnostic only
                print("headroom: reauthentication failed safely", file=sys.stderr)
                return 2
        print("headroom: unsupported desktop engine arguments", file=sys.stderr)
        return 2
    return main()


if __name__ == "__main__":
    raise SystemExit(cli_main())
