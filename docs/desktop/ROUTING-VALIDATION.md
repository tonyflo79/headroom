# Desktop routing and provider launch validation

This checklist records acceptance for Desktop 11: an engine-authoritative,
human-readable routing preview and a safely bounded way to copy or open the
selected provider CLI. It must use the same decision as the CLI router and
must never accept arbitrary command material from the webview.

## Automated gates

From the repository root:

```sh
uv run --python 3.13.12 python -m unittest -q \
  tests.test_desktop_bridge \
  tests.test_v2_supervision.SlotLease \
  tests.test_headroom.BlockReasonFailClosed \
  tests.test_headroom.CodexBlockReasonFailClosed \
  tests.test_headroom.GreatestHeadroom \
  tests.test_headroom.ReservedAccounts
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

The bridge tests must prove that selected, reserved, stale, unverified,
cooled-down, quarantined, leased, and infrastructure-held accounts receive
bounded stable codes without raw provider or router text. Launch-intent tests
must prove safe quoting, provider executable verification, exact-account
selection, and distinct authentication, capacity, lease, infrastructure, and
missing-CLI errors.

Rust and frontend tests must prove that preview and intent schemas are strict
and bounded, unknown fields are refused, and command/environment material can
never cross from JavaScript into a privileged native command. Route tests must
prove that the final launcher rechecks the exact account and never silently
repicks after a capacity change or lost lease race.

## Packaged routing console

1. Launch the exact packaged `Headroom.app` against an isolated
   `HEADROOM_DIR` with non-secret fixture accounts and a controlled executable
   provider fixture. Do not use a real provider credential for this gate.
2. Confirm Midnight presents the console on a black background with
   phosphor-green text and glowing controls/bars. Switching themes may change
   tokens but not routing meaning.
3. Preview Claude and Codex families. Confirm the selected row matches the
   frozen engine's ordered candidate result and every skipped row explains its
   stable state without showing a credential path, raw identity, provider
   response, or internal gate string.
4. Exercise reserved, stale, unverified, cooled-down, quarantined, leased, and
   corrupt protective-state fixtures. Confirm each is excluded exactly as it
   is from the CLI and presents its distinct action.
5. For a ready fixture, use Copy command. Confirm the clipboard contains only
   the engine-generated, safely quoted frozen-launcher invocation for the
   selected account and private Headroom directory.
6. Use Open in Terminal with Terminal, iTerm, and Warp when installed. Confirm
   only the configured allowlisted application opens, the temporary launcher
   is private and self-deleting, and the controlled provider fixture receives
   no user-supplied arguments.
7. Change capacity or acquire the account lease after preview but before
   launch. Confirm both Copy/Open request a fresh intent and the final launcher
   refuses if the selected account is no longer eligible. It must not switch
   to another account.
8. Confirm the packaged app and frozen engine open no TCP listener. Quit the
   app and close fixture terminals; no Headroom process or temporary launcher
   may remain.

## Security invariants

- JavaScript sends only `{ family }` for preview and
  `{ family, accountName }` for copy/open.
- Rust does not expose the private launch intent to the webview.
- Only a bundled `headroom-engine` executable and the exact private
  `--launch-provider <family> <account>` shape pass native validation.
- The environment is exactly `HEADROOM_DIR` plus `HEADROOM_SLOT_LEASE=1`.
- The provider executable is verified by the engine and is never accepted from
  JavaScript.
- Terminal selection comes from validated settings and is restricted to
  Terminal, iTerm, or Warp.
- Copy output is recomputed from the validated intent; Open uses a private,
  app-owned, self-deleting launcher script.
- The frozen launcher repeats all routing gates and atomically acquires the
  account lease before replacing itself with the provider CLI.

## Result record

Record the implementation commit, exact app and engine hashes, architecture,
macOS version, test totals, scenario results, installed-terminal coverage,
absence of TCP listeners, and cleanup state.

Status: complete for implementation commit
`c80aac96477a589dd840c546642397ed4ad8a6da`.

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| 2026-07-16T15:19:00Z | `c80aac9` | automated routing contracts | 100 focused Python, 21 frontend, and 24 Rust tests passed | pass |
| 2026-07-16T15:19:00Z | `c80aac9` | exact frozen arm64 package | `routing_launch` advertised; ad-hoc bundle passed strict deep signature verification on macOS 26.5.2 | pass |
| 2026-07-16T15:19:00Z | `c80aac9` | packaged Midnight routing preview | black/phosphor-green/glow console selected `ready-slot` and explained `reserved-slot` with the frozen engine's bounded semantics | pass |
| 2026-07-16T15:19:00Z | `c80aac9` | quoted copy and Apple Terminal launch | clipboard matched the engine intent; the controlled provider received zero arguments, the selected home, and leasing enabled; private script self-deleted | pass |
| 2026-07-16T15:19:00Z | `c80aac9` | stale decision and lease-race refusal | a foreign live lease after preview made Copy refuse with `close_other_session`; clipboard sentinel remained unchanged; frozen launcher exited 2 | pass |
| 2026-07-16T15:19:00Z | `c80aac9` | TCP/process/script cleanup | app and engine had zero TCP listeners; normal quit left no Headroom process or launch-intent script | pass |

The exact artifact was
`integrations/menubar/src-tauri/target/release/bundle/macos/Headroom.app`.
After the local QA signature, the app executable SHA-256 was
`1f02dadd6c4d822f2935e98e977de344cfce42fb374e215d989fbee75407e88e`
and the frozen engine SHA-256 was
`60037737620f00cc487f5e5e368d5b6b9ae8e6be4a8e76558bd2583e33504d55`.
The signature is ad hoc with no Team ID; Developer ID signing and notarization
remain release work.

Apple Terminal was installed and passed the full native Open action. iTerm and
Warp were not installed on this QA Mac, so their exact-package application-open
branches were not exercised; the shared allowlist and command contract are
covered by Rust tests.
