# Desktop singleton and sidecar lifecycle validation

This checklist is the installed-app evidence gate for desktop issue #10. Use
an isolated `HEADROOM_DIR`. Record only process counts, stable diagnostic codes,
revisions, and sanitized UI states—never provider output, credentials, private
paths, or unredacted identities.

## Singleton and activation

1. Launch the packaged application and record the app and bundled-engine
   process trees.
2. Launch the same bundle again while the first app is starting, while it is
   current, and while it is degraded. Each secondary process must exit before
   sidecar setup, activate the existing dashboard, and leave exactly one app
   process and one PyInstaller engine process tree.
3. Repeat simultaneous launches in a bounded stress loop. Confirm no duplicate
   dashboard, tray, collector, or engine survives.
4. Confirm normal Quit removes the singleton activation endpoint. Force-quit
   the app, confirm the sidecar exits when its bridge closes, then relaunch and
   confirm a refused stale endpoint is removed without user repair.

## Deterministic startup and command failure

1. Delay the frozen handshake and confirm the terminal-green native shell is
   visible and responsive in fail-closed `engine_starting` state before the
   handshake completes.
2. Exercise handshake timeout, pre-handshake exit, invalid handshake, and
   discovery failure fixtures. Confirm `engine_startup_timeout`,
   `engine_startup_exited`, `engine_incompatible`, or the applicable stable
   startup code is visible; raw stderr never crosses into the webview.
3. Kill the engine during a request. Confirm `engine_exited_mid_request` or
   `engine_communication_failed`, preserved last verified data, and one bounded
   restart flight.
4. Exercise the same exit during onboarding or login with no connected
   accounts. Confirm recovery does not depend on the collection scheduler.

## Watchdog, crash loop, and manual retry

1. Kill an idle sidecar without pressing Refresh. Within the two-second
   watchdog interval, confirm `RECOVERING` appears and a bounded restart begins.
2. Crash three engines inside five minutes, including engines that complete a
   handshake before failing. Confirm capped exponential retry with ±20 percent
   jitter and `DEGRADED` after the third crash.
3. Confirm `DEGRADED` shows the allowlisted diagnostic code, stops automatic
   restart, and changes Refresh to an explicit Retry engine action.
4. Select Retry engine once. Confirm the UI remains responsive; failure returns
   to `DEGRADED`, while success restores the frozen bridge and current view.
   Crash history clears only after five stable minutes.

## Shutdown and resource ownership

1. Quit from the dashboard shortcut and menu-bar action. Confirm the engine
   receives its shutdown frame, exits cleanly within six seconds, and is killed
   only as a bounded fallback.
2. Confirm logout/update-style app termination cannot leave an engine that
   accepts commands or a stale singleton endpoint that blocks relaunch.
3. Confirm the app owns no TCP listener before, during, or after recovery.
4. Confirm lifecycle work never blocks the main UI thread: the startup shell,
   window movement, theme controls, and Quit remain responsive while handshake
   and restart work runs on the blocking pool.

## Evidence record

Status: automated contract tests complete; exact packaged-app evidence pending

| UTC time | Build commit | Scenario | Result | Status |
|---|---|---|---|---|
| pending | pending | exact frozen and signed build | pending | pending |

The Midnight presentation remains a black canvas with phosphor-green text and
glowing bars. `RECOVERING` retains the green glow; `DEGRADED` uses amber plus
an explicit stable code and Retry engine label, so lifecycle status never
depends on color alone.
