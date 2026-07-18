# Desktop diagnostics and support-report validation

This gate covers Desktop 14 / issue #15. The diagnostic surface is available
from the dashboard toolbar and the tray menu. It remains usable when the
bundled engine is degraded because the native shell supplies app, sidecar, and
update health and substitutes stable unavailable components when the engine
cannot answer.

## Contracts

- Engine health: `headroom_engine_health@1`
- Native health: `headroom_desktop_diagnostics@1`
- Code-only journal: `headroom_diagnostic_events@1`
- Saved report: `headroom_support_bundle@1`

Every component contains exactly `id`, `state`, `code`, and `remediation`.
Component IDs, states, codes, remediations, inventory entries, and field counts
are bounded and validated again at the Rust and JavaScript boundaries. Unknown
fields fail closed.

The native report covers app, sidecar, update, engine, bridge, registry,
snapshot, activity index, Claude CLI, and Codex CLI. The update component is
honestly `update_not_configured` until the signed updater slice ships.

## Journal bounds

The private journal stores timestamps and allowlisted event codes only. It
never stores error strings or provider output. It is pruned to all three
bounds before each write:

- seven days;
- 256 events;
- 128 KiB.

The journal directory is mode 0700 and the file is mode 0600 on Unix. Symlink,
non-regular, malformed, oversized, or unknown-code journals fail closed.

## Report inventory and redaction

The diagnostics panel shows the exact inventory before the save button is
enabled:

- `health.json`: bounded redacted component health;
- `events.json`: at most 128 timestamp/code events.

The saved artifact is one mode-0600 JSON document capped at 256 KiB. A final
recursive scanner refuses token-shaped values, authorization material, email
addresses, API keys, home paths, prompts, conversations, and transcripts.
Private corrupt-config backups are never copied into the report.

The native save dialog owns the destination. JavaScript receives only
`saved` or `cancelled`; it never receives a filesystem path.

## Corrupt configuration

Discovery never resets an unreadable or incompatible registry. A regular
configuration file of at most 1 MiB receives one content-addressed, mode-0600
backup in the private recovery directory. Symlinks, non-regular files, and
oversized files are not copied. Diagnostics expose only whether a private
backup is available and a stable manual-remediation code.

## Automated verification

From the repository root:

```sh
uv run --python 3.13 python -m unittest -v tests.test_diagnostics tests.test_desktop_bridge
node --test integrations/menubar/tests/*.test.mjs
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

The Rust fixtures scan the generated report for bearer material, token and key
shapes, email addresses, private home paths, and conversation text. They also
prove private permissions, age/count/size rotation, and symlink refusal.

## Packaged acceptance

1. Build the frozen sidecar and unsigned app using the commands in the menubar
   README.
2. Open **Diagnostics** from both the dashboard and tray menu.
3. Confirm ten component rows and the two-file inventory appear.
4. Save a report, inspect its schema and file permissions, and run the same
   forbidden-fixture scan used by the Rust test.
5. Quit the engine process repeatedly until degraded mode appears. Confirm the
   dashboard offers one manual retry, diagnostics, and quit without an
   unbounded restart loop.
6. Corrupt a disposable fixture registry, not a real account registry. Confirm
   recovery is read-only and only a private backup is created.
