import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeBootstrap, percentLeft, refreshPresentation,
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
