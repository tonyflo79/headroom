# Desktop automatic-handoff health validation

This checklist records acceptance for Desktop 12: a passive,
engine-authoritative automatic-handoff status surface and a validated
next-launch preference. The desktop must never duplicate supervisor policy,
mutate a transcript, prove a cap, kill a provider process, or create a second
recovery path.

## Automated gates

From the repository root:

```sh
uv run --python 3.13.12 python -m unittest -q \
  tests.test_v2_supervision tests.test_supervisor tests.test_desktop_bridge
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

Python tests must prove that the CLI and desktop consume one capability
contract; the existing supervisor emits starting, armed, supervision-lost,
loop-guard, and ended transitions; raw reasons never enter health history; and
history is bounded, private, atomic, and symlink-safe. The desktop projection
must distinguish configured, unavailable, downgraded, armed,
supervision-lost, loop-guard, and disabled states without exposing a PID,
supervisor identifier, path, provider response, or raw reason.

Frontend and Rust tests must independently enforce the exact bounded health
schema, reject unknown fields and inconsistent active states, and require the
`handoff_health` engine capability. The settings gate must prove that the
saved automatic-handoff preference uses the existing atomic registry update
and is explicitly `next_launch_only`; a live provider child is never changed
or reclassified by a preference save.

## Packaged health console

1. Launch the exact packaged `Headroom.app` against an isolated
   `HEADROOM_DIR` with non-secret fixture accounts and a controlled executable
   Claude fixture. Do not use a real provider credential for this gate.
2. Confirm Midnight shows the health console on a black terminal surface with
   phosphor-green text and five glowing signal bars. State text and stable
   code must remain readable without color.
3. With no live supervisor event, confirm enabled configuration shows
   `configured`; disabling the preference shows `disabled`. Re-enable it for
   the remaining scenarios.
4. Drive sanitized fixtures for incompatible launch, supervised launch,
   authenticated SessionStart binding, post-launch supervision loss, and the
   three-handoffs-in-ten-minutes loop guard. Confirm the console shows
   `downgraded`, `armed`, `supervision lost`, and `loop guard` distinctly with
   the engine-provided action copy.
5. While an armed controlled child is live, disable automatic handoff in
   Settings. Confirm the child remains live and `armed`, the console explains
   that the saved preference applies to the next launch, and no transcript,
   hook journal, process, lease, or handoff ledger is changed by the save.
6. End the controlled child normally. Confirm the console returns to
   configured/disabled according to the saved preference. Corrupt or replace
   the health history with a symlink and confirm the app fails closed to
   `unavailable` without following or repairing it.
7. Inspect the desktop bootstrap and webview state. Confirm no PID,
   supervisor UUID, raw reason, provider output, transcript path, home path,
   credential, or command material crosses the bridge.
8. Confirm the packaged app and frozen engine open no TCP listener. Quit the
   app and controlled provider; no Headroom process may remain.

## Security invariants

- The supervisor remains the only owner of SessionStart proof, cap proof,
  handoff admission, transcript publication, child signaling, and recovery.
- The existing notification boundary writes one additional sanitized local
  projection; an observer failure can never fail or delay provider launch.
- Health history contains at most 64 exact-schema events and is stored at mode
  `0600` under the private Headroom state directory.
- The bridge composes validated configuration, the CLI capability contract,
  and sanitized events. It never exposes process-control material.
- Rust and JavaScript accept only `headroom_handoff_health@1`; unknown fields,
  arbitrary explanations, and impossible active-state combinations fail
  closed.
- The preference is committed by the engine's existing validated settings
  transaction and affects only a future compatible Claude launch.

## Result record

Record the implementation commit, exact app and engine hashes, architecture,
macOS version, test totals, each semantic state, active-child preference
behavior, history permissions, strict signature result, absence of TCP
listeners, and cleanup state.

Status: implementation complete; exact-package validation pending.

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| pending | pending | automated handoff contracts | 227 focused Python, 23 frontend, and 25 Rust tests passed locally | pass |
