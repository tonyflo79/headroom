const VALID_STATES = new Set(["current", "limited", "held", "stale"]);

export function percentLeft(windowValue) {
  if (!windowValue || windowValue.state !== "current") {
    return windowValue?.state === "limited" ? 0 : null;
  }
  const value = Number(windowValue.left_percent);
  return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

export function normalizeBootstrap(raw) {
  if (!raw || typeof raw !== "object") throw new Error("missing desktop bootstrap");
  if (raw.bridge?.bridge_schema !== "headroom_desktop_bridge@1") {
    throw new Error("incompatible desktop engine");
  }
  const snapshot = raw.snapshot;
  if (!snapshot || snapshot.schema !== "headroom_widget@1") {
    throw new Error("invalid sanitized snapshot");
  }
  const accounts = Array.isArray(snapshot.accounts)
    ? snapshot.accounts.map((account) => ({
        name: typeof account.name === "string" ? account.name : "unknown",
        provider: account.provider === "claude" || account.provider === "codex"
          ? account.provider
          : "unknown",
        state: VALID_STATES.has(account.state) ? account.state : "held",
        windows: account.windows && typeof account.windows === "object"
          ? account.windows
          : {},
      }))
    : [];
  return { bridge: raw.bridge, snapshot: { ...snapshot, accounts } };
}

function windowRow(label, value) {
  const row = document.createElement("div");
  row.className = "window-row";
  const name = document.createElement("span");
  name.textContent = label;
  const reading = document.createElement("strong");
  const left = percentLeft(value);
  reading.textContent = left === null ? value?.state || "held" : `${Math.round(left)}% left`;
  row.append(name, reading);
  return row;
}

function accountCard(account) {
  const article = document.createElement("article");
  article.className = `account state-${account.state}`;
  const header = document.createElement("header");
  const identity = document.createElement("div");
  const name = document.createElement("h3");
  name.textContent = account.name;
  const provider = document.createElement("p");
  provider.textContent = account.provider;
  identity.append(name, provider);
  const state = document.createElement("span");
  state.className = "state";
  state.textContent = account.state;
  header.append(identity, state);
  const windows = document.createElement("div");
  windows.className = "windows";
  for (const [key, value] of Object.entries(account.windows)) {
    windows.append(windowRow(key.replace("scoped:", ""), value));
  }
  article.append(header, windows);
  return article;
}

export function renderBootstrap(raw) {
  const value = normalizeBootstrap(raw);
  document.getElementById("engine-badge").textContent = value.bridge.runtime;
  document.getElementById("summary").textContent =
    `Engine ${value.bridge.product_version} · ${value.bridge.architecture}`;
  const average = Number(value.snapshot.headline?.avg_5h_left_percent);
  document.getElementById("headline").textContent = Number.isFinite(average)
    ? `${Math.round(average)}% average five-hour headroom`
    : "No current five-hour reading";
  const target = document.getElementById("accounts");
  target.replaceChildren(...value.snapshot.accounts.map(accountCard));
  return value;
}

if (typeof document !== "undefined") {
  try {
    renderBootstrap(window.__HEADROOM_BOOTSTRAP__);
  } catch (error) {
    document.getElementById("engine-badge").textContent = "unavailable";
    document.getElementById("summary").textContent = error.message;
    document.getElementById("headline").textContent =
      "The desktop engine did not start safely.";
  }
}
