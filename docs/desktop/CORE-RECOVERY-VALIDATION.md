# Core account recovery validation

This checklist records the provider-recovery slice of the mandatory core gate
in issue #37. Its purpose is narrow: a held account that genuinely requires
provider authentication must offer a usable recovery path, while every other
state must remain fail-closed and must not expose credential homes or arbitrary
shell access to the desktop frontend.

It does not by itself prove multi-account switching. Real-provider refresh,
route parity, selected-account launch, and automatic-handoff smoke acceptance
remain required after the affected provider logins are recovered.

## Contract

- The engine alone decides whether a projected account may expose
  `external_reauthentication`.
- The action is limited to `held` accounts whose stable reason maps to
  `reauthenticate_account` and whose lifecycle policy is `keychain_manual` or
  `provider_managed`.
- Rollback-safe Headroom-owned file credentials continue to use the existing
  managed in-app login job and never receive the external action.
- Healthy, stale/offline, reserved-only, missing-provider, and leased accounts
  cannot produce an external login intent.
- The webview sends only `{ accountName }` after a foreground confirmation.
- The bridge response contains only provider, account label, recovery kind,
  preferred terminal, frozen-engine launcher, and `HEADROOM_DIR`. It contains
  no provider home, provider executable, raw command, credential, or extra
  environment value.
- Rust accepts only an executable named `headroom-engine*` with the exact
  `--launch-reauthentication <account>` argument shape.
- The temporary terminal script is private, create-new, executable, synced,
  and self-deleting. Recovery does not set `HEADROOM_SLOT_LEASE`.
- The newly started frozen engine repeats account-state, lifecycle-policy,
  provider-executable, and live-lease checks before replacing itself with the
  provider's own login command.
- Provider login receives only the registered `CLAUDE_CONFIG_DIR` or
  `CODEX_HOME` through the engine's scrubbed environment.

## Automated gates

```sh
python3 -m unittest \
  tests.test_desktop_bridge \
  tests.test_account_lifecycle \
  tests.test_desktop_login \
  tests.test_codex_desktop_login
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
scripts/build-desktop-sidecar.sh
cd integrations/menubar && \
  npx --yes @tauri-apps/cli@2.11.4 build --bundles app
```

The tests cover engine authorization, healthy/managed refusal, exact provider
login argv and account-home environment, frontend normalization and warning
copy, unknown-field rejection, bundled-launcher validation, extra-environment
rejection, provider/recovery-kind agreement, and absence of routing-lease
material.

## Packaged bridge gate

Run the exact frozen engine inside the packaged app against a configured held
slot and request `external_reauthentication_intent`. Confirm:

1. The handshake advertises `provider_reauthentication_launch`.
2. The intent uses `headroom_provider_reauthentication_intent@1`.
3. The launcher points to the engine inside the same packaged app.
4. The response contains no provider home or executable.
5. No provider login is launched during this non-mutating contract check.

Do not automate the final provider sign-in with personal credentials. That is
a foreground human-controlled smoke step. After sign-in, refresh Headroom and
record provider parity, route selection, launch isolation, and switching in
the broader issue #37 result.

## Result record

Status: implementation and exact-package bridge complete for commit
`d8b46f43e53ed5bad896f58f86d4ca70d624501e`; human-controlled provider sign-in
and post-login switching acceptance remain pending.

| Local time | Build | Scenario | Result | Status |
|---|---|---|---|---|
| 2026-07-16 | `d8b46f4` | engine/lifecycle recovery contracts | 84 tests passed | pass |
| 2026-07-16 | `d8b46f4` | frontend boundary | 24 tests passed | pass |
| 2026-07-16 | `d8b46f4` | native shell | 26 tests passed | pass |
| 2026-07-16 | `d8b46f4` | frozen arm64 handshake | new capability advertised from Python 3.13.12 sidecar | pass |
| 2026-07-16 | `d8b46f4` | exact packaged intent for configured `claude1` | returned only account label, provider, keychain policy, terminal, bundled launcher, and state root; provider login was not started | pass |
| 2026-07-16 | pending human | provider sign-in, refresh, route, launch, switching | requires explicit account-owner interaction | pending |

Exact unsigned package:
`integrations/menubar/src-tauri/target/release/bundle/macos/Headroom.app`
on macOS 26.5.2 (25F84), arm64.

- app executable SHA-256:
  `ef1260b6b7b43d7ad9f3ae38e63a156c2ac4cf449536543e0f906317e6b65904`
- frozen engine SHA-256:
  `6153b566189461389986948fba283f5a22306862e43ab2d4de3f285ea2a00ef8`

The package is unsigned development evidence only. Developer ID signing,
notarization, and release distribution remain later release gates.
