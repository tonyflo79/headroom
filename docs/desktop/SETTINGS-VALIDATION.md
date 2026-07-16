# Desktop settings validation

This checklist records acceptance for Desktop 10: authoritative settings,
five synchronized themes, safe window restoration, locale formatting, native
shortcuts, and reversible macOS launch at login.

## Automated gates

From the repository root:

```sh
uv run --python 3.13.12 python -m unittest -q \
  tests.test_desktop_bridge tests.test_desktop_login tests.test_codex_desktop_login
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

The bridge tests must prove safe defaults, private atomic commits, rejection
without mutation, executable provider-path enforcement, and the
`validated_settings` handshake capability. Frontend tests must prove all five
themes share the semantic token contract, untrusted settings normalize safely,
the complete form patch is typed, and locale formatters own percentages and
reset times. Rust tests must prove dynamic refresh bounds, persisted theme
propagation, window geometry validation, and background login-launch routing.

## Packaged settings console

1. Launch the exact packaged `Headroom.app` against an isolated `HEADROOM_DIR`
   containing one non-secret fixture account.
2. Open Settings with the dashboard button, tray menu, popover, and
   `Command-,`. Each route must focus the same native window and settings form.
3. Confirm Midnight opens as a black terminal canvas with phosphor-green text
   and glowing capacity bars. Select Terminal, Minimal, Chrome, Paper, and
   Midnight; each choice must update dashboard and popover together and remain
   selected after a full app restart.
4. Enter invalid title, reserve, refresh, provider path, terminal, and
   notification thresholds. Save must remain unavailable for locally detectable
   errors. A missing/non-executable absolute provider path must be refused by
   the authoritative bridge with no registry mutation.
5. Save a valid title, redaction, reserve, handoff, 60–3600 second refresh,
   executable provider override, preferred terminal, window, and notification
   preference set. Confirm unrelated registry fields are unchanged and
   `config.json` remains mode `0600`.
6. Confirm notification and reset-notification preferences begin off. Saving
   them must not request macOS notification permission in this milestone.
7. Confirm reset dates/times follow the Mac locale, percentages use locale
   formatting, and `Command-R`, `Command-W`, and `Command-Q` refresh, hide, and
   quit respectively.

## Window state

1. With window memory enabled, move and resize the dashboard, quit normally,
   relaunch, and confirm the saved placement is restored.
2. Move the saved record completely outside every attached display, relaunch,
   and confirm it is ignored. At least 120 by 80 points must intersect an
   available display before a placement is accepted.
3. Disable window memory. Confirm the private saved record is removed, the
   dashboard centers once, and subsequent close/quit events do not recreate
   the record.
4. Re-enable window memory, reposition, quit, and confirm restoration resumes.

## macOS launch at login

1. Record the initial launch-at-login status. It must be off for a fresh app.
2. Enable it in Settings. Reopen Settings and confirm native status reports it
   enabled; inspect `~/Library/LaunchAgents` and confirm the generated item
   launches this exact packaged app with `--headroom-login-launch`.
3. Start the generated item with a ready fixture. Headroom must remain hidden
   in the menu bar, start exactly one frozen engine, and open no TCP listener.
4. Repeat with onboarding and recovery fixtures. The dashboard must appear
   because operator action is required.
5. Disable launch at login. Confirm status is off and the generated LaunchAgent
   is removed. Restore the initial state if it was enabled before testing.

## Result record

Record the packaged app path and hash, architecture, macOS version, test totals,
initial/final login-item status, window-state permissions, screenshots for the
five themes and invalid/valid settings states, and confirmation that the exact
package opened no TCP listener.

Status: complete for implementation commit `58b0b011785f6d05ce476ad95808a01d57d922e2`

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| 2026-07-16T14:43:13Z | `58b0b01` | automated settings contracts | 60 focused Python, 18 frontend, and 21 Rust tests passed | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | exact frozen arm64 package | `validated_settings` advertised; ad-hoc bundle passed strict deep signature verification on macOS 26.5.2 | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | packaged Midnight settings console | `Command-,` opened the black/phosphor-green/glow console; one app and one frozen engine ran with zero TCP listeners | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | authoritative valid settings commit | title, routing, 420-second refresh, executable path, iTerm, window, and notification preferences persisted at mode `0600` | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | rejected settings mutation | `invalid_setting_title`; before/after SHA-256 remained identical | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | all five durable themes | Midnight, Minimal, Chrome, Paper, and Terminal round-tripped through the exact frozen engine; Midnight restored | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | window memory disabled | private app-data directory remained mode `0700` and the saved window record was removed | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | launch at login enabled | native status showed enabled; `Headroom.plist` targeted the exact package with `--headroom-login-launch` | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | background login route | ready fixture stayed hidden with exactly one engine and zero TCP listeners; a normal secondary activation showed the dashboard | pass |
| 2026-07-16T14:43:13Z | `58b0b01` | launch at login disabled | native toggle removed `Headroom.plist`; final state matched the initially absent login item | pass |

The QA artifact was
`integrations/menubar/src-tauri/target/release/bundle/macos/Headroom.app`.
After the local QA signature, the app executable SHA-256 was
`d7d20bae8c627a763f4f6b80266088040895bdf0b78015fb9e5fb2a6e49e1cc0`
and the frozen engine SHA-256 was
`d25f1beab75b039d080f7e24d911d56f5ca4ceef4af36d742fe742800f36ffe4`.
The signature is ad hoc and has no Team ID; Developer ID signing and
notarization remain release work.

The exact packaged run began and ended with no Headroom LaunchAgent. Enabling
created a `RunAtLoad` LaunchAgent pointing only to this package and the stable
login-launch argument. Disabling removed it. The login argument kept a ready
fixture in the menu bar until an ordinary secondary launch activated the one
existing instance. Automated routing tests cover the onboarding and recovery
branches that intentionally require a visible operator window.
