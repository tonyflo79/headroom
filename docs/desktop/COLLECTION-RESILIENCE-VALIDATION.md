# Desktop collection resilience validation

This checklist is the installed-app evidence gate for desktop issue #9. Use an
isolated `HEADROOM_DIR` and fixture provider homes. Evidence may contain only
sanitized account names, stable diagnostic codes, ages, and revisions—never
credentials, provider payloads, raw exception text, or private paths.

## Single-flight scheduling and provider isolation

1. Start with a stale snapshot and confirm activation begins one collection
   without a click. Register the webview callback after collection completes
   and confirm the Rust-owned snapshot reconciliation still displays the new
   revision.
2. Trigger Refresh repeatedly from both dashboard and popover while collection
   is running. Confirm only one engine request runs and both surfaces settle on
   the same monotonic revision.
3. Make one provider exceed the collection deadline while another responds.
   Confirm the responsive account publishes in registry order and the slow
   account is held as `provider_timeout`; the UI remains interactive.
4. Confirm a healthy collection waits five minutes before the background
   schedule runs again. Failed collection retries must be exponential, jittered
   by at most 20 percent, and capped at five minutes.

## Stable failures and safe carryover

1. Exercise 401/403, 429, 5xx, offline, timeout, malformed JSON, future clocks,
   and out-of-range percentages. Confirm every state is text-labelled and raw
   provider or exception text never crosses the desktop bridge.
2. For offline, timeout, throttling, and temporary server failure, confirm only
   a prior identity- and credential-matched reading within the observation age
   bound remains visible. It must show its age, be `stale`, and be non-routable.
3. Change the bound identity or age the reading beyond policy. Confirm no prior
   capacity is displayed.
4. Confirm authentication and malformed readings hold safely without a stale
   carryover, while transient failures enter bounded retry. A partially healthy
   fleet must publish its healthy rows and use `BACKOFF`, not global `OFFLINE`.

## Wake, connectivity, and engine recovery

1. Sleep and resume with a reading older than one minute. Confirm wake requests
   a collection; a reading younger than one minute must not cause needless
   provider work.
2. Restore connectivity during retry backoff and confirm the bounded scheduler
   recovers without relaunching the app. The timer naturally pauses while the
   machine sleeps and no reachability polling loop runs.
3. Kill the bundled engine, then request refresh. Confirm the app remains open,
   shows `RECOVERING`, starts a new bundled engine under exponential backoff,
   and publishes a later revision.
4. Repeatedly fail engine restart three times. Confirm the UI enters `DEGRADED`
   for a five-minute cooldown rather than restarting indefinitely.
5. Confirm the app owns no TCP listener. Quit and confirm the app and every
   bundled-engine process exit.

## Evidence record

Status: automated contract tests and exact packaged-app fault injection complete

| UTC time | Build commit | Scenario | Revision/result | Status |
|---|---|---|---|---|
| 2026-07-16T13:13:10Z | `c319e4e` | frozen bridge capability | `resilient_collection`, frozen arm64 runtime | pass |
| 2026-07-16T13:13:10Z | `c319e4e` | stale activation before callback registration | reconciled to current snapshot | pass |
| 2026-07-16T13:13:10Z | `c319e4e` | kill bundled engine, then reopen/refresh | same app PID; `RECOVERING` to `CURRENT` with new engine | pass |
| 2026-07-16T13:13:10Z | `c319e4e` | fail engine three consecutive times | `DEGRADED`; no engine restart loop | pass |
| 2026-07-16T13:13:10Z | `c319e4e` | listener and Quit cleanup | zero TCP listeners; app and engine exited | pass |

The exact implementation commit was rebuilt as a PyInstaller arm64 frozen
engine and bundled into `Headroom.app`. The app received a local ad-hoc
signature and passed strict deep code-signature verification. This is QA
sealing, not Developer ID signing or notarization.

The packaged run began from an aged fixture snapshot. Activation completed
before the JavaScript callback was available, and the explicit Rust-store
reconciliation still advanced the visible surface to the current revision. An
injected engine crash displayed `RECOVERING`, retained the same Headroom app
process, started a new frozen-engine process tree, produced a later private
snapshot, and returned to `CURRENT`. Three consecutive injected restart
failures then displayed `DEGRADED` and left no engine process running, proving
the five-minute cooldown stopped the loop. The app opened no TCP listener and
Command-Q removed both app and engine processes.

Automated evidence covers the provider matrix that is unsafe or impractical to
force against real accounts: stable 401/403, 429, 5xx, offline, timeout, and
malformed-response classification; identity- and age-bound carryover; changed
identity refusal; provider deadline isolation with ordered partial results;
capped ±20 percent jitter; wake freshness gating; partial-fleet backoff; exact
bridge capability gating; and monotonic shared-snapshot publication. The
focused gates passed 135 Python, 15 frontend, and 17 Rust tests. The full Python
suite passed 602 tests; its remaining 26 failures and 11 errors are the
documented pre-existing macOS `/var` versus `/private/var` canonicalization
cases in handoff/supervisor code, outside this desktop slice.

The terminal-green Midnight design remains the default: black canvas,
phosphor-green monospace text, and glowing capacity bars. `RECOVERING` retains
the green glow; `BACKOFF`, `OFFLINE`, and `DEGRADED` use amber text plus explicit
labels, so resilience state never depends on color alone.
