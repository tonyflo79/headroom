# Headroom desktop application

This directory contains Headroom's self-contained Tauri desktop application.
The current implementation starts an architecture-specific bundled engine,
discovers existing Headroom and provider logins without changing them, renders
sanitized live account state, and can adopt one verified existing login into a
named slot. It does not require `headroom serve`, a browser, a localhost URL,
or a system Python installation.

The app can also start a fresh Claude browser login in a Headroom-owned slot.
That flow runs without a controlling Terminal, requires a verified current
Claude CLI on macOS, publishes stable progress/diagnostic codes, supports
cancel, and rolls back file and per-slot Keychain credentials on every failed
terminal state. Provider output never crosses the desktop bridge.

The dashboard's deliberate visual language is a black terminal canvas with
phosphor-green monospace text and glowing capacity bars. Limited and uncertain
states retain distinct red and amber treatments for accessibility.

This is still an implementation build, not a production release. Fresh-login
setup, complete account management, signing, notarization, updates, and release
distribution are delivered by the follow-on desktop issues linked from
[the desktop PRD](https://github.com/tonyflo79/headroom/issues/1).

## Runtime boundary

```text
Headroom.app
  Tauri/Rust process
    stdin/stdout JSON-lines bridge
      bundled headroom-engine (PyInstaller, Python 3.13)
    embedded HTML/CSS/JavaScript dashboard
```

- Rust starts `headroom-engine` as a Tauri sidecar and owns its lifecycle.
- Startup has a 12-second bound and requires the exact
  `headroom_desktop_bridge@1` schema, frozen runtime, and required capability.
- Engine stdout is protocol-only. Imported or child-process output is diverted
  to stderr and Rust never logs its contents.
- The bridge exposes only narrow account, refresh, and login-job commands. Calls
  are serialized, bounded, and a timed-out or malformed session is retired so
  a late frame cannot be mistaken for a later response.
- Only `headroom_desktop_view@1`, derived from the existing fail-closed widget
  projection, crosses the bridge. Identity is always email-redacted; credential
  paths, fingerprints, raw credentials, and provider payloads never cross it.
- The webview can navigate only to its embedded app document or `about:blank`.
- The page receives only the three desktop commands registered by Rust. It has
  no shell capability, arbitrary sidecar access, or filesystem capability.
- The app opens no HTTP listener.

## Supported development target

- macOS 13 or newer
- Apple Silicon or Intel, built natively on each architecture
- Rust 1.88
- Python 3.13.12, provisioned by `uv` and frozen into the sidecar
- PyInstaller 6.21.0
- Tauri CLI 2.11.4

The bundled runtime pins are the supported engine matrix for this tracer. The
repository's broader Python test suite also runs on Linux in CI. Some inherited
macOS-only tests compare `/var` with its canonical `/private/var` path and are
not used as the desktop runtime gate.

## Build and run

From the repository root:

```sh
# Build and smoke-test the architecture-specific frozen engine.
scripts/build-desktop-sidecar.sh

# Build an unsigned development app.
cd integrations/menubar
npx --yes @tauri-apps/cli@2.11.4 build --bundles app

# Launch it.
open src-tauri/target/release/bundle/macos/Headroom.app
```

The app is written to
`src-tauri/target/release/bundle/macos/Headroom.app`. Because this tracer is
unsigned and unnotarized, it is for local development only.

For a faster edit-run loop, build the sidecar first and run:

```sh
cd integrations/menubar
npx --yes @tauri-apps/cli@2.11.4 dev
```

## Verification

From the repository root:

```sh
uv run --python 3.13.12 python -m unittest tests.test_desktop_bridge
uv run --python 3.13.12 python -m unittest tests.test_desktop_login
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

After launching the packaged app, verify it owns no listener:

```sh
APP_PID="$(pgrep -n -f 'Headroom.app/Contents/MacOS/headroom-menubar')"
lsof -nP -a -p "$APP_PID" -iTCP -sTCP:LISTEN
```

No output is expected. The bundled engine appears beneath the app in the
process tree as `Headroom.app/Contents/MacOS/headroom-engine`, never as a
system `python` process. Quitting Headroom must remove both processes.

## Current limitations

- The app is macOS-only and is built for the runner's native architecture.
- Fresh GUI login currently supports Claude only; Codex device authentication
  is the next provider slice.
- A real Claude flow still requires the human validation checklist in
  `docs/desktop/CLAUDE-LOGIN-VALIDATION.md` before this slice can ship.
- Recovery is currently a safe read-only state, not a repair workflow.
- There is no complete account-management UI, launch-at-login, updater,
  diagnostics export, signing, or notarization yet.
- The old loopback popover helpers remain in Rust for their security tests but
  are not called by the desktop tracer.
