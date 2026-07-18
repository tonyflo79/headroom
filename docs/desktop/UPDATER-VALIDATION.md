# Signed desktop updater validation

This gate covers Desktop 17 / issue #18. Updates replace only the application
bundle. Headroom’s registry, provider homes, cooldowns, leases, quarantine,
snapshots, notification ledger, and window preferences live outside that
bundle and must survive upgrades, failures, rollback, and app removal.

## Release and trust contract

`VERSION` is the only product-release version source. The Python package,
frozen engine, Rust package, Tauri bundle, JavaScript package, CI artifact
name, and updater manifest are generated or checked against it. Bridge and
state schemas remain independently versioned compatibility contracts.

The app contains a Minisign public key and one HTTPS manifest endpoint. The
stable build checks only the repository’s `latest` release asset. The opt-in
prerelease build is produced with a separate compile-time overlay and checks
only the fixed `prerelease` channel asset. The webview cannot provide an
endpoint, path, signature, artifact, or command. `scripts/updater_release.py
--check-config` fails if either endpoint or either public key drifts.

Tauri verifies the downloaded bytes against the manifest signature before it
installs them. Ordinary CI builds set `createUpdaterArtifacts` to false so an
unsigned tracer cannot be mistaken for an update. The release overlays enable
updater artifacts and require `TAURI_SIGNING_PRIVATE_KEY` (and its password)
from the release environment. The private updater key must never enter the
repository, application bundle, logs, or manifest.

Build the opt-in channel only with both the prerelease overlay and
`HEADROOM_UPDATE_CHANNEL=prerelease`; this labels the bounded native projection
without exposing or selecting an endpoint at runtime.

The page receives only `headroom_desktop_update@1`: channel, current version,
phase, candidate version, bounded plain-text notes, and a stable code. It asks
twice by visible two-step actions: first before signature verification and
installation, then separately before restart. There is no silent restart.

## State-preservation boundary

The installer may replace `Headroom.app`; it must never read or mutate:

- `~/.headroom/config.json`;
- `~/.headroom/homes/` or an adopted external provider home;
- `~/.headroom/state/cooldowns.json`, `provider-backoff.json`, or
  `quarantine.json`;
- `~/.headroom/state/leases/` (live leases remain kernel-owned by the process
  holding them);
- private and public usage snapshots;
- the Tauri application-support notification and window ledgers.

Any future state-schema migration is governed by
`COMPATIBILITY-VALIDATION.md`; the updater itself does not migrate state.

## Automated checks

From the repository root:

```sh
uv run --python 3.13 python scripts/release-version.py --check
uv run --python 3.13 python scripts/updater_release.py --check-config
uv run --python 3.13 python -m unittest tests.test_updater_release
npm --prefix integrations/menubar test
cargo fmt --check --manifest-path integrations/menubar/src-tauri/Cargo.toml
cargo test --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml
```

These checks prove bounded metadata, immutable channel configuration, one
public key, exact versioned GitHub artifact URLs, explicit install/restart
actions, and absence of transport or signature material from the webview.
For a locally built staging artifact, additionally run:

```sh
cargo run --locked --manifest-path integrations/menubar/src-tauri/Cargo.toml \
  --example verify_updater_artifact -- \
  integrations/menubar/src-tauri/target/release/bundle/macos/Headroom.app.tar.gz \
  integrations/menubar/src-tauri/target/release/bundle/macos/Headroom.app.tar.gz.sig \
  integrations/menubar/src-tauri/tauri.conf.json
```

That verifier must accept the artifact and then refuse an in-memory one-byte
mutation using the same public key compiled into the app.

## Staging acceptance matrix

Use an isolated `HEADROOM_DIR` containing synthetic credential sentinels; do
not copy personal credentials into test artifacts. Record file hashes, modes,
and directory existence before and after each row. Confirm the previously
installed app still launches after every negative row.

| Scenario | Injection | Required result |
| --- | --- | --- |
| previous → candidate | publish a valid candidate signed by the staging key | notes shown; two confirmations; candidate launches; all state hashes/modes unchanged |
| interrupted download | terminate the local staging server midway through the artifact | install fails; previous app launches; state unchanged |
| insufficient disk | use a constrained staging volume smaller than the artifact | install fails; previous app launches; state unchanged |
| invalid metadata | omit target, use malformed SemVer, or exceed note bounds | no install action; stable failure code; previous app launches |
| invalid signature | sign with a different key | verification fails; previous app launches; state unchanged |
| tampered artifact | change one byte after signing | verification fails; previous app launches; state unchanged |
| rollback | publish a newly versioned, signed build containing the prior known-good code | updater treats it as a forward version; operator notes label it rollback; state unchanged |

Do not implement rollback by lowering the SemVer or bypassing the signature.
Cut `X.Y.(Z+1)` from the known-good source, sign it with the normal release
key, publish explicit rollback notes, test it on prerelease, then promote it to
stable. If the release key may be compromised, stop publishing and ship a
manually installed, Developer-ID-signed build with a rotated public key; never
weaken verification in the field.

## Current evidence

The automated contract and unsigned packaged build are release prerequisites,
not substitutes for the staging matrix. Record candidate commit, previous and
candidate versions, architecture, macOS version, artifact hashes, and the
result of every row here when the signed staging channel is exercised.

| Date | Candidate | Channel | Result |
| --- | --- | --- | --- |
| pending | pending | prerelease staging | signed end-to-end matrix not yet run |
