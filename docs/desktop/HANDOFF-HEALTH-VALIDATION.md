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

Status: implementation and exact-package validation complete for commit
`d13eb69c4223a5a75811357dd21a7ac9ec3182bd`.

Validated on macOS 26.5.2 (25F84), arm64. The exact packaged executables
were ad-hoc signed for local QA; Developer ID signing and notarization remain
release-pipeline gates.

- App executable SHA-256:
  `1042c8fa52888308a31fcabfaae8b26fee30796dc7ead47a9f0666d08a43d123`
- Frozen engine SHA-256:
  `db4f86b2d9786412e580e5ef58f533f309e9d398002f1a4af79d4fd93889d2b1`
- `codesign --verify --deep --strict` passed; both executables are arm64.
- The final app visually showed the black terminal surface, phosphor-green
  typography and glow, five signal bars, stable state/code/action text, and
  explicit non-color status labels.

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| 2026-07-16T16:13:01Z | `d13eb69` | automated handoff contracts | 227 focused Python, 23 frontend, and 25 Rust tests passed locally; the final inactive-metadata correction additionally passed all 35 desktop-bridge unit tests | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | exact frozen bridge | Required `handoff_health` capability advertised; the strict `headroom_handoff_health@1` projection crossed the packaged stdio boundary | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | semantic state matrix | Packaged engine returned configured, downgraded, armed, supervision-lost, loop-guard, disabled, and fail-closed unavailable states with bounded code/action copy | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | live preference update | Disabling the saved preference left the controlled armed child active, preserved its armed classification, added next-launch-only copy, and left the supervision journal hash unchanged | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | privacy and hostile history | Raw fixture reasons were absent; history and config remained `0600`; a symlink journal returned `handoff_health_unreadable` without following, modifying, or repairing its target | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | terminal health console | Configured, armed, armed-with-disabled-preference, and loop-guard captures were inspected in the exact package; state remained readable without color | pass |
| 2026-07-16T16:13:01Z | `d13eb69` | process and network cleanup | App and frozen engine opened no TCP listener; all Headroom and controlled-provider processes exited after QA | pass |
