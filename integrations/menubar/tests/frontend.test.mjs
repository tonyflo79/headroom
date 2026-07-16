import assert from "node:assert/strict";
import test from "node:test";

import { normalizeBootstrap, percentLeft } from "../dist/main.js";

const bootstrap = {
  bridge: {
    bridge_schema: "headroom_desktop_bridge@1",
    product_version: "0.4.0",
    architecture: "arm64",
    runtime: "frozen",
  },
  snapshot: {
    schema: "headroom_widget@1",
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
  assert.equal(value.snapshot.accounts[0].name, "personal");
  assert.equal(value.snapshot.accounts[0].provider, "claude");
});

test("rejects an incompatible bridge", () => {
  assert.throws(
    () => normalizeBootstrap({ ...bootstrap, bridge: { bridge_schema: "other" } }),
    /incompatible desktop engine/,
  );
});

test("does not trust malformed state or percentages", () => {
  const malformed = structuredClone(bootstrap);
  malformed.snapshot.accounts[0].state = "pretend-live";
  assert.equal(normalizeBootstrap(malformed).snapshot.accounts[0].state, "held");
  assert.equal(percentLeft({ state: "current", left_percent: 101 }), null);
  assert.equal(percentLeft({ state: "limited", left_percent: 50 }), 0);
});
