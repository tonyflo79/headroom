"""Transactional account policy and lifecycle operations for desktop.

Provider homes and credentials are deliberately outside these mutations.
Multi-file rename/removal uses a private intent journal: a crash before the
commit marker rolls the whole mutation back on next discovery, while a crash
after it rolls the whole mutation forward. Unknown concurrent state refuses
recovery instead of being overwritten.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import time

from . import collect, handoff, paths, registry, route


SCHEMA = "headroom_account_lifecycle@1"
JOURNAL_SCHEMA = "headroom_account_lifecycle_journal@1"
DOCUMENT_KEYS = ("config", "private_snapshot", "public_snapshot",
                 "cooldowns", "quarantine")


class LifecycleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _journal_path():
    return os.path.join(paths.state_dir(), "account-lifecycle.json")


def _expanded_without_following_final(path):
    return os.path.abspath(os.path.expanduser(path))


def home_kind(account):
    """Return ``headroom`` only for a real direct child of homes/."""
    raw = account.get("home") if isinstance(account, dict) else None
    if not isinstance(raw, str) or not raw:
        return "adopted"
    lexical = _expanded_without_following_final(raw)
    resolved = registry.expand(raw)
    root = registry.expand(paths.homes_dir())
    if (os.path.islink(lexical) or not os.path.isdir(lexical)
            or os.path.dirname(resolved) != root):
        return "adopted"
    return "headroom"


def account_policy(account, index, total):
    kind = home_kind(account)
    provider = account.get("provider")
    home = registry.expand(account.get("home", ""))
    if kind != "headroom":
        reauth = "provider_managed"
    elif provider == "claude" and sys.platform == "darwin" \
            and not os.path.isfile(os.path.join(home, ".credentials.json")):
        reauth = "keychain_manual"
    else:
        reauth = "available"
    return {
        "schema": SCHEMA,
        "home_kind": kind,
        "home_retained_on_remove": True,
        "rename_keeps_home": True,
        "reauthentication": reauth,
        "position": index,
        "count": total,
        "can_move_up": index > 0,
        "can_move_down": index + 1 < total,
        "can_remove": total > 1,
    }


def set_reserved(name, reserved):
    if not isinstance(reserved, bool):
        raise LifecycleError("invalid_reserved_state",
                             "reserved state must be true or false")

    def apply(config):
        account = _find(config, name)
        if reserved:
            account["reserved"] = True
        else:
            account.pop("reserved", None)

    return registry.mutate(apply)


def move_account(name, direction):
    if direction not in {"up", "down"}:
        raise LifecycleError("invalid_account_move",
                             "account move must be up or down")

    def apply(config):
        accounts = config["accounts"]
        index = next((i for i, row in enumerate(accounts)
                      if row.get("name") == name), None)
        if index is None:
            raise LifecycleError("account_missing", "account no longer exists")
        target = index - 1 if direction == "up" else index + 1
        if not 0 <= target < len(accounts):
            raise LifecycleError("account_move_boundary",
                                 "account is already at that boundary")
        accounts[index], accounts[target] = accounts[target], accounts[index]

    return registry.mutate(apply)


def rename_account(name, new_name):
    _valid_name(name)
    _valid_name(new_name)
    if name == new_name:
        raise LifecycleError("account_name_unchanged", "account name is unchanged")

    def transform(before):
        config = copy.deepcopy(before["config"]["value"])
        _find(config, name)
        if any(row.get("name") == new_name for row in config["accounts"]):
            raise LifecycleError("duplicate_account_name",
                                 "account name is already in use")
        for row in config["accounts"]:
            if row.get("name") == name:
                row["name"] = new_name
        after = copy.deepcopy(before)
        after["config"] = _present(config)
        for key in ("private_snapshot", "public_snapshot"):
            if after[key]["exists"]:
                after[key]["value"] = _rename_snapshot(
                    after[key]["value"], name, new_name)
        if after["cooldowns"]["exists"]:
            after["cooldowns"]["value"] = _rename_cooldowns(
                after["cooldowns"]["value"], name, new_name)
        if after["quarantine"]["exists"]:
            after["quarantine"]["value"] = _rename_mapping_key(
                after["quarantine"]["value"], name, new_name)
        return after

    return _transaction("rename", name, new_name, transform)


def remove_account(name):
    _valid_name(name)

    def transform(before):
        config = copy.deepcopy(before["config"]["value"])
        _find(config, name)
        if len(config["accounts"]) == 1:
            raise LifecycleError("final_account_refused",
                                 "the final connected account cannot be removed")
        config["accounts"] = [row for row in config["accounts"]
                              if row.get("name") != name]
        after = copy.deepcopy(before)
        after["config"] = _present(config)
        for key in ("private_snapshot", "public_snapshot"):
            if after[key]["exists"]:
                snapshot = after[key]["value"]
                collect._prune_snapshot_slot(snapshot, name)
        if after["cooldowns"]["exists"]:
            after["cooldowns"]["value"] = {
                key: value for key, value in after["cooldowns"]["value"].items()
                if not key.startswith(name + ":")}
        if after["quarantine"]["exists"]:
            after["quarantine"]["value"].pop(name, None)
        return after

    return _transaction("remove", name, None, transform)


def recover():
    """Finish or roll back an interrupted lifecycle transaction."""
    if not os.path.lexists(_journal_path()):
        return False
    with collect.collection_lock(), handoff._handoff_lock(), \
            registry.config_lock(), route._cooldown_lock(), \
            route._quarantine_lock():
        return _recover_unlocked()


def _transaction(operation, name, new_name, transform):
    with collect.collection_lock(), handoff._handoff_lock(), \
            registry.config_lock(), route._cooldown_lock(), \
            route._quarantine_lock():
        _recover_unlocked()
        _refuse_live_state(name)
        before = _read_documents()
        after = transform(before)
        journal = {
            "schema": JOURNAL_SCHEMA, "phase": "prepared",
            "operation": operation, "account": name,
            "new_name": new_name, "created_at": int(time.time()),
            "before": before, "after": after,
        }
        paths.write_json_atomic(_journal_path(), journal)
        try:
            _apply_documents(after)
            journal["phase"] = "committed"
            paths.write_json_atomic(_journal_path(), journal)
            _remove_journal()
        except Exception as error:
            try:
                _apply_documents(before)
                _remove_journal()
            except Exception as rollback_error:
                raise LifecycleError(
                    "lifecycle_recovery_required",
                    "account change interrupted and requires safe recovery") \
                    from rollback_error
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError("account_change_failed",
                                 "account change was rolled back safely") from error
        return registry.load()


def _recover_unlocked():
    path = _journal_path()
    if not os.path.lexists(path):
        return False
    if os.path.islink(path) or not os.path.isfile(path):
        raise LifecycleError("lifecycle_journal_unreadable",
                             "account lifecycle recovery state is unsafe")
    journal = paths.load_json(path)
    if (not isinstance(journal, dict)
            or journal.get("schema") != JOURNAL_SCHEMA
            or journal.get("phase") not in {"prepared", "committed"}
            or not _valid_documents(journal.get("before"))
            or not _valid_documents(journal.get("after"))):
        raise LifecycleError("lifecycle_journal_unreadable",
                             "account lifecycle recovery state is unreadable")
    current = _read_documents()
    before, after = journal["before"], journal["after"]
    for key in DOCUMENT_KEYS:
        if current[key] != before[key] and current[key] != after[key]:
            raise LifecycleError(
                "lifecycle_recovery_conflict",
                "account state changed during lifecycle recovery")
    target = after if journal["phase"] == "committed" else before
    _apply_documents(target)
    _remove_journal()
    return True


def _refuse_live_state(name):
    try:
        if route.slot_lease_active(name):
            raise LifecycleError("account_in_use",
                                 "account is used by a live provider process")
        rows = handoff._validated_automatic_rows(
            handoff._read_jsonl(handoff._ledger_path(), "handoff ledger"))
    except LifecycleError:
        raise
    except Exception as error:
        raise LifecycleError("protective_state_unreadable",
                             "protective account state is unreadable") from error
    finished = {row.get("handoff_id") for row in rows
                if row.get("action") in {"failure", "resume_bound"}}
    if any(row.get("handoff_id") not in finished
           and name in {row.get("source_slot"), row.get("target_slot")}
           for row in rows):
        raise LifecycleError("account_handoff_active",
                             "account has an incomplete handoff")


def _read_documents():
    return {
        "config": _present(registry.load()),
        "private_snapshot": _read_optional(paths.private_snapshot_path()),
        "public_snapshot": _read_optional(paths.public_snapshot_path()),
        "cooldowns": _read_optional_mapping(paths.cooldowns_path()),
        "quarantine": _read_optional_mapping(paths.quarantine_path()),
    }


def _read_optional(path):
    if not os.path.exists(path):
        return _absent()
    value = paths.load_json(path)
    if not isinstance(value, dict):
        raise LifecycleError("protective_state_unreadable",
                             "account state is unreadable")
    return _present(value)


def _read_optional_mapping(path):
    document = _read_optional(path)
    if document["exists"] and not isinstance(document["value"], dict):
        raise LifecycleError("protective_state_unreadable",
                             "protective account state is unreadable")
    return document


def _apply_documents(documents):
    destinations = {
        "private_snapshot": (paths.private_snapshot_path(), 0o600),
        "public_snapshot": (paths.public_snapshot_path(), 0o644),
        "cooldowns": (paths.cooldowns_path(), 0o600),
        "quarantine": (paths.quarantine_path(), 0o600),
    }
    for key, (path, mode) in destinations.items():
        document = documents[key]
        if document["exists"]:
            paths.write_json_atomic(path, document["value"], mode=mode)
        else:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    registry.save(documents["config"]["value"])


def _remove_journal():
    try:
        os.unlink(_journal_path())
    except FileNotFoundError:
        pass


def _rename_snapshot(snapshot, old, new):
    snapshot = copy.deepcopy(snapshot)
    for row in snapshot.get("accounts", []):
        if isinstance(row, dict) and row.get("name") == old:
            row["name"] = new
    warnings = snapshot.get("integrity_warnings")
    if isinstance(warnings, list):
        pattern = re.compile(rf"(?<![a-z0-9_-]){re.escape(old)}(?![a-z0-9_-])")
        snapshot["integrity_warnings"] = [
            pattern.sub(new, warning) if isinstance(warning, str) else warning
            for warning in warnings]
    return snapshot


def _rename_cooldowns(cooldowns, old, new):
    result = {}
    for key, value in cooldowns.items():
        target = new + key[len(old):] if key.startswith(old + ":") else key
        if target in result:
            raise LifecycleError("protective_state_conflict",
                                 "renamed cooldown state would collide")
        result[target] = value
    return result


def _rename_mapping_key(mapping, old, new):
    result = dict(mapping)
    if old not in result:
        return result
    if new in result:
        raise LifecycleError("protective_state_conflict",
                             "renamed protective state would collide")
    result[new] = result.pop(old)
    return result


def _find(config, name):
    account = next((row for row in config.get("accounts", [])
                    if row.get("name") == name), None)
    if account is None:
        raise LifecycleError("account_missing", "account no longer exists")
    return account


def _valid_name(value):
    if not isinstance(value, str) or not registry.NAME_RE.fullmatch(value):
        raise LifecycleError("invalid_account_name", "account name is invalid")


def _present(value):
    return {"exists": True, "value": value}


def _absent():
    return {"exists": False, "value": None}


def _valid_documents(value):
    structurally_valid = (
        isinstance(value, dict) and set(value) == set(DOCUMENT_KEYS)
        and all(isinstance(value[key], dict)
                and isinstance(value[key].get("exists"), bool)
                and set(value[key]) == {"exists", "value"}
                for key in DOCUMENT_KEYS)
        and value["config"]["exists"] is True
        and all((document["exists"] and isinstance(document["value"], dict))
                or (not document["exists"] and document["value"] is None)
                for document in value.values()))
    if not structurally_valid:
        return False
    try:
        registry.validate(value["config"]["value"])
    except registry.RegistryError:
        return False
    return True
