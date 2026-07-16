# Desktop core-function release gate

This document is the evidence index for issue #37. It keeps account accuracy,
routing, launch isolation, and automatic handoff ahead of notifications and
other release polish. A checked automated row is not a substitute for the
pending real-account acceptance pass.

## Acceptance matrix

| Requirement | Current evidence | State |
|---|---|---|
| Expired Claude usage tokens are authentication failures | `tests.test_desktop_bridge.DesktopBridgeUnit.test_routing_preview_treats_expired_usage_token_as_authentication` exercises the full desktop preview seam and requires `authentication_required` / `reauthenticate_account`. | automated pass |
| Live Codex capacity matches the provider payload | The target-Mac app-server observation reported 10% weekly used and 0% Spark weekly used; the desktop projected 90% and 100% remaining. The provider omitted the lifted 5-hour window and Headroom did not invent one. | live read pass |
| Every held account receives a truthful recovery path | `CORE-RECOVERY-VALIDATION.md` proves the bounded external provider-login intent for Keychain/provider-managed slots and preserves managed reauthentication for rollback-safe Headroom-owned credentials. | packaged contract pass; human sign-in pending |
| All real accounts are current, limited, held, or reserved after recovery | Current Claude slots are truthfully held for expired usage tokens. They cannot become current until the account owner completes provider sign-in. | pending human sign-in and refresh |
| Desktop and CLI routing decisions are identical | `test_desktop_and_cli_route_the_same_snapshot_for_claude_and_codex` runs the real candidate engine and CLI picker against the same snapshot for Claude, Opus, Sonnet, Haiku, and Codex. It compares the selected account, complete candidate order, eligibility decisions, reserved/cap exclusions, and Codex greatest-headroom behavior with an omitted 5-hour window. | automated pass |
| Packaged Open in Terminal launches exactly the selected account under a lease | `ROUTING-VALIDATION.md` records the exact frozen-app launch, selected home, enabled lease, zero provider arguments, stale-decision refusal, lost-lease refusal without repicking, private self-deleting script, and zero TCP listeners. | packaged fixture pass; real-account pass pending |
| Automatic handoff remains engine-owned and fail-closed | The supervisor and V2 lease suites cover authenticated cap proof, target eligibility, transcript and identity guards, target lease acquisition, active-account lease reconciliation, loop guard, and source recovery. Desktop exposes only configuration and sanitized health. | automated pass; real-account pass pending |
| Human-controlled refresh, selection, launch, and switch pass | Must be performed only after the provider-owned login completes. No automation may submit credentials or dismiss provider consent. | pending human interaction |
| Desktop 6–12 are integrated before core fixes | PR #38 is green and is the required first merge. PRs #39 and #40 remain stacked behind it. | pending merge |

## Current focused gate

Run from the repository root with the supported bundled Python:

```sh
uv run --python 3.13.12 python -m unittest -v \
  tests.test_desktop_bridge \
  tests.test_v2_supervision.SlotLease \
  tests.test_v2_supervision.LeaseFollowsActiveAccount \
  tests.test_headroom.BlockReasonFailClosed \
  tests.test_headroom.CodexBlockReasonFailClosed \
  tests.test_headroom.GreatestHeadroom \
  tests.test_headroom.ReservedAccounts
```

On 2026-07-16 this gate passed 116 tests: 47 desktop bridge and protocol tests
plus 69 route, lease, and fail-closed tests. PR CI remains authoritative for
the complete 666-test Linux engine suite and the unsigned macOS application
build.

## Non-negotiable hold

Notifications remain paused and are not part of this integration stack. The
core gate remains open until PRs #38–#40 are integrated and the account owner
has completed the real-provider recovery, refresh, selected launch, and
automatic-switch acceptance pass.
