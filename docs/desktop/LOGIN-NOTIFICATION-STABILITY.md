# Login and notification stability gate

This gate covers the final core-utility reliability slice: Claude account
cards must survive normal OAuth access-token expiry without a browser login,
and notifications must remain quiet, truthful, and opt-in.

## Credential finding

The four live macOS Claude slots are correctly isolated in namespaced Keychain
items. Each credential contains both an access token and a refresh token, but
the access token expires after roughly eight hours. The prior collector checked
`expiresAt` and held the card immediately; it never invoked Claude Code's token
repair. A browser login is a separate session and cannot extend this CLI token.

An isolated diagnostic proved the provider boundary: `claude auth status`
reported an expired credential as logged in, while `claude doctor` exercised
Claude Code's own refresh-token and Keychain maintenance path without sending a
model prompt. Headroom now uses that provider-owned maintenance command for the
exact slot `CLAUDE_CONFIG_DIR` when a token is missing, near expiry, expired, or
rejected once with HTTP 401. Headroom never posts or stores the refresh token.

After repair, the collector re-reads the Keychain item and binds the snapshot
to the rotated access-token digest. If provider repair fails, the slot remains
held and the existing human reauthentication path remains available.

## Notification contract

- Native Notification Center delivery only; no Headroom window is opened,
  focused, or overlaid.
- Disabled by default and evaluated only after the user enables it.
- Only `verified` / `verified_local` accounts with current or limited windows
  are eligible. Held, stale, unverified, missing, and malformed readings are
  silent.
- Global and provider-specific thresholds use the existing validated settings.
- New low windows for one account are coalesced into one notification.
- A durable mode-0600 ledger suppresses repeats across scheduled refreshes and
  application restarts.
- Reset alerts are optional and require the provider's reset timestamp to
  advance. Percentage corrections inside the same window stay silent.
- Turning notifications off clears prior crossings, preventing retroactive
  reset alerts when the user opts in again.

## Automated evidence

```sh
python3 -m unittest discover -s tests
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --lib \
  --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

Result on 2026-07-17: 683 Python tests, 29 frontend tests, and 30 native-shell
tests passed. The live token index reconciled its local-day row exactly to the
`today` total and the sum of attributable plus unattributed sources. Coverage
remains honestly labeled `partial` where older Codex logs expose no token
events; those gaps are not converted into invented zeros.

## Human acceptance

1. Leave Headroom running across an access-token expiry (or overnight).
2. Confirm each Claude card refreshes without opening a browser or changing
   its account identity.
3. Enable notifications in Settings and accept the macOS permission if asked.
4. Confirm one low-capacity system banner appears at a real threshold crossing,
   typing focus remains unchanged, and repeated refreshes stay silent.
5. If reset notifications are enabled, confirm one banner only after the
   provider advances the window reset timestamp.
