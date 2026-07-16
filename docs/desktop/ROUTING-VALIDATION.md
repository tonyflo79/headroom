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

Status: implementation complete; exact-package evidence pending.

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| pending | pending | automated routing contracts | pending | pending |
| pending | pending | exact frozen package | pending | pending |
| pending | pending | packaged routing preview | pending | pending |
| pending | pending | quoted copy and controlled launch | pending | pending |
| pending | pending | stale decision and lease-race refusal | pending | pending |
| pending | pending | TCP/process/script cleanup | pending | pending |
