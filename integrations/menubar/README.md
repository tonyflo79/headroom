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

When a held adopted or macOS Keychain-backed slot requires provider
authentication, its account details expose an explicit provider-login action.
The webview sends only the account name after a foreground confirmation. The
engine verifies that the slot is still held for authentication, checks that no
live lease owns it, and generates a one-use launcher for the configured
terminal. A fresh frozen engine re-proves those conditions before executing
the provider's own login command with the registered account home. Provider
homes, executable paths, credentials, and shell text never cross the webview;
Keychain-backed sign-in is clearly labeled as provider-managed and not
rollback-safe. Healthy, offline, leased, or rollback-safe managed slots do not
offer this external action.

The dashboard's deliberate visual language is a black terminal canvas with
phosphor-green monospace text and glowing capacity bars. Limited and uncertain
states retain distinct red and amber treatments for accessibility. Appearance
also offers Minimal, Chrome, Paper, and Terminal live previews; all five themes
use the same semantic state model and never rely on color alone. Midnight is
the default terminal treatment.

The native Settings console owns title, redaction, routing reserve,
collection interval, provider executable overrides, preferred
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

The compact account cards show the exact weekly reset alongside incremental
24-hour, 7-day, and 30-day token activity. Codex activity comes from its local
telemetry database; Claude activity is read only from the registered
Headroom-owned account home, so shared transcripts are never guessed into an
account. The totals strip aggregates tokens and distinct sessions. Coverage is
explicit: normal values are complete, `≥` is partial history, `…` is tracking
from now, and `—` is unavailable. Commits and pull requests remain unavailable
until a repository scope and attributable source are defined. Raw transcripts,
paths, session IDs, thread IDs, and provider payloads never cross the bridge.
Automatic handoff is disabled by default and has no desktop control or health
panel; account changes remain manual.

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
  `headroom_desktop_bridge@1` schema, frozen runtime, required capabilities,
  and the exact redacted `headroom_compatibility@1` matrix. The matrix binds
  product and engine versions, bridge/state schema ranges, platform,
  architecture, and capabilities before the first live view is trusted.
- Startup and restart handshakes run on the blocking pool; the native UI stays
  responsive and fail-closed while the engine starts.
- Engine stdout is protocol-only. Imported or child-process output is diverted
  to stderr and Rust never logs its contents.
- Rust owns the current sanitized view, a monotonic revision, and the live
  theme. Both webviews receive the same immutable snapshot envelope; stale or
  duplicate revisions and stale command responses are ignored.
- The bridge exposes only narrow onboarding, account, refresh, login-job,
  validated-settings, routing-preview, app-owned launch-intent, and bounded
  activity projections.
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
- External provider recovery may send only an engine-authorized account name.
  Rust accepts an exact three-argument frozen-engine launcher and a single
  `HEADROOM_DIR` value; provider homes, executables, arbitrary arguments, and
  extra environment variables are rejected. The recovery launcher never
  acquires a routing lease and refuses while an existing lease is active.
- Activity projection crosses only the exact `headroom_activity@1` schema with
  bounded numeric counts and coverage states. Its incremental cursors and
  hashed session membership stay in a private `0600` state file, and one
  refresh has a fixed Claude transcript-read budget.
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
uv run --python 3.13.12 python -m unittest tests.test_compatibility
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

For the engine/bridge/state compatibility matrix, migration safety,
concurrent CLI coexistence, and downgrade refusal, see
`docs/desktop/COMPATIBILITY-VALIDATION.md`.

For engine-authoritative automatic-handoff states, next-launch preference
behavior, strict sanitization, and packaged active-child acceptance, see
`docs/desktop/HANDOFF-HEALTH-VALIDATION.md`.

## Current limitations

- The app is macOS-only and is built for the runner's native architecture.
- Real Claude and Codex flows still require the human validation checklists in
  `docs/desktop/CLAUDE-LOGIN-VALIDATION.md` and
  `docs/desktop/CODEX-LOGIN-VALIDATION.md` before those slices can ship.
- Claude access-token expiry is repaired through Claude Code's own per-slot
  credential manager; rejected/revoked refresh credentials still require the
  explicit human reauthentication workflow.
- There is no updater, diagnostics export, signing, or notarization yet.
- Native capacity notifications are durable, deduplicated, verified-reading
  only, and off by default. macOS permission is requested only after opt-in.
- The old loopback viewer helpers remain in Rust for their security tests but
  are not called by the desktop application path.
