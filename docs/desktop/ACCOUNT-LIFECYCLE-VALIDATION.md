# Desktop account lifecycle validation

This checklist is the installed-app evidence gate for desktop issue #7. Use
fixture provider homes and an isolated `HEADROOM_DIR`; never use personal
credentials for destructive-path validation.

## Policy and order

1. Confirm each card labels its home as Headroom-managed or provider-managed.
2. Reserve and unreserve each provider and confirm collection remains visible
   while routing excludes only the reserved slot.
3. Move accounts up and down and confirm the dashboard order and routing
   preference order match after refresh and relaunch.
4. Run a compatible CLI registry mutation concurrently and confirm both
   changes persist without a lost update.

## Rename and protective state

1. Seed current and held snapshots, cooldowns, quarantine, completed handoff
   history, and an unlocked historical lease file for one fixture slot.
2. Rename it and confirm the slot name changes everywhere that is live while
   its provider home path and credentials remain byte-identical.
3. Relaunch and confirm the renamed slot can collect, route, and safely
   re-authenticate when its ownership policy permits.
4. Repeat with a live lease and an incomplete handoff. Both must refuse without
   changing any registry, snapshot, cooldown, quarantine, home, or credential.
5. Inject a crash after each journal publication point. Recovery must roll the
   complete mutation backward before commit and forward after commit; unknown
   concurrent state must open safe recovery instead of being overwritten.

## Re-authentication and removal

1. Re-authenticate a file-backed Headroom-managed Claude fixture and a
   Headroom-managed Codex fixture. Wrong, duplicate, cancelled, and rejected
   identity paths must restore the exact prior credentials.
2. Confirm adopted homes and Keychain-backed Claude homes show provider-owned
   guidance and cannot be overwritten by the desktop flow.
3. Type the exact slot name to remove a non-final account. Confirm its registry,
   snapshot, cooldown, and quarantine references disappear while its provider
   home and credentials remain untouched.
4. Confirm an incorrect typed name and removal of the final account are both
   refused without mutation.

## Evidence record

Status: automated contract tests and isolated packaged-app run complete

| UTC time | Build commit | Scenario | State preserved | Provider home preserved | Result |
|---|---|---|---|---|---|
| 2026-07-16 11:29:05Z | `6cad5b0` | reserve/reorder/concurrent CLI | yes | yes | pass |
| 2026-07-16 11:29:05Z | `6cad5b0` | rename/recovery/active protections | yes | yes | pass |
| 2026-07-16 11:29:05Z | `6cad5b0` | Claude/Codex re-authentication | yes | yes | pass |
| 2026-07-16 11:29:05Z | `6cad5b0` | confirmed/final-account removal | yes | yes | pass |

The exact implementation commit was rebuilt as `Headroom.app`, locally sealed
with an ad-hoc signature, and passed strict deep code-signature verification.
Production Developer ID signing, notarization, and Gatekeeper verification stay
owned by desktop issue #19. The packaged run used two isolated fixture homes and
confirmed terminal-green lifecycle controls through the native Tauri commands,
restart persistence, zero TCP listeners, exact-confirmation removal, and live
Claude and Codex fixture re-authentication with quarantine clearing. Automated
failure-path tests cover wrong or duplicate identity, cancellation, provider
rejection, exact credential rollback, crash recovery, live lease refusal, and
incomplete handoff refusal.
