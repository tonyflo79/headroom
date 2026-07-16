# Codex desktop login validation

This is the human acceptance gate for desktop issue #5. Use one legitimately
owned ChatGPT subscription account on a clean macOS user. Never paste a device
code, credentials, tokens, raw provider output, or an unredacted email into
this file or a GitHub comment.

## Prerequisites

- A packaged Headroom app signed with the identity intended for the prototype.
- Codex CLI 0.144.0 or newer, with device auth and app-server support, installed
  in a supported fixed location.
- A backup of `~/.headroom` made while Headroom is closed.
- No unrelated Codex login or collection process running.
- A ChatGPT subscription login distinct from any Codex identity already in the
  Headroom registry. An API key is intentionally not accepted by this flow.

## Acceptance run

1. Launch Headroom from Finder, not Terminal.
2. Choose **connect new Codex login**, enter a unique lowercase slot name, and
   optionally enter the expected account email.
3. Confirm Headroom shows a one-time code and an **Open OpenAI** button without
   displaying raw Codex output.
4. Open the link and confirm the browser destination is exactly
   `https://auth.openai.com/codex/device` before entering the one-time code.
5. Complete the provider flow with the owned subscription account.
6. Confirm Headroom reports success only after showing a redacted identity and
   a current live capacity reading. It must not publish an honestly unknown,
   local-only, or API-key reading as connected.
7. Quit Headroom and confirm both the app and frozen engine exit.
8. Relaunch from Finder and confirm the slot persists without another login.

## Failure-state safety run

Using fixture accounts or a disposable macOS user, repeat the flow for cancel,
expired code, provider rejection, malformed instructions, wrong expected
identity, duplicate identity, API-key auth, unsupported CLI, and live capacity
failure. For every case, record only:

- UTC time;
- packaged build commit;
- terminal result code shown by Headroom;
- pass/fail for exact prior credential restoration;
- pass/fail for absence of a new registry slot.

Cancel must send the structured `account/login/cancel` request. A URL outside
the exact allowlist must be refused before the system browser can open it.

## Evidence record

Status: not yet human-validated

| UTC time | Build commit | Scenario | Result code | Rollback | Registry unchanged |
|---|---|---|---|---|---|
| pending | pending | real owned subscription | pending | pending | pending |
