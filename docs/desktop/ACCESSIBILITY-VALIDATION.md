# Desktop accessibility validation

This gate covers Desktop 16 / issue #17. It applies to onboarding, the main
dashboard, compact popover, account lifecycle and login flows, settings,
routing, recovery, token detail, diagnostics, and the signed-update surface.

## Automated contract

The desktop test package pins `axe-core` and `jsdom` in `package-lock.json`.
CI installs them with lifecycle scripts disabled, then scans every currently
renderable major surface against WCAG 2 A/AA, WCAG 2.1 A/AA, and WCAG 2.2 AA
rules. Axe’s color-contrast rule cannot run reliably in jsdom, so an additional
deterministic test computes relative luminance and contrast for every semantic
text token against every canvas, panel, strong-panel, and control background.

The weakest tested normal-text pairing in each theme is:

| Theme | Minimum ratio |
| --- | ---: |
| Midnight | 5.38:1 |
| Terminal | 6.03:1 |
| Minimal | 4.75:1 |
| Chrome | 5.03:1 |
| Paper | 4.69:1 |

All exceed the WCAG AA 4.5:1 threshold. State is also written in text and
exposed through labels, `aria-valuetext`, `aria-pressed`, or stable status
copy; color and glow are never the only signal.

Automated interaction tests prove:

- Refresh, Route, and Settings appear in logical keyboard order.
- Route and Settings move focus into the opened panel.
- Escape closes either panel and returns focus to its opener.
- Every capacity window is a named meter with a value or “not available.”
- Account cards and window groups have stable accessible names.
- Login, reauthentication, settings, routing, snapshot, and updater progress
  use polite, atomic status regions without forcing focus.
- Install and restart remain separate, described confirmation actions.
- token heatmap cells and the weekly trend expose non-color text equivalents;
  the activity table has a caption and range controls expose pressed state.
- locale-sensitive percentage, compact-number, relative-time, and reset-time
  formatters use the system locale and timezone.
- reduced motion disables smooth focus scrolling, transitions, and repeating
  animation through `prefers-reduced-motion`.

Run the gate from the repository root:

```sh
npm ci --prefix integrations/menubar --ignore-scripts
npm --prefix integrations/menubar test
```

## Native surfaces

macOS Notification Center owns notification focus, announcement, dismissal,
and accessibility. Headroom supplies redacted native titles and bodies only;
it never shows a custom overlay or activates a window. The macOS title bar,
standard controls, menu-bar item, and Finder-installed application bundle are
reviewed in the packaged human pass below.

## Human VoiceOver acceptance

Automation cannot prove announcement quality, rotor order, system zoom,
Notification Center behavior, or native WebKit focus behavior. A human must
run this matrix on the oldest supported macOS release (13) and the newest
supported release. The beta pool must also cover both Intel and Apple Silicon;
those architecture checks need not be repeated for every OS version.

For each machine:

1. Enable VoiceOver and Full Keyboard Access before launching Headroom.
2. Install through Finder and confirm the app, menu-bar item, and initial
   dashboard are announced without Terminal.
3. Traverse Refresh, Route, Settings, every account, token detail, Add Account,
   and the footer in order using only the keyboard.
4. Open and close Route and Settings; confirm focus moves into the panel and
   returns to the opener with Escape.
5. Exercise demo/onboarding, one account login, held-account recovery, rename,
   reorder, removal confirmation, validation error, and engine recovery.
6. Confirm account name, provider, identity, state, 5-hour/week/Fable value,
   reset time, token coverage, progress, and error remediation are meaningful.
7. Enable notifications explicitly, cross a synthetic verified threshold, and
   confirm Notification Center announces it without activating Headroom.
8. Present a signed staging update; confirm notes, first confirmation,
   download/verification progress, second restart confirmation, and failure
   copy are announced.
9. Repeat in every theme, at increased text/zoom, and with Reduce Motion on.
10. Record OS, architecture, app commit/version, failures, and scrubbed notes.

## Current evidence

| Date | Environment | Result |
| --- | --- | --- |
| 2026-07-18 | automated jsdom + axe-core 4.12.1, Node 22 | dashboard, onboarding, recovery, settings, routing, diagnostics, popover, account, token, and update scans pass; contrast/keyboard/semantics/locale/reduced-motion pass |
| pending | macOS 13 Intel or Apple Silicon | human VoiceOver pass required |
| pending | newest supported macOS, opposite architecture where possible | human VoiceOver pass required |

Do not close issue #17 until both human rows are complete.
