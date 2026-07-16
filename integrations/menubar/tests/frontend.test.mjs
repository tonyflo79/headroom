import assert from "node:assert/strict";
import test from "node:test";

import {
  accountNameError, loginMessage, normalizeBootstrap, normalizeDeviceInstructions,
  onboardingPresentation, percentLeft, refreshPresentation, suggestedAccountName,
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
