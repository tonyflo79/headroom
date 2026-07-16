"""Side-effect-free command capability contract shared by CLI and desktop."""


def contract():
    """Return command-scoped engine capabilities without claiming on failure."""
    has_marker = has_fallback = has_lease = has_notify = True
    has_auto_handoff = has_handoff_health = False
    try:
        from . import handoff, notify, route, supervisor
        has_marker = callable(getattr(route, "write_launch_marker", None))
        has_fallback = callable(getattr(route, "bare_fallback_exec", None))
        has_lease = callable(getattr(route, "acquire_slot_lease", None))
        has_notify = callable(getattr(notify, "emit", None))
        has_auto_handoff = all(callable(value) for value in (
            getattr(supervisor, "cmd_claude", None),
            getattr(supervisor, "hook_settings", None),
            getattr(handoff, "reserve_automatic", None),
        ))
        has_handoff_health = has_auto_handoff and all(
            callable(getattr(notify, name, None))
            for name in ("emit", "read_health_events"))
    except Exception:  # noqa: BLE001 - capability JSON must still be emitted
        pass
    return {
        "schema": 2,
        "launch_marker": {"claude": has_marker, "codex": has_marker},
        "launch_fallback": {"claude": has_fallback,
                            "codex": has_fallback, "run": False},
        "notify_cmd": has_notify,
        "slot_lease": {"claude": has_lease, "codex": has_lease,
                       "run": False, "fail_closed": has_lease},
        "auto_handoff": {"claude": has_auto_handoff, "codex": False,
                         "health": has_handoff_health},
    }
