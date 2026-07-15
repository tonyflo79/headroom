"""V2 supervision guarantees: in-process launch fallback, bounded notify
hook, pid-liveness slot lease, and the `headroom caps` capability probe.

The two safety-critical boundaries proven here:
- the launch fallback fires ONLY for failures strictly BEFORE the first
  child CLI process was successfully spawned — never after;
- a slot lease blocks an account only for a LIVE lease held by a DIFFERENT
  pid; dead-pid leases are stale and cleaned, corruption never crashes
  routing.
"""
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from headroom import (  # noqa: E402
    __main__, notify, paths, registry, route, supervisor,
)

IDENTITY = {"account_fingerprint": "AAAA", "credential_digest": "BBBB"}


def usage_row(name, used5=10.0, used7=10.0, captured=None):
    captured = int(time.time()) if captured is None else captured
    return {"name": name, "provider": "claude", "ok": True,
            "routable": True, "trust_state": "verified", "stale": False,
            "captured_at": captured, "identity": dict(IDENTITY),
            "windows": {
                "5h": {"used_percent": used5, "resets_at": captured + 3600,
                       "window_minutes": 300},
                "7d": {"used_percent": used7,
                       "resets_at": captured + 7 * 86400,
                       "window_minutes": 10080},
            }}


class TempDirCase(unittest.TestCase):
    """A fresh HEADROOM_DIR per test, with no launch/lease env leakage."""

    CLEAR_VARS = ("HEADROOM_LAUNCH_MARKER", "HEADROOM_LAUNCH_FALLBACK",
                  "HEADROOM_SLOT_LEASE", "HEADROOM_NOTIFY_CMD",
                  "HEADROOM_NOTIFY_TIMEOUT", "CLAUDE_CONFIG_DIR",
                  "CODEX_HOME")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environ = {key: value for key, value in os.environ.items()
                   if key not in self.CLEAR_VARS}
        environ["HEADROOM_DIR"] = os.path.join(self.temp.name, "headroom")
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        route._HELD_LEASES.clear()

    def account(self, name="acct-a"):
        return {"name": name, "provider": "claude",
                "home": os.path.join(self.temp.name, "homes", name)}


class CapsProbe(TempDirCase):
    def test_caps_prints_versioned_capability_flags(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(__main__._dispatch(["caps"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], 1)
        for key in ("launch_marker", "launch_fallback", "notify_cmd",
                    "slot_lease"):
            self.assertIs(payload[key], True, key)
        self.assertEqual(
            set(payload), {"schema", "launch_marker", "launch_fallback",
                           "notify_cmd", "slot_lease"})


class NotifyDelivery(TempDirCase):
    def notify_script(self):
        out = os.path.join(self.temp.name, "events.log")
        script = os.path.join(self.temp.name, "notify.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n"
                         f"printf '%s\\n' \"$#\" >> {shlex.quote(out)}\n"
                         f"printf '%s\\n' \"$1\" >> {shlex.quote(out)}\n")
        os.chmod(script, 0o755)
        return script, out

    def test_unset_env_is_a_silent_no_op(self):
        self.assertFalse(notify.emit({"event": "launch"}))

    def test_event_is_delivered_as_a_single_json_argument(self):
        script, out = self.notify_script()
        event = {"event": "launch", "mode": "exec", "account": "a",
                 "model": "sonnet", "note": ""}
        with mock.patch.dict(os.environ, {"HEADROOM_NOTIFY_CMD": script}):
            self.assertTrue(notify.emit(event))
        with open(out, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(lines[0], "1")  # exactly one argument
        self.assertEqual(json.loads(lines[1]), event)

    def test_command_with_its_own_args_still_gets_json_last(self):
        script, out = self.notify_script()
        with mock.patch.dict(os.environ,
                             {"HEADROOM_NOTIFY_CMD": f"/bin/sh {script}"}):
            self.assertTrue(notify.emit({"event": "fallback", "reason": "x"}))
        with open(out, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(json.loads(lines[1]),
                         {"event": "fallback", "reason": "x"})

    def test_hung_command_is_killed_within_the_bound(self):
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {
                "HEADROOM_NOTIFY_CMD": "/bin/sh -c 'sleep 30'",
                "HEADROOM_NOTIFY_TIMEOUT": "0.2"}), \
                redirect_stderr(errors):
            started = time.monotonic()
            self.assertFalse(notify.emit({"event": "launch"}))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertIn("timed out", errors.getvalue())

    def test_missing_command_never_raises(self):
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {
                "HEADROOM_NOTIFY_CMD": "/definitely/not/here/notify"}), \
                redirect_stderr(errors):
            self.assertFalse(notify.emit({"event": "launch"}))
        self.assertIn("notify failed", errors.getvalue())

    def test_malformed_command_string_never_raises(self):
        with mock.patch.dict(os.environ,
                             {"HEADROOM_NOTIFY_CMD": "'unclosed"}), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(notify.emit({"event": "launch"}))

    def test_blank_command_is_a_no_op(self):
        with mock.patch.dict(os.environ, {"HEADROOM_NOTIFY_CMD": "   "}):
            self.assertFalse(notify.emit({"event": "launch"}))

    def test_unserializable_event_is_swallowed(self):
        script, _ = self.notify_script()
        with mock.patch.dict(os.environ, {"HEADROOM_NOTIFY_CMD": script}), \
                redirect_stderr(io.StringIO()):
            self.assertFalse(notify.emit({"bad": object()}))

    def test_bogus_timeout_override_keeps_the_default_bound(self):
        script, out = self.notify_script()
        with mock.patch.dict(os.environ, {
                "HEADROOM_NOTIFY_CMD": script,
                "HEADROOM_NOTIFY_TIMEOUT": "bogus"}):
            self.assertTrue(notify.emit({"event": "launch"}))
        self.assertTrue(os.path.exists(out))

    def test_nonzero_exit_of_the_observer_is_ignored(self):
        with mock.patch.dict(os.environ,
                             {"HEADROOM_NOTIFY_CMD": "/bin/sh -c 'exit 3'"}):
            self.assertTrue(notify.emit({"event": "launch"}))


class LaunchFallbackExec(TempDirCase):
    """route.cmd_exec: the fallback boundary is reaching the routed exec."""

    def test_default_off_keeps_the_plain_refusal(self):
        with mock.patch.object(route, "pick", return_value=None), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = route.cmd_exec("sonnet", ["claude"])
        self.assertEqual(code, 2)
        execute.assert_not_called()

    def test_no_account_falls_back_to_bare_cli(self):
        command = ["claude", "--model", "sonnet"]
        with mock.patch.object(route, "pick", return_value=None), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = route.cmd_exec("sonnet", command, fallback=True)
        self.assertEqual(code, 0)
        execute.assert_called_once_with("claude", command)
        events = [call.args[0]["event"] for call in emit.call_args_list]
        self.assertEqual(events, ["fallback"])

    def test_routing_exception_falls_back_instead_of_raising(self):
        with mock.patch.object(route, "pick",
                               side_effect=RuntimeError("collect broke")), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()) as errors:
            code = route.cmd_exec("sonnet", ["claude"], fallback=True)
        self.assertEqual(code, 0)
        execute.assert_called_once_with("claude", ["claude"])
        self.assertIn("collect broke", errors.getvalue())

    def test_unwritable_marker_falls_back_before_any_routed_exec(self):
        account = self.account()
        snapshot = {"generated": time.time(), "accounts": []}
        with mock.patch.dict(os.environ,
                             {"HEADROOM_LAUNCH_MARKER": "relative/m.json"}), \
                mock.patch.object(route, "pick", return_value=account), \
                mock.patch.object(route, "ensure_fresh_snapshot",
                                  return_value=snapshot), \
                mock.patch.object(route, "block_reason", return_value=None), \
                mock.patch.object(route, "cooldowns", return_value={}), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = route.cmd_exec("sonnet", ["claude"], fallback=True)
        self.assertEqual(code, 0)
        # exactly one exec: the bare fallback, never the routed launch too
        execute.assert_called_once_with("claude", ["claude"])
        events = [call.args[0]["event"] for call in emit.call_args_list]
        self.assertEqual(events, ["fallback"])

    def test_boundary_reaching_the_routed_exec_never_falls_back(self):
        account = self.account()
        snapshot = {"generated": time.time(), "accounts": []}
        with mock.patch.object(route, "pick", return_value=account), \
                mock.patch.object(route, "ensure_fresh_snapshot",
                                  return_value=snapshot), \
                mock.patch.object(route, "block_reason", return_value=None), \
                mock.patch.object(route, "cooldowns", return_value={}), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = route.cmd_exec("sonnet", ["claude"], fallback=True)
        self.assertEqual(code, 0)
        execute.assert_called_once()  # the routed exec only — no bare launch
        self.assertEqual(execute.call_args.args[1], ["claude"])
        events = [call.args[0]["event"] for call in emit.call_args_list]
        self.assertEqual(events, ["launch"])  # and never a fallback event

    def test_fallback_exec_failure_reports_127(self):
        with mock.patch.object(route, "pick", return_value=None), \
                mock.patch.object(route.os, "execvp",
                                  side_effect=FileNotFoundError("gone")), \
                redirect_stderr(io.StringIO()) as errors:
            code = route.cmd_exec("sonnet", ["claude"], fallback=True)
        self.assertEqual(code, 127)
        self.assertIn("fallback exec", errors.getvalue())


class LaunchFallbackSupervised(TempDirCase):
    """supervisor.cmd_claude: fallback fires only before the first spawn."""

    def stub_supervisor(self, spawned_any, outcome):
        holder = {}

        class Stub:
            def __init__(self, family, args, account):
                self.spawned_any = False
                holder["instance"] = self
                holder["account"] = account

            def run(self):
                self.spawned_any = spawned_any
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        return Stub, holder

    def test_no_account_falls_back(self):
        with mock.patch.object(supervisor, "_initial_account",
                               return_value=None), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = supervisor.cmd_claude("sonnet", [],
                                         fallback_argv=["claude"])
        self.assertEqual(code, 0)
        execute.assert_called_once_with("claude", ["claude"])
        self.assertEqual(emit.call_args.args[0]["event"], "fallback")

    def test_no_account_without_fallback_keeps_exit_2(self):
        with mock.patch.object(supervisor, "_initial_account",
                               return_value=None), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = supervisor.cmd_claude("sonnet", [])
        self.assertEqual(code, 2)
        execute.assert_not_called()

    def test_preparation_exception_falls_back(self):
        with mock.patch.object(
                supervisor, "_initial_account",
                side_effect=registry.RegistryError("no config")), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()) as errors:
            code = supervisor.cmd_claude("sonnet", [],
                                         fallback_argv=["claude"])
        self.assertEqual(code, 0)
        execute.assert_called_once_with("claude", ["claude"])
        self.assertIn("no config", errors.getvalue())

    def test_preparation_exception_without_fallback_raises(self):
        with mock.patch.object(
                supervisor, "_initial_account",
                side_effect=registry.RegistryError("no config")):
            with self.assertRaises(registry.RegistryError):
                supervisor.cmd_claude("sonnet", [])

    def test_first_spawn_failure_falls_back(self):
        stub, _ = self.stub_supervisor(spawned_any=False, outcome=127)
        with mock.patch.object(supervisor, "_initial_account",
                               return_value=self.account()), \
                mock.patch.object(supervisor, "Supervisor", stub), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = supervisor.cmd_claude("sonnet", [],
                                         fallback_argv=["claude"])
        self.assertEqual(code, 0)
        execute.assert_called_once_with("claude", ["claude"])

    def test_boundary_spawned_child_exit_never_falls_back(self):
        # a capped/failed child AFTER a successful spawn is a normal exit
        stub, _ = self.stub_supervisor(spawned_any=True, outcome=42)
        with mock.patch.object(supervisor, "_initial_account",
                               return_value=self.account()), \
                mock.patch.object(supervisor, "Supervisor", stub), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = supervisor.cmd_claude("sonnet", [],
                                         fallback_argv=["claude"])
        self.assertEqual(code, 42)
        execute.assert_not_called()

    def test_boundary_post_spawn_exception_still_raises(self):
        # even a crash is not a fallback once a child was spawned: the CLI
        # ran; restarting it bare could double a running session
        stub, _ = self.stub_supervisor(
            spawned_any=True, outcome=RuntimeError("post-spawn crash"))
        with mock.patch.object(supervisor, "_initial_account",
                               return_value=self.account()), \
                mock.patch.object(supervisor, "Supervisor", stub), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                supervisor.cmd_claude("sonnet", [], fallback_argv=["claude"])
        execute.assert_not_called()

    def test_boundary_real_spawn_then_clean_child_exit(self):
        # end-to-end shape: a REAL Supervisor spawns a (fake) child that
        # exits nonzero — spawned_any flips and no fallback ever fires
        account = self.account()
        real = supervisor.Supervisor

        class FakeProcess:
            pid = os.getpid()

            @staticmethod
            def poll():
                return 7

        def factory(family, args, chosen):
            return real(family, args, chosen,
                        popen=lambda argv, env=None, cwd=None: FakeProcess(),
                        sleep=lambda seconds: None)

        with mock.patch.object(supervisor, "_initial_account",
                               return_value=account), \
                mock.patch.object(supervisor, "Supervisor", factory), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            code = supervisor.cmd_claude("sonnet", [],
                                         fallback_argv=["claude"])
        self.assertEqual(code, 7)
        execute.assert_not_called()


class NotifyWiring(TempDirCase):
    def test_exec_launch_emits_downgrade_then_launch(self):
        account = self.account()
        snapshot = {"generated": time.time(), "accounts": []}
        with mock.patch.object(route, "pick", return_value=account), \
                mock.patch.object(route, "ensure_fresh_snapshot",
                                  return_value=snapshot), \
                mock.patch.object(route, "block_reason", return_value=None), \
                mock.patch.object(route, "cooldowns", return_value={}), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp"), \
                redirect_stderr(io.StringIO()):
            route.cmd_exec("sonnet", ["claude"],
                           launch_note="auto-handoff disabled: --settings")
        payloads = [call.args[0] for call in emit.call_args_list]
        self.assertEqual([p["event"] for p in payloads],
                         ["downgrade", "launch"])
        self.assertEqual(payloads[0]["account"], "acct-a")
        self.assertEqual(payloads[0]["reason"],
                         "auto-handoff disabled: --settings")
        self.assertEqual(payloads[1]["mode"], "exec")
        self.assertEqual(payloads[1]["model"], "sonnet")
        self.assertEqual(payloads[1]["note"],
                         "auto-handoff disabled: --settings")

    def test_exec_launch_without_note_emits_launch_only(self):
        account = self.account()
        snapshot = {"generated": time.time(), "accounts": []}
        with mock.patch.object(route, "pick", return_value=account), \
                mock.patch.object(route, "ensure_fresh_snapshot",
                                  return_value=snapshot), \
                mock.patch.object(route, "block_reason", return_value=None), \
                mock.patch.object(route, "cooldowns", return_value={}), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(route.os, "execvp"), \
                redirect_stderr(io.StringIO()):
            route.cmd_exec("sonnet", ["claude"])
        self.assertEqual([call.args[0]["event"]
                          for call in emit.call_args_list], ["launch"])

    def test_supervised_spawn_emits_launch_once_for_generation_one(self):
        account = self.account()
        runner = supervisor.Supervisor(
            "sonnet", [], account, popen=mock.Mock(return_value=mock.Mock()))
        with mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(runner, "_settings_file", return_value=""):
            runner._spawn(account, [], self.temp.name, False)
            runner._spawn(account, [], self.temp.name, False)
        emit.assert_called_once()
        payload = emit.call_args.args[0]
        self.assertEqual(payload["event"], "launch")
        self.assertEqual(payload["mode"], "supervised")
        self.assertEqual(payload["account"], "acct-a")
        self.assertEqual(payload["model"], "sonnet")
        self.assertTrue(runner.spawned_any)

    def test_spawn_refusal_emits_no_launch_event(self):
        account = self.account()
        popen = mock.Mock()
        runner = supervisor.Supervisor("sonnet", [], account, popen=popen)
        with mock.patch.object(route, "write_launch_marker",
                               return_value=False), \
                mock.patch.object(notify, "emit") as emit, \
                mock.patch.object(runner, "_settings_file", return_value=""), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(supervisor.SupervisorError):
                runner._spawn(account, [], self.temp.name, False)
        emit.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(runner.spawned_any)

    def test_bind_timeout_emits_supervision_lost_once(self):
        account = self.account()
        polls = iter([None, None, 0])

        class FakeProcess:
            pid = os.getpid()

            @staticmethod
            def poll():
                return next(polls)

        clock = {"t": 1000.0}
        runner = supervisor.Supervisor(
            "sonnet", [], account,
            popen=lambda argv, env=None, cwd=None: FakeProcess(),
            now=lambda: clock["t"], sleep=lambda seconds: None)
        with mock.patch.object(notify, "emit") as emit, \
                redirect_stderr(io.StringIO()):
            child = runner._spawn(account, [], self.temp.name, True)
            clock["t"] = 1000.0 + supervisor.BIND_TIMEOUT + 1
            outcome = runner._monitor(child)
        self.assertEqual(outcome, 0)
        events = [call.args[0] for call in emit.call_args_list]
        self.assertEqual([event["event"] for event in events],
                         ["launch", "supervision_lost"])
        self.assertEqual(events[1]["account"], "acct-a")
        self.assertIn("SessionStart hook never bound", events[1]["reason"])
        self.assertFalse(child.automation)


class SlotLease(TempDirCase):
    def lease_env(self):
        return mock.patch.dict(os.environ, {"HEADROOM_SLOT_LEASE": "1"})

    def live_foreign_pid(self):
        process = subprocess.Popen(
            ["sleep", "60"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process.pid

    def write_lease(self, name, pid):
        paths.write_json_atomic(route._lease_path(name), {
            "account": name, "pid": pid, "family": "sonnet",
            "written_at": time.time()})

    def test_disabled_is_a_complete_no_op(self):
        account = self.account()
        self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
        self.assertFalse(os.path.exists(route._lease_path("acct-a")))
        with self.lease_env():
            self.write_lease("acct-a", self.live_foreign_pid())
        # feature off: even a live foreign lease is invisible
        self.assertIsNone(route.lease_holder("acct-a"))
        self.assertEqual(
            route.block_reason(account, "sonnet", None, {}, time.time()),
            "no usage reading yet")

    def test_acquire_writes_the_lease_payload(self):
        with self.lease_env():
            self.assertTrue(route.acquire_slot_lease(self.account(), "sonnet"))
        with open(route._lease_path("acct-a"), encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["account"], "acct-a")
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["family"], "sonnet")
        self.assertIsInstance(payload["written_at"], float)

    def test_own_lease_never_blocks_and_can_be_reacquired(self):
        account = self.account()
        with self.lease_env():
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
            self.assertIsNone(route.lease_holder("acct-a"))
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
            self.assertEqual(
                route.block_reason(account, "sonnet", None, {}, time.time()),
                "no usage reading yet")

    def test_live_foreign_lease_blocks_routing_and_acquire(self):
        account = self.account()
        with self.lease_env():
            pid = self.live_foreign_pid()
            self.write_lease("acct-a", pid)
            self.assertEqual(route.lease_holder("acct-a"), pid)
            reason = route.block_reason(account, "sonnet", None, {},
                                        time.time())
            self.assertIn("slot leased by another live launch", reason)
            self.assertFalse(route.acquire_slot_lease(account, "sonnet"))

    def test_dead_pid_lease_is_stale_ignored_and_cleaned(self):
        account = self.account()
        with self.lease_env():
            process = subprocess.Popen(
                ["sleep", "60"], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            process.kill()
            process.wait()
            self.write_lease("acct-a", process.pid)
            self.assertIsNone(route.lease_holder("acct-a"))
            self.assertFalse(os.path.exists(route._lease_path("acct-a")))
            self.assertEqual(
                route.block_reason(account, "sonnet", None, {}, time.time()),
                "no usage reading yet")
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))

    def test_corrupt_lease_never_crashes_and_never_blocks(self):
        account = self.account()
        with self.lease_env():
            os.makedirs(route._leases_dir(), exist_ok=True)
            with open(route._lease_path("acct-a"), "w",
                      encoding="utf-8") as handle:
                handle.write("not json {{{")
            self.assertIsNone(route.lease_holder("acct-a"))
            self.assertEqual(
                route.block_reason(account, "sonnet", None, {}, time.time()),
                "no usage reading yet")
            # acquire clears the corrupt file and claims cleanly
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
            with open(route._lease_path("acct-a"),
                      encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["pid"], os.getpid())

    def test_unusable_pid_values_are_treated_as_no_lease(self):
        with self.lease_env():
            for bad in ("123", True, -5, 0, None):
                with self.subTest(pid=bad):
                    paths.write_json_atomic(
                        route._lease_path("acct-a"),
                        {"account": "acct-a", "pid": bad})
                    self.assertIsNone(route.lease_holder("acct-a"))

    def test_release_removes_only_our_own_lease(self):
        account = self.account()
        with self.lease_env():
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
            route.release_slot_leases()
            self.assertFalse(os.path.exists(route._lease_path("acct-a")))
            # a lease meanwhile re-claimed by another pid is left alone
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
            self.write_lease("acct-a", self.live_foreign_pid())
            route.release_slot_leases()
            self.assertTrue(os.path.exists(route._lease_path("acct-a")))

    def test_unavailable_lease_dir_degrades_to_launching_unleased(self):
        account = self.account()
        with self.lease_env(), redirect_stderr(io.StringIO()) as errors:
            paths.ensure_private(paths.state_dir())
            with open(route._leases_dir(), "w", encoding="utf-8") as handle:
                handle.write("a file where the directory should be")
            self.assertTrue(route.acquire_slot_lease(account, "sonnet"))
        self.assertIn("slot lease unavailable", errors.getvalue())

    def test_concurrent_initial_launches_pick_different_accounts(self):
        homes = {name: os.path.join(self.temp.name, "homes", name)
                 for name in ("acct-a", "acct-b")}
        registry.save({"schema_version": 1, "accounts": [
            {"name": name, "provider": "claude", "home": home}
            for name, home in homes.items()]})
        snapshot = {"generated": time.time(),
                    "accounts": [usage_row("acct-a"), usage_row("acct-b")]}
        with self.lease_env(), \
                mock.patch.object(route.collector, "local_binding",
                                  return_value=("AAAA", "BBBB")):
            ranked = route.candidates("sonnet", snapshot)
            self.assertEqual(
                [(account["name"], reason) for account, reason in ranked],
                [("acct-a", None), ("acct-b", None)])
            # a second launcher holds acct-a: this launcher must diverge
            self.write_lease("acct-a", self.live_foreign_pid())
            ranked = route.candidates("sonnet", snapshot)
            by_name = {account["name"]: reason for account, reason in ranked}
            self.assertIsNone(by_name["acct-b"])
            self.assertIn("slot leased", by_name["acct-a"])
            self.assertEqual(ranked[0][0]["name"], "acct-b")

    def test_cmd_exec_repicks_when_the_claim_race_is_lost(self):
        # both launchers passed selection; the other one claimed acct-a in
        # the race window — this exec re-picks and launches acct-b
        account_a, account_b = self.account("acct-a"), self.account("acct-b")
        snapshot = {"generated": time.time(), "accounts": []}
        with self.lease_env(), \
                mock.patch.object(route, "pick",
                                  side_effect=[account_a, account_b]), \
                mock.patch.object(route, "ensure_fresh_snapshot",
                                  return_value=snapshot), \
                mock.patch.object(route, "block_reason", return_value=None), \
                mock.patch.object(route, "cooldowns", return_value={}), \
                mock.patch.object(route.os, "execvp") as execute, \
                redirect_stderr(io.StringIO()):
            self.write_lease("acct-a", self.live_foreign_pid())
            route.cmd_exec("sonnet", ["claude"])
            selected = os.environ.get("CLAUDE_CONFIG_DIR")
        execute.assert_called_once()
        self.assertEqual(selected, account_b["home"])
        with open(route._lease_path("acct-b"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pid"], os.getpid())

    def test_cmd_claude_repicks_when_the_claim_race_is_lost(self):
        account_a, account_b = self.account("acct-a"), self.account("acct-b")
        stub_accounts = []

        class Stub:
            def __init__(self, family, args, chosen):
                self.spawned_any = True
                stub_accounts.append(chosen)

            @staticmethod
            def run():
                return 0

        with self.lease_env(), \
                mock.patch.object(supervisor, "_initial_account",
                                  side_effect=[account_a, account_b]), \
                mock.patch.object(supervisor, "Supervisor", Stub), \
                redirect_stderr(io.StringIO()):
            self.write_lease("acct-a", self.live_foreign_pid())
            code = supervisor.cmd_claude("sonnet", [])
        self.assertEqual(code, 0)
        self.assertEqual([entry["name"] for entry in stub_accounts],
                         ["acct-b"])


class CliWiringV2(TempDirCase):
    def test_claude_flag_is_stripped_and_enables_exec_fallback(self):
        with mock.patch.object(registry, "auto_handoff", return_value=False), \
                mock.patch("headroom.route.cmd_exec",
                           return_value=5) as execute:
            code = __main__._dispatch(
                ["claude", "--headroom-launch-fallback", "--model", "sonnet"])
        self.assertEqual(code, 5)
        execute.assert_called_once_with(
            "sonnet", ["claude", "--model", "sonnet"],
            launch_note="auto-handoff not enabled", fallback=True)

    def test_env_var_enables_fallback_without_the_flag(self):
        with mock.patch.dict(os.environ,
                             {"HEADROOM_LAUNCH_FALLBACK": "1"}), \
                mock.patch.object(registry, "auto_handoff",
                                  return_value=False), \
                mock.patch("headroom.route.cmd_exec",
                           return_value=5) as execute:
            __main__._dispatch(["claude", "--model", "sonnet"])
        execute.assert_called_once_with(
            "sonnet", ["claude", "--model", "sonnet"],
            launch_note="auto-handoff not enabled", fallback=True)

    def test_defaults_keep_the_exact_legacy_call_shape(self):
        with mock.patch.object(registry, "auto_handoff", return_value=False), \
                mock.patch("headroom.route.cmd_exec",
                           return_value=5) as execute:
            __main__._dispatch(["claude", "--model", "sonnet"])
        execute.assert_called_once_with(
            "sonnet", ["claude", "--model", "sonnet"],
            launch_note="auto-handoff not enabled")

    def test_codex_flag_is_stripped_and_enables_fallback(self):
        with mock.patch("headroom.route.cmd_exec", return_value=7) as execute:
            code = __main__._dispatch(["codex", "--headroom-launch-fallback"])
        self.assertEqual(code, 7)
        execute.assert_called_once_with("codex", ["codex"], launch_note="",
                                        fallback=True)

    def test_codex_flag_after_separator_passes_through(self):
        with mock.patch("headroom.route.cmd_exec", return_value=0) as execute:
            __main__._dispatch(["codex", "--", "--headroom-launch-fallback"])
        execute.assert_called_once_with(
            "codex", ["codex", "--", "--headroom-launch-fallback"],
            launch_note="")

    def test_supervised_launch_gets_the_bare_fallback_argv(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch.object(registry, "auto_handoff", return_value=True), \
                mock.patch.object(__main__.sys, "stdin", tty), \
                mock.patch.object(__main__.sys, "stdout", tty), \
                mock.patch.object(__main__.sys, "stderr", tty), \
                mock.patch("headroom.supervisor.cmd_claude",
                           return_value=9) as run:
            code = __main__._dispatch(
                ["claude", "--headroom-launch-fallback", "--model", "sonnet"])
        self.assertEqual(code, 9)
        run.assert_called_once_with(
            "sonnet", ["--model", "sonnet"],
            fallback_argv=["claude", "--model", "sonnet"])

    def test_split_headroom_flags_respects_values_and_separator(self):
        cleaned, found = supervisor.split_headroom_flags([
            "--model", "--headroom-launch-fallback",
            "--headroom-launch-fallback", "--",
            "--headroom-launch-fallback"])
        self.assertEqual(cleaned, [
            "--model", "--headroom-launch-fallback", "--",
            "--headroom-launch-fallback"])
        self.assertEqual(found, {"--headroom-launch-fallback"})


if __name__ == "__main__":
    unittest.main()
