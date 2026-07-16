# Desktop onboarding validation

This checklist is the installed-app evidence gate for desktop issue #6. Use an
isolated `HEADROOM_DIR` or a clean macOS user. Provider fixtures must never
contain real credentials, and evidence must not include device codes, tokens,
provider payloads, unredacted identities, or credential paths.

## Disclosure and demo

1. Launch the packaged app with an empty state root and provider executables
   that record every invocation.
2. Confirm the welcome page explains local storage, provider reads, provider
   credential ownership, optional routing, and the sanitized dashboard before
   either executable is invoked.
3. Choose **Explore demo** and confirm Claude and Codex sample accounts render
   as current with glowing capacity bars.
4. Confirm demo created no registry, provider home, or provider invocation and
   opened no listener or provider network flow.
5. Quit and relaunch. Confirm demo resumes and remains explicitly labelled as
   sample data.

## Provider and account paths

Repeat provider readiness with fixtures for Claude-only, Codex-only, both, and
neither. Every path must remain usable: ready providers offer a new login,
missing or old providers show an actionable prerequisite, existing logins offer
adoption, and demo remains available.

Generated slot names must satisfy `[a-z0-9][a-z0-9_-]{0,31}`. Invalid and
duplicate names must show inline errors and must not cross the bridge.

## Resume and completion

1. Advance to account selection, quit, and inspect
   `state/desktop-onboarding.json`. It may contain only schema, step, and update
   time and must have mode `0600`.
2. Relaunch and confirm account selection resumes without starting a login.
3. Start each fixture login, quit during authorization and live verification,
   and confirm rollback completes within the app shutdown budget.
4. Complete an adoption or login and confirm setup exits to the full dashboard
   with a current reading or an honestly held/stale snapshot when collection is
   unavailable.
5. Relaunch and confirm the registered account remains in the dashboard without
   onboarding or a repeated login attempt.

## Evidence record

Status: automated contract tests and isolated packaged-app smoke run complete;
live-account completion remains a human release gate

| UTC time | Build commit | Path | Resume | No provider access before consent | Result |
|---|---|---|---|---|---|
| 2026-07-16T10:45:47Z | `2ffbe6f` | clean/demo | yes | yes | pass — sample fleet resumed; no registry, provider invocation, or listener |
| 2026-07-16T10:45:47Z | `2ffbe6f` | Claude-only fixture | automated | yes | pass — readiness matrix contract |
| 2026-07-16T10:45:47Z | `2ffbe6f` | Codex-only fixture | automated | yes | pass — readiness matrix contract |
| 2026-07-16T10:45:47Z | `2ffbe6f` | both/neither | yes | yes | pass — packaged UI showed both ready and both missing; demo remained available |

The packaged run also confirmed valid generated names, inline rejection of
`Bad Name`, account-step resume without a login attempt, terminal-green text
and glowing controls/bars on black, and a clean application shutdown.
