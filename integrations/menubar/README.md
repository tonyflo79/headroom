# Headroom desktop application

This directory contains Headroom's self-contained Tauri desktop application.
The current implementation starts an architecture-specific bundled engine,
discovers existing Headroom and provider logins without changing them, renders
sanitized live account state, and can adopt one verified existing login into a
named slot. It does not require `headroom serve`, a browser, a localhost URL,
or a system Python installation.

On a clean first launch, the app presents its local-data, provider-read, and
credential-ownership disclosures before probing either provider. Continuing
starts a resumable provider-readiness and account journey; choosing demo mode
renders fresh bundled sample data without a provider CLI, account, credential,
or network read. The only persisted onboarding data is a private schema and
step marker.

The app can also start fresh Claude and Codex logins in Headroom-owned slots.
Both run without a controlling Terminal, require verified current provider
CLIs, publish stable progress/diagnostic codes, support cancel, and roll back
credentials on every failed terminal state. Codex uses the CLI's structured
device-auth app-server protocol and is not published until a live,
identity-bound subscription-capacity read succeeds. Provider output never
crosses the desktop bridge.

Connected accounts expose transactional reserve, reorder, rename,
re-authenticate, and removal controls. The app labels Headroom-managed versus
adopted provider homes, keeps every provider home and credential on rename or
removal, requires typed confirmation before removal, and refuses the final
account. Rename/removal carry snapshots, cooldowns, and quarantine state
through a private recoverable intent journal; active leases or incomplete
handoffs refuse the mutation.

The dashboard's deliberate visual language is a black terminal canvas with
phosphor-green monospace text and glowing capacity bars. Limited and uncertain
states retain distinct red and amber treatments for accessibility. Appearance
also offers Minimal, Chrome, Paper, and Terminal live previews; all five themes
use the same semantic state model and never rely on color alone. Midnight is
the default terminal treatment.

The native Settings console owns title, redaction, routing reserve, automatic
handoff, collection interval, provider executable overrides, preferred
terminal, window memory, and opt-in notification preferences. The frozen
engine validates every partial update under the registry lock and commits it
atomically; the webview has no file access. Provider overrides must resolve to
executable absolute paths. Settings use local number/date formatting and the
standard macOS `Command-,`, `Command-R`, `Command-W`, and `Command-Q`
shortcuts.

The resizable dashboard and the 420-by-680 menu-bar popover are projections of
one revisioned snapshot owned by Rust. They keep account order, capacity,
freshness, trust, routing, and theme changes synchronized without polling one
another. The popover scrolls for large fleets and provides Refresh, Dashboard,
Settings, and Quit actions. Closing the dashboard keeps the tray and bundled
engine running; Quit ends both. A private, schema-validated window record
restores the dashboard only when its size and position remain safely visible.
Window memory can be disabled, which deletes the saved record and centers the
window. Launch at login is off by default and uses a reversible macOS
LaunchAgent; login launches remain in the menu bar unless onboarding or safe
recovery requires operator attention.

One Rust-owned, single-flight schedule now serves stale activation, manual,
wake, and bounded background refreshes. Provider accounts collect concurrently
in registry order, so a slow or hung provider cannot suppress a responsive
one. Transient failures use stable diagnostics and capped exponential backoff
with jitter; only an age-bounded, identity-matched verified reading may remain
visible, explicitly stale and never routable. The app restarts a failed frozen
engine under a separate bounded policy and enters a visible degraded state
after repeated failures instead of looping indefinitely.

Headroom is also a process-level singleton. A synchronous, owner-checked claim
binds its private activation channel before Tauri setup, so simultaneous
launches cannot cross a listener-startup race: every secondary process
activates the existing dashboard and exits before it can start a frozen engine.
The first window renders immediately from a fail-closed recovery projection
while the handshake runs off the UI thread. A two-second sidecar watchdog and
every bridge command feed one rolling five-minute crash policy with capped ±20
percent jitter. Three crashes enter `DEGRADED`; Retry engine permits one
explicit attempt, and only five stable minutes clear the rolling history.

The dashboard includes an engine-authoritative routing console. It shows the
selected provider account and a stable explanation for every skipped account,
using the same reservation, freshness, identity, capacity, cooldown,
quarantine, and live-lease gates as the CLI router. Copy command and Open in
Terminal are available only for that selected verified account. Both actions
ask the engine to prove the decision again; the eventual frozen launcher
re-proves it a final time, acquires the account lease, and refuses instead of
silently switching slots if anything changed. The terminal-style black,
phosphor-green, and glowing-bar treatment remains the default Midnight theme.

The dashboard and menu-bar popover also show an engine-authoritative
automatic-handoff health console. It distinguishes configured, unavailable,
downgraded, armed, supervision-lost, and loop-guard states using bounded
sanitized supervisor events; process identifiers, supervisor identifiers,
paths, raw reasons, and provider output never cross the bridge. The setting is
explicitly next-launch-only. Saving it cannot signal, kill, recover, or alter
an already running provider child.

This is still an implementation build, not a production release. Complete
account management, signing, notarization, updates, and release
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
- Before sidecar setup, the app takes a non-blocking owner-checked file lock and
  synchronously binds a `0600` Unix-domain activation channel. Secondaries wait
  only for that bounded startup claim, activate the primary, and exit. A new
  owner removes a safely owned stale endpoint after force-quit; normal exit
  removes the live endpoint and releases the kernel lock.
- Startup has a 12-second bound and requires the exact
  `headroom_desktop_bridge@1` schema, frozen runtime, and required capability.
- Startup and restart handshakes run on the blocking pool; the native UI stays
  responsive and fail-closed while the engine starts.
- Engine stdout is protocol-only. Imported or child-process output is diverted
  to stderr and Rust never logs its contents.
- Rust owns the current sanitized view, a monotonic revision, and the live
  theme. Both webviews receive the same immutable snapshot envelope; stale or
  duplicate revisions and stale command responses are ignored.
- The bridge exposes only narrow onboarding, account, refresh, login-job,
  validated-settings, routing-preview, app-owned launch-intent, and passive
  automatic-handoff health projections.
  Calls are serialized, bounded, and a timed-out or malformed session is
  retired so a late frame cannot be mistaken for a later response.
- Bootstrap requires the `resilient_collection` capability. Rust owns one
  refresh flight, a five-minute healthy interval, capped jittered retry, wake
  recovery, and the bounded sidecar-restart/degraded policy.
- Only `headroom_desktop_view@1`, derived from the existing fail-closed widget
  projection, crosses the bridge. Identity is always email-redacted; credential
  paths, fingerprints, raw credentials, and provider payloads never cross it.
- The webview can navigate only to its embedded app document or `about:blank`.
- The page receives only the narrow desktop commands registered by Rust. It
  has no shell capability, arbitrary sidecar access, or filesystem capability.
  Its sole external-open command accepts only the exact
  `https://auth.openai.com/codex/device` URL.
- The routing webview may send only a known model family and selected account
  name. It cannot send a command, executable, environment variable, terminal,
  or path. Rust accepts only the frozen engine's exact launcher schema and one
  of Terminal, iTerm, or Warp; copied commands are recomputed from the same
  validated intent.
- Automatic-handoff health is projected from the same CLI capability contract
  and supervisor notification transitions used by terminal launches. The
  webview receives only the exact `headroom_handoff_health@1` schema and has no
  process, transcript, hook, lease, ledger, or recovery command.
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
uv run --python 3.13.12 python -m unittest tests.test_resilient_collection
uv run --python 3.13.12 python -m unittest tests.test_desktop_login
uv run --python 3.13.12 python -m unittest tests.test_codex_desktop_login
uv run --python 3.13.12 python -m unittest tests.test_account_lifecycle
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

For the dashboard/popover acceptance record, including synchronized refresh,
large-fleet scrolling, close-to-tray, theme propagation, and window restore,
see `docs/desktop/SURFACE-SYNC-VALIDATION.md`.

For offline, throttled, slow-provider, wake, and frozen-engine recovery
acceptance, see `docs/desktop/COLLECTION-RESILIENCE-VALIDATION.md`.

For singleton activation, startup, watchdog, crash-loop, manual retry,
force-quit recovery, and orphan cleanup acceptance, see
`docs/desktop/LIFECYCLE-VALIDATION.md`.

For validated preferences, live theme propagation, window state, shortcuts,
locale formatting, and reversible launch-at-login acceptance, see
`docs/desktop/SETTINGS-VALIDATION.md`.

For engine/CLI routing parity, safely quoted copy output, allowlisted terminal
launch, final selection re-proof, and lease-race acceptance, see
`docs/desktop/ROUTING-VALIDATION.md`.

For engine-authoritative automatic-handoff states, next-launch preference
behavior, strict sanitization, and packaged active-child acceptance, see
`docs/desktop/HANDOFF-HEALTH-VALIDATION.md`.

## Current limitations

- The app is macOS-only and is built for the runner's native architecture.
- Real Claude and Codex flows still require the human validation checklists in
  `docs/desktop/CLAUDE-LOGIN-VALIDATION.md` and
  `docs/desktop/CODEX-LOGIN-VALIDATION.md` before those slices can ship.
- Recovery is currently a safe read-only state, not a repair workflow.
- There is no updater, diagnostics export, signing, or notarization yet.
- Notification preferences are durable and off by default, but native alert
  delivery and the macOS permission prompt belong to the notification slice.
- The old loopback viewer helpers remain in Rust for their security tests but
  are not called by the desktop application path.
