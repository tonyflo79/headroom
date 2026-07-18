import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { JSDOM } from "jsdom";


const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
const dom = new JSDOM(html, {
  pretendToBeVisual: true,
  url: "https://headroom.invalid/",
});
const { window } = dom;
for (const name of [
  "window", "document", "Node", "NodeList", "Element", "HTMLElement",
  "HTMLInputElement", "HTMLSelectElement", "HTMLButtonElement", "DocumentFragment",
  "MutationObserver", "getComputedStyle",
]) {
  globalThis[name] = name === "window" ? window
    : name === "document" ? window.document : window[name];
}
window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
window.HTMLElement.prototype.scrollIntoView = () => {};

const axe = (await import("axe-core")).default;
const { prefersReducedMotion, renderBootstrap } = await import("../dist/main.js");

const handoff = {
  schema: "headroom_handoff_health@1", configured: true, supported: true,
  state: "configured", code: "handoff_configured",
  explanation: "Manual account routing is ready.", action: "none",
  active_session: false, account: null, model: null, observed_at: null,
  preference_effect: "next_launch_only",
};
const update = {
  schema: "headroom_desktop_update@1", channel: "stable",
  current_version: "0.4.0", phase: "current", available_version: null,
  notes: null, code: "update_current",
};
const diagnostics = {
  schema: "headroom_desktop_diagnostics@1", generated_at: 1_800_000_000,
  app_version: "0.4.0", engine_version: "0.4.0", private_backup: false,
  components: [
    "app", "sidecar", "update", "engine", "bridge", "registry", "snapshot",
    "activity", "provider_claude", "provider_codex",
  ].map((id) => ({ id, state: "ok", code: `${id}_ready`, remediation: "none" })),
  inventory: [
    { name: "health.json", kind: "redacted_health", records: 10 },
    { name: "events.json", kind: "code_only_events", records: 2 },
  ],
};
const bridge = {
  bridge_schema: "headroom_desktop_bridge@1", product_version: "0.4.0",
  architecture: "arm64", runtime: "frozen",
};
const settings = {
  title: "Headroom", theme: "terminal", redact_emails: true,
  reserve_percent: 10, auto_handoff: false, refresh_interval_seconds: 300,
  provider_paths: {}, preferred_terminal: "terminal", remember_window: true,
  notifications: {
    enabled: false, reset_enabled: false, global_threshold_percent: 20,
    provider_threshold_percent: {},
  },
};

function view(mode = "ready") {
  return {
    schema: "headroom_desktop_view@1", mode, settings,
    freshness: { state: "current", age_seconds: 0, reason: "collected" },
    headline: {
      avg_5h_left_percent: 72, avg_7d_left_percent: 64,
      current_accounts: 2, total_accounts: 2,
    },
    handoff, candidates: [],
    accounts: [{
      name: "claude1", provider: "claude", identity: "a***@example.com",
      plan: "Max", state: "current", trust_state: "verified", reserved: false,
      windows: {
        "5h": { state: "current", left_percent: 72, resets_at: 1_800_000_000 },
        "7d": { state: "current", left_percent: 64, resets_at: 1_800_500_000 },
        "scoped:Fable": { state: "current", left_percent: 91, resets_at: 1_800_500_000 },
      },
    }],
    onboarding: {
      schema: "headroom_desktop_onboarding@1",
      step: mode === "onboarding" ? "welcome" : "complete",
      resumable: false, recovery_code: null,
      providers: [
        { provider: "claude", state: "ready", candidate_available: false, connected_count: 1 },
        { provider: "codex", state: "ready", candidate_available: false, connected_count: 1 },
      ],
    },
  };
}

async function invoke(command) {
  if (command === "desktop_update_status") return update;
  if (command === "desktop_diagnostics") return diagnostics;
  if (command === "desktop_launch_at_login_status") return false;
  return {};
}

function render(mode = "ready", surface = "main") {
  return renderBootstrap({
    bridge, view: view(mode), revision: 1, theme: "terminal", surface,
  }, invoke);
}

async function scan(label, context = document) {
  const result = await axe.run(context, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    rules: { "color-contrast": { enabled: false } },
  });
  assert.deepEqual(
    result.violations.map((violation) => ({
      id: violation.id,
      nodes: violation.nodes.map((node) => node.target.join(" ")),
    })),
    [],
    `${label} accessibility violations`,
  );
}

test("axe scans every major desktop webview surface", async () => {
  render();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await scan("dashboard");

  window.__headroomOpenPanel("settings");
  await scan("settings", document.getElementById("settings"));
  document.getElementById("settings").hidden = true;

  window.__headroomOpenPanel("routing");
  await scan("routing", document.getElementById("routing"));
  document.getElementById("routing").hidden = true;

  window.__headroomOpenPanel("diagnostics");
  await scan("diagnostics", document.getElementById("diagnostics"));
  document.getElementById("diagnostics").hidden = true;

  window.__headroomApplyUpdate({
    ...update, phase: "available", available_version: "0.4.1",
    notes: "Accessibility and stability improvements.", code: "update_available",
  });
  await scan("update", document.getElementById("update-panel"));

  render("onboarding");
  await scan("onboarding");

  render("recovery");
  await scan("recovery");

  render("ready", "popover");
  await scan("popover");
});

function luminance(hex) {
  const channels = hex.slice(1).match(/../g).map((value) => parseInt(value, 16) / 255)
    .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(first, second) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

function themeTokens(css, theme) {
  const marker = `body[data-theme="${theme}"] {`;
  const block = css.split(marker, 2)[1].split("}", 1)[0];
  return Object.fromEntries([...block.matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})/gi)]
    .map((match) => [match[1], match[2].toLowerCase()]));
}

test("every semantic text token meets WCAG AA on every theme surface", () => {
  const css = readFileSync(new URL("../dist/style.css", import.meta.url), "utf8");
  for (const theme of ["midnight", "terminal", "minimal", "chrome", "paper"]) {
    const tokens = themeTokens(css, theme);
    for (const foreground of ["phosphor", "phosphor-bright", "phosphor-dim", "warning", "danger"]) {
      for (const background of ["canvas", "panel", "panel-strong", "control"]) {
        assert.ok(
          contrast(tokens[foreground], tokens[background]) >= 4.5,
          `${theme} ${foreground} on ${background} must meet 4.5:1`,
        );
      }
    }
  }
});

test("keyboard focus enters and leaves optional panels in logical order", async () => {
  render();
  const refresh = document.getElementById("refresh");
  const diagnosticsButton = document.getElementById("open-diagnostics");
  const routing = document.getElementById("open-routing");
  const settingsButton = document.getElementById("open-settings");
  assert.ok(refresh.compareDocumentPosition(diagnosticsButton) &
    window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(diagnosticsButton.compareDocumentPosition(routing) &
    window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(refresh.compareDocumentPosition(routing) & window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.ok(routing.compareDocumentPosition(settingsButton) & window.Node.DOCUMENT_POSITION_FOLLOWING);

  routing.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(document.getElementById("routing").hidden, false);
  assert.equal(document.activeElement, document.getElementById("routing-family"));
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(document.getElementById("routing").hidden, true);
  assert.equal(document.activeElement, routing);

  diagnosticsButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(document.getElementById("diagnostics").hidden, false);
  assert.equal(document.activeElement, document.getElementById("close-diagnostics"));
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(document.getElementById("diagnostics").hidden, true);
  assert.equal(document.activeElement, diagnosticsButton);

  settingsButton.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(document.getElementById("settings").hidden, false);
  assert.equal(document.activeElement, document.getElementById("settings-title-input"));
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(document.getElementById("settings").hidden, true);
  assert.equal(document.activeElement, settingsButton);
});

test("dynamic capacity and update state expose meaningful VoiceOver semantics", () => {
  render();
  const account = document.querySelector("article.account");
  assert.equal(account.getAttribute("aria-labelledby"), "account-claude1-title");
  assert.equal(account.querySelectorAll('[role="meter"][aria-valuetext]').length, 3);
  assert.match(account.querySelector('[role="meter"]').getAttribute("aria-label"), /capacity left/);

  window.__headroomApplyUpdate({
    ...update, phase: "available", available_version: "0.4.1",
    notes: "Accessibility improvements.", code: "update_available",
  });
  const panel = document.getElementById("update-panel");
  assert.equal(panel.getAttribute("aria-live"), "polite");
  assert.equal(panel.getAttribute("aria-busy"), "false");
  assert.equal(document.getElementById("update-install").getAttribute("aria-describedby"),
    "update-status");
});

test("reduced-motion preference disables animated focus scrolling", () => {
  assert.equal(prefersReducedMotion(() => ({ matches: true })), true);
  assert.equal(prefersReducedMotion(() => ({ matches: false })), false);
  const css = readFileSync(new URL("../dist/style.css", import.meta.url), "utf8");
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /animation-duration: 0\.01ms !important/);
  assert.match(css, /transition: none !important/);
});
