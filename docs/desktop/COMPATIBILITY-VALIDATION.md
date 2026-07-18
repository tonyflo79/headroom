# Desktop compatibility and CLI coexistence validation

This gate covers Desktop 15 / issue #16. It treats the installed CLI, bundled
engine, bridge, registry, and desktop shell as one compatibility boundary while
allowing compatible CLI and desktop processes to share the same state root.

## Released compatibility matrix

Headroom currently has one released registry schema: `schema_version: 1`.
There is therefore no historical production transition to run or invent. The
engine advertises this fact through the exact redacted
`headroom_compatibility@1` contract:

- product name and version;
- engine version and compatible version interval;
- bridge identifier, current schema, and inclusive compatible schema range;
- observed registry schema, current schema, compatible range, status, stable
  code, remediation, and migration preview;
- platform, architecture, and the exact capability list.

The same contract is available through `headroom compatibility` and the
private desktop bridge. It contains no registry path, provider home, account
name, identity, credential, snapshot, or provider response. Rust validates all
fields and rejects unknown fields before trusting the bundled engine.

## Migration invariants

Schema v1 validates idempotently without rewriting or backing up the registry.
A future released schema transition must register every consecutive transform
in `headroom.compatibility.MIGRATIONS`; a partial chain is unavailable rather
than guessed.

The migration runner:

1. acquires the same registry lock used by compatible CLI mutations;
2. refuses symlinked, non-regular, unreadable, malformed, oversized, and newer
   registries before any write;
3. creates one content-addressed exact-byte backup in a private `0700`
   directory with a `0600` file before transforming an older schema;
4. applies every transform to an in-memory copy and validates the final schema;
5. publishes through the existing atomic registry writer;
6. is idempotent and never reads, writes, moves, or deletes provider homes.

The automated suite drives the generic runner with a synthetic v0 fixture to
prove the future transition machinery. That fixture is test-only and is not a
claim that v0 was released or is a supported production input.

## Downgrade and recovery behavior

An observed schema newer than the bundled engine is
`incompatible_newer / state_schema_too_new / upgrade_headroom`. Both CLI and
desktop expose that redacted diagnosis, leave the registry byte-identical, and
never attempt a downgrade. Invalid or unsafe state uses stable diagnostics
guidance. The shell may still open its recovery surface; incompatibility must
not crash-loop the sidecar or destroy the state needed by a newer Headroom.

## Concurrent CLI acceptance

Automated tests prove the migration runner and CLI mutation path serialize on
one registry lock. The compatibility and lifecycle suites drive concurrent
reserve, reorder, rename, cooldown, and quarantine writers. The collection
suite proves an in-flight dashboard/collector cannot republish a removed slot,
and the supervision suite proves live cross-process leases select different
accounts and cannot be stolen. One long-lived bridge process re-reads the
registry on each compatibility/discovery request, so a compatible external
state change is observed without restarting the application or blindly
overwriting it.

## Automated verification

From the repository root:

```sh
uv run --python 3.13 python -m unittest -v \
  tests.test_compatibility tests.test_desktop_bridge tests.test_account_lifecycle
uv run --python 3.13 python -m unittest discover tests
scripts/build-desktop-sidecar.sh
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

The frozen sidecar smoke frame must contain matching top-level and nested
product, platform, architecture, and capability values. The native shell must
reject mismatches, missing compatibility, unknown fields, and invalid ranges.

## Packaged acceptance

1. Start the packaged app against an isolated v1 fixture and confirm the live
   view opens with `state_schema_current`.
2. Mutate the fixture through the installed compatible CLI while the app stays
   open. Confirm the next refresh shows the change and preserves the desktop
   mutation made concurrently.
3. Replace the fixture with a byte-recorded newer-schema file. Confirm recovery
   guidance appears, no downgrade is attempted, and the file hash is unchanged.
4. Run the synthetic migration test with credential sentinels inside both an
   adopted home and a Headroom-owned home. Confirm every sentinel hash and path
   remains unchanged.
5. Quit, relaunch, and remove the app bundle. Confirm the shared state root and
   all provider homes remain present and byte-identical.
