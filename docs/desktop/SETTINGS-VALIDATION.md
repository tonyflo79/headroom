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
