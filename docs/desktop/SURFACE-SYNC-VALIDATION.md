# Desktop dashboard and popover validation

This checklist is the installed-app evidence gate for desktop issue #8. Use an
isolated `HEADROOM_DIR` and fixture provider homes. Evidence must contain only
sanitized account names and redacted identities—never credentials, provider
payloads, device codes, or private paths.

## Shared snapshot and large fleets

1. Launch the packaged app with current, limited, held, stale, reserved, and
   model-scoped fixture readings across at least twelve ordered accounts.
2. Open the menu-bar popover and confirm it scrolls, exposes every account in
   the same order as the dashboard, and preserves window and model scopes.
3. Refresh from the popover and confirm both surfaces advance to the same
   monotonic revision without clearing verified readings while providers run.
4. Confirm current, limited, held, stale, freshness, trust, routing, and
   diagnostic states remain understandable from text without color.

## Native actions and window behavior

1. Confirm Dashboard opens and focuses the main window and hides the popover.
2. Confirm Settings opens the dashboard's Appearance panel and hides the
   popover.
3. Close the main window and confirm the tray, popover, and bundled engine keep
   running. Reopen the dashboard from the popover.
4. Resize and move the dashboard, quit, and relaunch. Confirm a private,
   schema-validated placement restores only when it safely intersects a live
   monitor.
5. Quit from the popover and confirm the app and every bundled-engine process
   exit.

## Themes and local-only boundary

1. Confirm Midnight starts as the black terminal canvas with phosphor-green
   text and glowing capacity bars.
2. Switch themes live and confirm dashboard and popover update together while
   retaining identical semantic content. Automated coverage must exercise the
   Midnight, Minimal, Chrome, Paper, and Terminal token contracts.
3. Confirm neither surface opens a TCP listener, fetches a localhost document,
   nor accepts navigation outside its bundled app document or `about:blank`.

## Evidence record

Status: automated contract tests and isolated packaged-app run complete

| UTC time | Build commit | Scenario | Dashboard revision | Popover revision | Result |
|---|---|---|---:|---:|---|
| 2026-07-16T12:17:46Z | `01a02d3` | 12-account order and popover scroll | 1 | 1 | pass — identical ordered account list |
| 2026-07-16T12:17:46Z | `01a02d3` | refresh initiated in popover | 2 | 2 | pass — stale response rejected after shared publication |
| 2026-07-16T12:17:46Z | `01a02d3` | Midnight to Paper live preview | 2 | 2 | pass — both surfaces changed together |
| 2026-07-16T12:17:46Z | `01a02d3` | close, reopen, restore, and quit | n/a | n/a | pass — placement restored; all processes exited |

The exact implementation commit was rebuilt as `Headroom.app` with its frozen
arm64 sidecar, locally sealed with an ad-hoc signature, and passed strict deep
code-signature verification. The packaged run confirmed a 900-by-650 resizable
dashboard, a 420-by-680 scrolling popover, exact account ordering on both
surfaces, synchronized refresh revisions, Dashboard and Settings routing,
close-to-tray behavior, private `0700`/`0600` window state, safe geometry
restore, zero TCP listeners, and complete sidecar cleanup on Quit.

The packaged run visually checked the terminal-green Midnight design and the
light Paper projection. Frontend tests verify all five themes define the same
semantic token contract; Rust tests cover monotonic snapshots, exact theme
allowlisting, bundled-only navigation, unsafe placement rejection, and the
absence of hostname-based loopback trust.
