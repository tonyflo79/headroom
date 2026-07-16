# Claude desktop login validation

This is the human acceptance gate for desktop issue #4. Use one legitimately
owned test account on a clean macOS user. Never paste credentials, tokens,
Keychain contents, raw provider output, or an unredacted email into this file or
a GitHub comment.

## Prerequisites

- A packaged Headroom app signed with the identity intended for the prototype.
- Claude Code 2.1.207 or newer installed in a supported fixed location.
- A backup of `~/.headroom` made while Headroom is closed.
- No unrelated Claude login or collection process running.

## Acceptance run

1. Launch Headroom from Finder, not Terminal.
2. Choose **connect new Claude login**, enter a unique lowercase slot name, and
   optionally enter the expected account email.
3. Confirm the system browser opens the provider-owned login page and Headroom
   shows `Waiting for Claude browser sign-in` without provider output.
4. Complete the provider flow with the owned test account.
5. Confirm Headroom reports the account as connected with a redacted identity
   and an honestly held reading until the first manual refresh.
6. Quit Headroom and confirm both the app and frozen engine exit.
7. Relaunch from Finder and confirm the slot persists without another login.

## Destructive-state safety run

Using fixture accounts or a disposable macOS user, repeat the flow for cancel,
timeout, CLI failure, wrong expected identity, duplicate identity, and
unreadable identity. For every case, record only:

- UTC time;
- packaged build commit;
- terminal result code shown by Headroom;
- pass/fail for prior credential restoration;
- pass/fail for absence of a new registry slot.

The legacy shared-Keychain prerequisite must refuse before the browser login
starts and must offer no force bypass.

## Evidence record

Status: not yet human-validated

| UTC time | Build commit | Scenario | Result code | Rollback | Registry unchanged |
|---|---|---|---|---|---|
| pending | pending | real owned account | pending | pending | pending |
