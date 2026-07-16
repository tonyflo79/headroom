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

Status: automated contract tests complete; packaged-app run pending

| UTC time | Build commit | Scenario | State preserved | Provider home preserved | Result |
|---|---|---|---|---|---|
| pending | pending | reserve/reorder/concurrent CLI | pending | yes | pending |
| pending | pending | rename/recovery/active protections | pending | pending | pending |
| pending | pending | Claude/Codex re-authentication | pending | pending | pending |
| pending | pending | confirmed/final-account removal | pending | pending | pending |
