# Token-burn validation

The desktop token-burn view follows the normalized local-day contract from
Nate B. Jones's token-burn guide, adapted to Headroom's bundled native sidecar.
It does not use a localhost or Vercel service.

## Fidelity contract

- Source fields are read exactly from local provider events, but the displayed
  number is deliberately labeled an **effective-token estimate**. It is a
  comparison baseline, not a reconstruction of either provider's proprietary
  subscription-limit formula.
- Codex uses `last_token_usage.total_tokens` as raw throughput. Because
  `cached_input_tokens` is a subset of that total, effective tokens are
  `total - cached + round(cached × 0.10)`.
- Claude Code sums input, output, cache-creation, and cache-read fields as raw
  throughput. Effective tokens are `raw - cache_read +
  round(cache_read × 0.10)`; cache creation remains fully weighted.
- Repeated Claude transcript rows with the same session and message ID are one
  API call. The index retains the maximum observed usage for that call.
- Copied or forked Codex event rows are deduplicated by an opaque event hash.
- UTC timestamps are converted to the configured system timezone before local
  calendar dates and today/7-day/30-day windows are calculated.
- Historical Claude logs in a shared provider home are reported as exact but
  unattributed. They are never assigned to one of the four configured account
  cards without account evidence.
- Renaming, adding, removing, or moving a configured account atomically rebuilds
  the private index. Historical events therefore follow the current slot/home
  mapping, and removed accounts cannot remain hidden inside aggregate totals.
  Upgrading to this index schema also forces one repair rebuild for installations
  that may already contain stale attribution.
- Codex sessions that contain no token events create a visible partial-coverage
  warning. Their cumulative thread total is not guessed onto a date.
- No Claude chat or ChatGPT estimates are produced by this version.

## Privacy boundary

`~/.headroom/state/activity-v2.sqlite` is mode `0600`. Raw logs, prompts,
emails, project names, file paths, session IDs, request IDs, and message IDs do
not cross the desktop bridge. The UI receives bounded numeric daily rows,
generic driver labels, fidelity state, and stable warning codes only.

## Reconciliation gate

The regression fixture independently exercises both provider formulas:

- Codex: raw `150`, cache reads `110`, effective `51`.
- Claude Code: raw `200`, cache reads `60`, effective `146`.

The private index must return those effective values with a delta of zero. A
release candidate must additionally reconcile at least one completed private
local day with an independent, non-persisting calculation.

## Release gate

1. Run `python3 -m unittest discover -s tests`.
2. Run `npm test` in `integrations/menubar`.
3. Run `cargo test` in `integrations/menubar/src-tauri`.
4. Reconcile at least one completed local day independently.
5. Confirm an initial full index runs off the UI thread and subsequent scans
   read only appended bytes.
6. Confirm the desktop app has no listening TCP socket.
