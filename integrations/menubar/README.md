# Headroom desktop application

This directory contains Headroom's self-contained Tauri desktop application.
The first desktop tracer is intentionally narrow: an unsigned macOS app starts
an architecture-specific bundled engine and renders a deterministic, sanitized
two-account snapshot in a normal native window. It does not require
`headroom serve`, a browser, a localhost URL, or a system Python installation.

This is an implementation tracer, not a production release. Account setup,
live collection, menu-bar behavior, signing, notarization, updates, and release
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
- Only the existing sanitized `headroom_widget@1` projection crosses the
  bridge. Raw credentials and provider payloads do not.
- The webview can navigate only to its embedded app document or `about:blank`.
- The tracer opens no HTTP listener and grants the page no Tauri command API.

## Supported development target

- macOS 13 or newer
- Apple Silicon or Intel, built natively on each architecture
- Rust 1.88
- Python 3.13, provisioned by `uv` and frozen into the sidecar
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

# Install the pinned packager once if needed.
cargo install tauri-cli --version 2.11.4 --locked

# Build an unsigned development app.
cd integrations/menubar
cargo tauri build --bundles app

# Launch it.
open src-tauri/target/release/bundle/macos/Headroom.app
```

The app is written to
`src-tauri/target/release/bundle/macos/Headroom.app`. Because this tracer is
unsigned and unnotarized, it is for local development only.

For a faster edit-run loop, build the sidecar first and run:

```sh
cd integrations/menubar
cargo tauri dev
```

## Verification

From the repository root:

```sh
uv run --python 3.13 python -m unittest tests.test_desktop_bridge
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

- The dashboard uses deterministic fixture accounts, not installed accounts.
- The app is macOS-only and is built for the runner's native architecture.
- There is no menu-bar popover, account management, launch-at-login, updater,
  recovery UI, diagnostics export, signing, or notarization yet.
- The old loopback popover helpers remain in Rust for their security tests but
  are not called by the desktop tracer.
