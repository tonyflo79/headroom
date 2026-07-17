import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  accountNameError, accountStatePresentation, formatAge, formatPercent, formatReset,
  formatWeeklyReset, loginMessage,
  compactAccountWindows, externalReauthenticationConfirmation,
  externalReauthenticationPresentation,
  normalizeBootstrap, normalizeDeviceInstructions, normalizeHandoffHealth,
  normalizeRoutingPreview,
  onboardingPresentation, percentLeft,
  refreshPresentation, refreshStatePresentation, shouldApplyCommandResult,
  shouldApplySnapshot, settingsPatch, suggestedAccountName, validateSettingsDraft,
} from "../dist/main.js";

const bootstrap = {
  bridge: {
    bridge_schema: "headroom_desktop_bridge@1",
    product_version: "0.4.0",
    architecture: "arm64",
    runtime: "frozen",
  },
  view: {
    schema: "headroom_desktop_view@1",
    mode: "ready",
    settings: { title: "AI Fleet" },
    handoff: {
      schema: "headroom_handoff_health@1", configured: true, supported: true,
      state: "configured", code: "handoff_configured",
      explanation: "Automatic handoff is ready for the next Claude launch.",
      action: "none", active_session: false, account: null, model: null,
      observed_at: null, preference_effect: "next_launch_only",
    },
    candidates: [],
    accounts: [{
      name: "personal",
      provider: "claude",
      state: "current",
      windows: { "5h": { state: "current", left_percent: 72 } },
    }],
  },
};

test("normalizes a compatible sanitized bootstrap", () => {
  const value = normalizeBootstrap(bootstrap);
  assert.equal(value.view.accounts[0].name, "personal");
  assert.equal(value.view.accounts[0].provider, "claude");
});

test("normalizes only the bounded engine handoff contract", () => {
  const raw = structuredClone(bootstrap.view.handoff);
  raw.state = "armed";
  raw.code = "supervision_armed";
  raw.active_session = true;
  raw.account = "claude-a";
  raw.model = "sonnet";
  raw.observed_at = 1_800_000_000;
  assert.deepEqual(normalizeHandoffHealth(raw), raw);
  assert.throws(() => normalizeHandoffHealth({ ...raw, pid: 1234 }),
    /handoff health/);
  assert.throws(() => normalizeHandoffHealth({ ...raw, explanation: "x".repeat(257) }),
    /handoff health/);
  assert.throws(() => normalizeHandoffHealth({ ...raw, state: "pretend_healthy" }),
    /handoff health/);
  assert.doesNotThrow(() => normalizeHandoffHealth({
    ...bootstrap.view.handoff, state: "configured", code: "awaiting_session_start",
    action: "wait_for_session", active_session: true,
  }));
});

test("removes automatic handoff from the desktop surface", () => {
  const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
  assert.doesNotMatch(html, /id="handoff-health"/);
  assert.doesNotMatch(html, /settings-auto-handoff/);
  assert.doesNotMatch(html, /automatic account handoff/i);
});

test("rejects an incompatible bridge", () => {
  assert.throws(
    () => normalizeBootstrap({ ...bootstrap, bridge: { bridge_schema: "other" } }),
    /incompatible desktop engine/,
  );
});

test("does not trust malformed state or percentages", () => {
  const malformed = structuredClone(bootstrap);
  malformed.view.accounts[0].state = "pretend-live";
  assert.equal(normalizeBootstrap(malformed).view.accounts[0].state, "held");
  assert.equal(percentLeft({ state: "current", left_percent: 101 }), null);
  assert.equal(percentLeft({ state: "limited", left_percent: 50 }), 0);
});

test("accepts only sanitized adopt candidates and known modes", () => {
  const value = structuredClone(bootstrap);
  value.view.mode = "invented";
  value.view.candidates = [
    { id: "existing-codex", provider: "codex", identity: "p***@example.com" },
    { id: "bad", provider: "other", identity: "raw@example.com" },
  ];
  const normalized = normalizeBootstrap(value);
  assert.equal(normalized.view.mode, "recovery");
  assert.deepEqual(normalized.view.candidates, [value.view.candidates[0]]);
});

test("refresh progress has deterministic busy and settled presentations", () => {
  assert.deepEqual(refreshPresentation(true),
    { label: "Refreshing…", busy: true });
  assert.deepEqual(refreshPresentation(false),
    { label: "Refresh", busy: false });
});

test("Claude login diagnostics are stable and never echo unknown provider text", () => {
  assert.equal(loginMessage("browser_login"),
    "Waiting for Claude browser sign-in");
  assert.equal(loginMessage("wrong_identity"),
    "Signed-in identity did not match; credentials restored");
  assert.equal(loginMessage("raw provider secret"),
    "Login could not be completed safely");
});

test("shared login diagnostics remain accurate for Codex", () => {
  assert.equal(loginMessage("preflight"), "Checking provider CLI prerequisite");
  assert.equal(loginMessage("connected"), "Account connected");
  assert.equal(loginMessage("duplicate_identity"),
    "That identity is already connected");
  assert.equal(loginMessage("reauthenticated"),
    "Account identity verified; prior protective hold cleared");
});

test("normalizes account lifecycle policy without accepting home details", () => {
  const raw = structuredClone(bootstrap);
  raw.view.accounts[0].policy = {
    schema: "headroom_account_lifecycle@1",
    home_kind: "headroom",
    home_retained_on_remove: true,
    rename_keeps_home: true,
    reauthentication: "available",
    position: 0,
    count: 2,
    can_move_up: false,
    can_move_down: true,
    can_remove: true,
    home: "/private/provider/home",
  };
  const policy = normalizeBootstrap(raw).view.accounts[0].policy;
  assert.deepEqual(policy, {
    schema: "headroom_account_lifecycle@1",
    home_kind: "headroom",
    home_retained_on_remove: true,
    rename_keeps_home: true,
    reauthentication: "available",
    position: 0,
    count: 2,
    can_move_up: false,
    can_move_down: true,
    can_remove: true,
  });
  assert.equal(JSON.stringify(policy).includes("/private"), false);
  raw.view.accounts[0].policy.reauthentication = "invented";
  assert.equal(normalizeBootstrap(raw).view.accounts[0].policy, null);
});

test("external provider recovery appears only for an engine-authorized held slot", () => {
  const raw = structuredClone(bootstrap);
  raw.view.accounts[0].state = "held";
  raw.view.accounts[0].recovery_action = "external_reauthentication";
  raw.view.accounts[0].policy = {
    schema: "headroom_account_lifecycle@1", home_kind: "headroom",
    home_retained_on_remove: true, rename_keeps_home: true,
    reauthentication: "keychain_manual", position: 0, count: 1,
    can_move_up: false, can_move_down: false, can_remove: false,
  };
  const account = normalizeBootstrap(raw).view.accounts[0];
  const presentation = externalReauthenticationPresentation(account);
  assert.equal(presentation.label, "Open Claude login");
  assert.match(presentation.warning, /cannot be rolled back/);
  assert.match(presentation.confirmation, /personal/);

  raw.view.accounts[0].state = "current";
  assert.equal(externalReauthenticationPresentation(
    normalizeBootstrap(raw).view.accounts[0]), null);
  raw.view.accounts[0].state = "held";
  raw.view.accounts[0].recovery_action = "invented";
  assert.equal(normalizeBootstrap(raw).view.accounts[0].recovery_action, null);
});

test("external provider recovery uses visible two-step confirmation", () => {
  const raw = structuredClone(bootstrap);
  raw.view.accounts[0].state = "held";
  raw.view.accounts[0].recovery_action = "external_reauthentication";
  raw.view.accounts[0].policy = {
    schema: "headroom_account_lifecycle@1", home_kind: "headroom",
    home_retained_on_remove: true, rename_keeps_home: true,
    reauthentication: "keychain_manual", position: 0, count: 1,
    can_move_up: false, can_move_down: false, can_remove: false,
  };
  const account = normalizeBootstrap(raw).view.accounts[0];
  assert.deepEqual(externalReauthenticationConfirmation(account), {
    shouldLaunch: false,
    label: "Confirm Claude login",
    message: "Click again to open claude sign-in for personal.",
  });
  assert.deepEqual(externalReauthenticationConfirmation(account, true), {
    shouldLaunch: true,
    label: "Opening Claude login…",
    message: "Re-proving personal before opening provider sign-in…",
  });
  const source = readFileSync(new URL("../dist/main.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /window\.confirm/);
});

test("compact account cards show only 5h, week, and Fable without inventing windows", () => {
  const five = { state: "current", left_percent: 80 };
  const week = { state: "current", left_percent: 60 };
  const fable = { state: "current", left_percent: 40 };
  assert.deepEqual(compactAccountWindows({
    "5h": five, "7d": week, "scoped:FABLE": fable,
    "scoped:Opus": { state: "current", left_percent: 20 },
  }), [
    { label: "5h", value: five },
    { label: "week", value: week },
    { label: "Fable", value: fable },
  ]);
  assert.deepEqual(compactAccountWindows({ "7d": week }), [
    { label: "5h", value: null },
    { label: "week", value: week },
    { label: "Fable", value: null },
  ]);
});

test("device instructions accept only the exact OpenAI HTTPS origin", () => {
  const safe = { verification_url: "https://auth.openai.com/codex/device",
    user_code: "ABCD-EFGH" };
  assert.deepEqual(normalizeDeviceInstructions(safe), safe);
  assert.equal(normalizeDeviceInstructions({ ...safe,
    verification_url: "https://auth.openai.com.evil.test/codex/device" }), null);
  assert.equal(normalizeDeviceInstructions({ ...safe,
    verification_url: "https://auth.openai.com/other" }), null);
  assert.equal(normalizeDeviceInstructions({ ...safe, user_code: "<secret>" }), null);
});

test("generated slot names are valid and avoid configured names", () => {
  assert.equal(suggestedAccountName("claude", []), "claude-1");
  assert.equal(suggestedAccountName("claude", ["claude-1"]), "claude-2");
  assert.equal(suggestedAccountName("codex", ["codex-new"], "new"), "codex-2");
  assert.equal(accountNameError("Codex 1"),
    "Use lowercase letters, digits, - or _ (32 characters maximum)");
  assert.equal(accountNameError("codex-1", ["codex-1"]),
    "That slot name is already in use");
  assert.equal(accountNameError("codex-2", ["codex-1"]), null);
});

test("normalizes resumable onboarding without accepting provider details", () => {
  const raw = structuredClone(bootstrap);
  raw.view.mode = "onboarding";
  raw.view.onboarding = {
    schema: "headroom_desktop_onboarding@1",
    step: "providers",
    resumable: true,
    providers: [
      { provider: "claude", state: "ready", candidate_available: true,
        connected_count: 0, path: "/private/claude" },
      { provider: "codex", state: "missing", candidate_available: false,
        connected_count: 0, version: "secret-version" },
      { provider: "other", state: "ready" },
    ],
  };
  const value = normalizeBootstrap(raw);
  assert.equal(value.view.onboarding.step, "providers");
  assert.equal(value.view.onboarding.resumable, true);
  assert.deepEqual(value.view.onboarding.providers, [
    { provider: "claude", state: "ready", candidate_available: true,
      connected_count: 0 },
    { provider: "codex", state: "missing", candidate_available: false,
      connected_count: 0 },
  ]);
  assert.equal(JSON.stringify(value).includes("/private/claude"), false);
  assert.equal(onboardingPresentation(value.view.onboarding).headline,
    "Choose which provider accounts to use");
});

test("demo presentation is explicit and provider-free", () => {
  assert.deepEqual(onboardingPresentation({ step: "demo" }), {
    title: "> demo mode",
    headline: "Bundled sample data · no provider access",
  });
});

test("revisioned snapshots preserve account order and sanitize surface metadata", () => {
  const raw = structuredClone(bootstrap);
  raw.revision = 9;
  raw.surface = "popover";
  raw.theme = "terminal";
  raw.view.accounts = [
    { name: "codex-first", provider: "codex", state: "held", windows: {} },
    { name: "claude-second", provider: "claude", state: "current", windows: {} },
  ];
  const value = normalizeBootstrap(raw);
  assert.equal(value.revision, 9);
  assert.equal(value.surface, "popover");
  assert.equal(value.theme, "terminal");
  assert.deepEqual(value.view.accounts.map((row) => row.name),
    ["codex-first", "claude-second"]);
  raw.surface = "remote";
  raw.theme = "https://evil.test/theme.css";
  assert.equal(normalizeBootstrap(raw).surface, "main");
  assert.equal(normalizeBootstrap(raw).theme, "terminal");
  assert.equal(shouldApplySnapshot(9, 10), true);
  assert.equal(shouldApplySnapshot(9, 9), false);
  assert.equal(shouldApplySnapshot(9, 8), false);
  assert.equal(shouldApplyCommandResult(9, 9), true);
  assert.equal(shouldApplyCommandResult(9, 10), false);
});

test("reset and account state copy remain actionable without color", () => {
  assert.equal(formatReset(1_800_003_600, 1_800_000_000_000).label,
    "resets in 1 hour");
  assert.equal(formatReset(1_799_999_999, 1_800_000_000_000).label, "reset due");
  assert.match(formatWeeklyReset(1_800_086_400), /\w{3,}.*\d{1,2}.*\d{1,2}:\d{2}/);
  assert.equal(formatWeeklyReset(null), "—");
  assert.match(accountStatePresentation({ state: "stale" }).action, /refresh/);
  assert.match(accountStatePresentation({
    state: "stale", diagnostic_code: "provider_offline",
    observation_age_seconds: 3720,
  }).action, /1h 2m old.*automatic retry/);
  assert.equal(formatAge(3720), "1h 2m");
  assert.match(accountStatePresentation({ state: "limited" }).action, /wait/);
  assert.match(accountStatePresentation({ state: "held", note: "Reconnect account" }).action,
    /Reconnect/);
  assert.match(accountStatePresentation({ state: "current", reserved: true }).action,
    /excluded/);
  assert.match(refreshStatePresentation("offline").label, /OFFLINE/);
  assert.equal(refreshStatePresentation("refreshing").busy, true);
  assert.match(refreshStatePresentation("backoff").label, /jittered/);
  assert.equal(refreshStatePresentation("recovering").busy, true);
  assert.match(refreshStatePresentation("degraded", "engine_unexpected_exit").label,
    /restart loop.*engine_unexpected_exit/);
  assert.doesNotMatch(refreshStatePresentation("degraded", "raw private detail").label,
    /private/);
});

test("normalizes desktop settings with safe local defaults", () => {
  const raw = structuredClone(bootstrap);
  raw.view.settings = {
    title: "Terminal Fleet", theme: "midnight", redact_emails: false,
    reserve_percent: 12.5, auto_handoff: false, refresh_interval_seconds: 600,
    provider_paths: { claude: "/opt/homebrew/bin/claude", codex: "relative/codex" },
    preferred_terminal: "warp", remember_window: false,
    notifications: {
      enabled: true, reset_enabled: true, global_threshold_percent: 15,
      provider_threshold_percent: { claude: 12, codex: 100 },
    },
  };
  const settings = normalizeBootstrap(raw).view.settings;
  assert.equal(settings.theme, "midnight");
  assert.equal(settings.refresh_interval_seconds, 600);
  assert.deepEqual(settings.provider_paths, { claude: "/opt/homebrew/bin/claude" });
  assert.equal(settings.preferred_terminal, "warp");
  assert.equal(settings.remember_window, false);
  assert.deepEqual(settings.notifications.provider_threshold_percent, { claude: 12 });

  raw.view.settings = { title: "Headroom" };
  const defaults = normalizeBootstrap(raw).view.settings;
  assert.equal(defaults.refresh_interval_seconds, 300);
  assert.equal(defaults.notifications.enabled, false);
  assert.equal(defaults.notifications.reset_enabled, false);
});

test("validates and projects the complete settings contract", () => {
  const draft = {
    theme: "terminal", title: "Headroom", redact_emails: true,
    reserve_percent: "10.5", auto_handoff: true,
    refresh_interval_seconds: "300", claude_path: "", codex_path: "/usr/bin/true",
    preferred_terminal: "terminal", remember_window: true,
    notifications_enabled: false, reset_notifications: false,
    notification_threshold: "20", claude_notification_threshold: "",
    codex_notification_threshold: "15",
  };
  assert.deepEqual(validateSettingsDraft(draft), {});
  assert.deepEqual(settingsPatch(draft), {
    theme: "terminal", title: "Headroom", redact_emails: true,
    reserve_percent: 10.5, auto_handoff: true, refresh_interval_seconds: 300,
    provider_paths: { claude: null, codex: "/usr/bin/true" },
    preferred_terminal: "terminal", remember_window: true,
    notifications: {
      enabled: false, reset_enabled: false, global_threshold_percent: 20,
      provider_threshold_percent: { claude: null, codex: 15 },
    },
  });
  assert.equal(validateSettingsDraft({ ...draft, title: " Headroom" }).title !== undefined,
    true);
  assert.equal(validateSettingsDraft({ ...draft, refresh_interval_seconds: "30" })
    .refresh_interval_seconds !== undefined, true);
  assert.equal(validateSettingsDraft({ ...draft, codex_path: "relative/codex" })
    .codex_path !== undefined, true);
  assert.equal(validateSettingsDraft({ ...draft, notification_threshold: "100" })
    .notification_threshold !== undefined, true);
});

test("uses locale formatters for percentages and exposes the settings console", () => {
  assert.match(formatPercent(72), /72/);
  const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
  for (const id of ["settings-theme", "settings-refresh", "settings-terminal",
    "settings-launch-at-login", "settings-notifications", "settings-save"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /⌘, settings · ⌘R refresh · ⌘W close · ⌘Q quit/);
});

test("normalizes one bounded engine routing decision without command material", () => {
  const preview = normalizeRoutingPreview({
    schema: "headroom_desktop_routing@1", family: "sonnet", provider: "claude",
    selected: { name: "claude-one", provider: "claude" },
    candidates: [
      { name: "claude-one", provider: "claude", selected: true, eligible: true,
        code: "selected", explanation: "This is the engine-selected account.",
        action: "copy_or_open" },
      { name: "claude-two", provider: "claude", selected: false, eligible: false,
        code: "leased", explanation: "Another live launch currently owns this slot.",
        action: "close_other_session" },
      { name: "claude-three", provider: "claude", selected: false, eligible: false,
        code: "quarantined", explanation: "Authentication was rejected for this slot.",
        action: "reauthenticate_account" },
    ],
    launch: { status: "ready", code: "launch_ready",
      explanation: "The selected account can be launched safely.",
      action: "copy_or_open" },
    command: "rm -rf /",
    environment: { DANGEROUS: "ignored" },
  });
  assert.equal(preview.selected.name, "claude-one");
  assert.deepEqual(preview.candidates.map((row) => row.code),
    ["selected", "leased", "quarantined"]);
  assert.equal("command" in preview, false);
  assert.equal("environment" in preview, false);
});

test("routing preview refuses malformed selection and unbounded explanations", () => {
  const base = {
    schema: "headroom_desktop_routing@1", family: "codex", provider: "codex",
    selected: { name: "codex-one", provider: "codex" },
    candidates: [{ name: "codex-one", provider: "codex", selected: true,
      eligible: true, code: "selected", explanation: "Selected.",
      action: "copy_or_open" }],
    launch: { status: "ready", code: "launch_ready", explanation: "Ready.",
      action: "copy_or_open" },
  };
  assert.throws(() => normalizeRoutingPreview({ ...base,
    selected: { name: "other", provider: "codex" } }), /selection summary/);
  const oversized = structuredClone(base);
  oversized.candidates[0].explanation = "x".repeat(257);
  assert.throws(() => normalizeRoutingPreview(oversized), /candidate/);
  assert.throws(() => normalizeRoutingPreview({ ...base, family: "arbitrary" }),
    /routing preview/);
});

test("routing UI exposes only family and account based native actions", () => {
  const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
  const source = readFileSync(new URL("../dist/main.js", import.meta.url), "utf8");
  for (const id of ["routing-family", "routing-preview", "routing-copy",
    "routing-open", "routing-candidates"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(source, /desktop_copy_routing_command/);
  assert.match(source, /desktop_open_routing_launch/);
  assert.match(source, /desktop_open_external_reauthentication/);
  assert.doesNotMatch(source, /desktop_account_action/);
  assert.doesNotMatch(source, /desktop_(copy|open)_routing_(command|launch)[\s\S]{0,180}(command|environment):/);
  assert.doesNotMatch(source, /desktop_open_external_reauthentication[\s\S]{0,180}(home|command|environment):/);
});

test("all five themes define the same semantic token contract", () => {
  const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
  const css = readFileSync(new URL("../dist/style.css", import.meta.url), "utf8");
  assert.match(css, /\.account header > div \{ min-width: 0; \}/);
  assert.match(css, /\.account header \.state \{ flex: 0 0 auto; \}/);
  assert.match(css, /overflow-wrap: anywhere/);
  assert.match(css, /body\[data-surface="main"\] \.accounts \{ grid-template-columns: repeat\(5/);
  assert.match(html, /id="routing"[^>]*hidden/);
  assert.ok(html.indexOf('id="accounts"') < html.indexOf('id="add-account"'));
  assert.match(css, /\.weekly-reset/);
  const tokens = ["--canvas:", "--panel:", "--panel-strong:", "--control:",
    "--phosphor:", "--phosphor-bright:", "--phosphor-dim:", "--line:",
    "--warning:", "--danger:", "--scanline:", "--ambient:", "--glow-color:"];
  for (const theme of ["midnight", "minimal", "chrome", "paper", "terminal"]) {
    const block = css.split(`body[data-theme="${theme}"] {`, 2)[1].split("}", 1)[0];
    for (const token of tokens) assert.match(block, new RegExp(token));
  }
});
