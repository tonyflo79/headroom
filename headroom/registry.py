"""Account registry: load, validate, and query config.json.

The registry is intentionally boring. Each account is a named *slot* bound to
one provider and one isolated CLI config home. Identity (email, plan) is
*discovered* from the provider at collect time, never trusted from config —
config only records what the operator expects, so a clobbered login can be
detected.

Config shape (schema_version 1)::

    {
      "schema_version": 1,
      "dashboard": {"theme": "midnight", "title": "AI Fleet",
                     "redact_emails": false, "port": 8377},
      "accounts": [
        {"name": "personal", "provider": "claude",
         "home": "~/.claude",  # or ~/.headroom/homes/personal
         "expected_email": "me@example.com",  # optional but recommended
         "reserved": false}    # optional: true = tracked but never routed to
      ]
    }

A ``reserved: true`` account is still collected and shown on the dashboard,
but routing never selects it: not for `pick`/`env`, not as a launch account,
and never as a rotation/handoff target. Use it for a slot that belongs to
some other workflow and must not be consumed by automatic rotation.
"""
import contextlib
import fcntl
import os
import re

from . import paths

PROVIDERS = ("claude", "codex")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
THEMES = ("midnight", "minimal", "chrome", "paper", "terminal")
PREFERRED_TERMINALS = ("terminal", "iterm", "warp")
REFRESH_INTERVAL_MIN = 60
REFRESH_INTERVAL_MAX = 3600
DEFAULT_DASHBOARD = {
    "theme": "midnight",
    "title": "AI Fleet",
    "redact_emails": True,
    "port": 8377,
}
DEFAULT_DESKTOP = {
    "refresh_interval_seconds": 300,
    "provider_paths": {},
    "preferred_terminal": "terminal",
    "remember_window": True,
    "notifications": {
        "enabled": False,
        "reset_enabled": False,
        "global_threshold_percent": 20,
        "provider_threshold_percent": {},
    },
}

# Model-family -> provider. `pick`/`run` accept any model string; family()
# reduces it to one of these.
FAMILY_PROVIDER = {
    "opus": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    "fable": "claude",
    "claude": "claude",
    "codex": "codex",
    "gpt": "codex",
}


class RegistryError(ValueError):
    pass


def family(model):
    model = (model or "").lower().strip()
    for name in ("fable", "opus", "sonnet", "haiku", "codex", "gpt"):
        if name in model:
            return "codex" if name == "gpt" else name
    if not model or "claude" in model:
        return "claude"
    # An unknown model must not silently route as generic Claude — a typo'd
    # scoped model would bypass its own weekly cap.
    raise RegistryError(
        f"unknown model family: {model!r} (use opus/sonnet/haiku/claude/codex)")


def family_provider(fam):
    return FAMILY_PROVIDER.get(fam, "claude")


def expand(path):
    # realpath so two paths that resolve to the same home (one via a symlink)
    # canonicalize identically for storage and duplicate detection
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RegistryError("config.json missing or wrong schema_version (expected 1)")
    dashboard = config.get("dashboard")
    if dashboard is not None and not isinstance(dashboard, dict):
        raise RegistryError("dashboard settings must be an object")
    dashboard = dashboard or {}
    if "theme" in dashboard and dashboard["theme"] not in THEMES:
        raise RegistryError(f"dashboard theme must be one of {THEMES}")
    if "title" in dashboard and not _valid_title(dashboard["title"]):
        raise RegistryError("dashboard title must be 1-80 printable characters")
    if "redact_emails" in dashboard \
            and not isinstance(dashboard["redact_emails"], bool):
        raise RegistryError("dashboard redact_emails must be true or false")

    routing = config.get("routing")
    if routing is not None and not isinstance(routing, dict):
        raise RegistryError("routing settings must be an object")
    routing = routing or {}
    if "reserve_percent" in routing \
            and not _valid_number(routing["reserve_percent"], 0, 99):
        raise RegistryError("routing reserve_percent must be between 0 and 99")
    if "auto_handoff" in routing \
            and not isinstance(routing["auto_handoff"], bool):
        raise RegistryError("routing auto_handoff must be true or false")

    desktop = config.get("desktop")
    if desktop is not None:
        _validate_desktop(desktop)

    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RegistryError("config.json has no accounts; run `headroom setup`")
    names, homes = set(), set()
    for account in accounts:
        if not isinstance(account, dict):
            raise RegistryError("account entries must be objects")
        name = account.get("name")
        provider = account.get("provider")
        home = account.get("home")
        if not isinstance(name, str) or not name or name in names:
            raise RegistryError(f"account name missing/duplicate: {name!r}")
        if not NAME_RE.fullmatch(name):
            raise RegistryError(
                f"account name {name!r} invalid: lowercase letters, digits, "
                f"- and _ only (max 32 chars)")
        if provider not in PROVIDERS:
            raise RegistryError(f"account {name}: provider must be one of {PROVIDERS}")
        if not isinstance(home, str) or not home:
            raise RegistryError(f"account {name}: home missing")
        # optional fields: validate types when present, never require them —
        # existing configs without these fields must keep loading unchanged
        if "shared_desktop" in account \
                and not isinstance(account["shared_desktop"], bool):
            raise RegistryError(
                f"account {name}: shared_desktop must be true or false")
        if "reserved" in account \
                and not isinstance(account["reserved"], bool):
            raise RegistryError(
                f"account {name}: reserved must be true or false")
        if "handoff_group" in account:
            group = account["handoff_group"]
            if not isinstance(group, str) or not group.strip():
                raise RegistryError(
                    f"account {name}: handoff_group must be a non-empty string")
        resolved = expand(home)
        if resolved in homes:
            raise RegistryError(f"account {name}: home {resolved} already used by another account")
        names.add(name)
        homes.add(resolved)
    return config


def _valid_number(value, minimum, maximum):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and minimum <= value <= maximum)


def _valid_title(value):
    return (isinstance(value, str) and value == value.strip()
            and 1 <= len(value) <= 80
            and all(ord(character) >= 32 and ord(character) != 127
                    for character in value))


def _valid_provider_path(value):
    return (isinstance(value, str) and 1 <= len(value) <= 4096
            and "\0" not in value and os.path.isabs(value))


def _validate_desktop(desktop):
    if not isinstance(desktop, dict):
        raise RegistryError("desktop settings must be an object")
    interval = desktop.get("refresh_interval_seconds", 300)
    if not isinstance(interval, int) or isinstance(interval, bool) \
            or not REFRESH_INTERVAL_MIN <= interval <= REFRESH_INTERVAL_MAX:
        raise RegistryError(
            f"desktop refresh interval must be {REFRESH_INTERVAL_MIN}-"
            f"{REFRESH_INTERVAL_MAX} seconds")
    terminal = desktop.get("preferred_terminal", "terminal")
    if terminal not in PREFERRED_TERMINALS:
        raise RegistryError(
            f"desktop preferred terminal must be one of {PREFERRED_TERMINALS}")
    if "remember_window" in desktop \
            and not isinstance(desktop["remember_window"], bool):
        raise RegistryError("desktop remember_window must be true or false")
    provider_paths = desktop.get("provider_paths", {})
    if not isinstance(provider_paths, dict) \
            or set(provider_paths) - set(PROVIDERS):
        raise RegistryError("desktop provider paths are invalid")
    for provider, value in provider_paths.items():
        if not _valid_provider_path(value):
            raise RegistryError(
                f"desktop {provider} path must be an absolute executable path")
    notifications = desktop.get("notifications", {})
    if not isinstance(notifications, dict):
        raise RegistryError("desktop notification settings must be an object")
    allowed = {"enabled", "reset_enabled", "global_threshold_percent",
               "provider_threshold_percent"}
    if set(notifications) - allowed:
        raise RegistryError("desktop notification settings contain unknown fields")
    for key in ("enabled", "reset_enabled"):
        if key in notifications and not isinstance(notifications[key], bool):
            raise RegistryError(f"desktop notifications {key} must be true or false")
    threshold = notifications.get("global_threshold_percent", 20)
    if not isinstance(threshold, int) or isinstance(threshold, bool) \
            or not 1 <= threshold <= 99:
        raise RegistryError("desktop notification threshold must be 1-99 percent")
    provider_thresholds = notifications.get("provider_threshold_percent", {})
    if not isinstance(provider_thresholds, dict) \
            or set(provider_thresholds) - set(PROVIDERS):
        raise RegistryError("desktop provider notification thresholds are invalid")
    for provider, value in provider_thresholds.items():
        if not isinstance(value, int) or isinstance(value, bool) \
                or not 1 <= value <= 99:
            raise RegistryError(
                f"desktop {provider} notification threshold must be 1-99 percent")


def load():
    path = paths.config_path()
    if not os.path.exists(path):
        raise RegistryError(f"no config at {path}; run `headroom setup` first")
    config = paths.load_json(path)
    if config is None:
        raise RegistryError(
            f"config at {path} exists but is unreadable or not valid JSON; "
            f"fix or delete it, then run `headroom setup`")
    return validate(config)


def accounts(config=None):
    config = load() if config is None else config
    result = []
    for account in config["accounts"]:
        row = dict(account)
        row["home"] = expand(row["home"])
        result.append(row)
    return result


def dashboard_settings(config=None):
    config = load() if config is None else config
    settings = dict(DEFAULT_DASHBOARD)
    settings.update(config.get("dashboard") or {})
    # coerce a wrong-typed port so it can never reach the socket bind as a str
    try:
        port = int(settings.get("port", 8377))
        settings["port"] = port if 1 <= port <= 65535 else 8377
    except (TypeError, ValueError):
        settings["port"] = 8377
    return settings


def desktop_settings(config=None):
    """Return the validated desktop preference projection with safe defaults."""
    config = load() if config is None else validate(config)
    source = config.get("desktop") or {}
    notifications = dict(DEFAULT_DESKTOP["notifications"])
    notifications.update(source.get("notifications") or {})
    notifications["provider_threshold_percent"] = dict(
        (source.get("notifications") or {}).get(
            "provider_threshold_percent") or {})
    return {
        "refresh_interval_seconds": source.get(
            "refresh_interval_seconds", DEFAULT_DESKTOP["refresh_interval_seconds"]),
        "provider_paths": dict(source.get("provider_paths") or {}),
        "preferred_terminal": source.get(
            "preferred_terminal", DEFAULT_DESKTOP["preferred_terminal"]),
        "remember_window": source.get(
            "remember_window", DEFAULT_DESKTOP["remember_window"]),
        "notifications": notifications,
    }


def ordered_for(fam, config=None):
    """Accounts eligible for a model family, in registry (preference) order."""
    provider = family_provider(fam)
    return [account for account in accounts(config) if account["provider"] == provider]


def reserve_percent(config=None):
    """Minimum % of headroom an account must have LEFT to be routable.

    0 (default) = use every account down to its limit. Set e.g. 10 to skip any
    account with under 10% left so a session starts fresh instead of hitting a
    wall mid-task. Read from config['routing']['reserve_percent'], clamped to
    [0, 99]. Never raises — an unreadable/absent config yields 0.0 so routing
    degrades to the default behaviour."""
    try:
        config = load() if config is None else config
    except RegistryError:
        return 0.0
    routing = (config or {}).get("routing")
    if not isinstance(routing, dict):
        return 0.0
    try:
        value = float(routing.get("reserve_percent", 0))
    except (TypeError, ValueError):
        return 0.0
    return value if 0 <= value <= 99 else 0.0


def auto_handoff(config=None):
    """Whether the supervisor may hand a capped session off automatically.

    OFF by default. Automatic account switching is an explicit opt-in because
    ordinary manual routing remains useful without it. A wrong-typed value
    never enables the supervisor.
    """
    try:
        config = load() if config is None else config
    except RegistryError:
        return False
    routing = (config or {}).get("routing")
    return isinstance(routing, dict) and routing.get("auto_handoff") is True


def save(config):
    validate(config)
    paths.write_json_atomic(paths.config_path(), config, mode=0o600)


@contextlib.contextmanager
def config_lock():
    lock_path = paths.config_path() + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def mutate(fn):
    """Locked reload-mutate-validate-save. Raises RegistryError if the config
    doesn't exist or is corrupt (never creates/overwrites here). Use for every
    non-interactive config write so concurrent writers can't lose each other."""
    with config_lock():
        config = load()
        fn(config)
        save(config)
        return config


def remove_account(name):
    """Atomically remove one non-final slot and return its former entry."""
    removed = []

    def _remove(config):
        accounts = config["accounts"]
        match = next((account for account in accounts
                      if account.get("name") == name), None)
        if match is None:
            raise RegistryError(f"no connected account named {name!r}")
        if len(accounts) == 1:
            raise RegistryError("refusing to remove the final connected account")
        config["accounts"] = [account for account in accounts
                              if account.get("name") != name]
        removed.append(dict(match))

    mutate(_remove)
    return removed[0]


def apply_pins(pins):
    """Record usage-org pins WITHOUT clobbering a concurrent account add:
    take the config lock, reload the latest config, merge pins by slot name,
    save. A collector that loaded a stale config can no longer delete an
    account that `connect` added in the meantime."""
    pins = {name: value for name, value in (pins or {}).items() if value}
    if not pins:
        return
    with config_lock():
        config = load()
        changed = False
        for entry in config["accounts"]:
            if entry["name"] in pins and not entry.get("pinned_usage_org"):
                entry["pinned_usage_org"] = pins[entry["name"]]
                changed = True
        if changed:
            save(config)
