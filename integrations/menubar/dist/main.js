const VALID_STATES = new Set(["current", "limited", "held", "stale"]);
const VALID_MODES = new Set(["ready", "empty", "recovery"]);

export function percentLeft(windowValue) {
  if (!windowValue || windowValue.state !== "current") {
    return windowValue?.state === "limited" ? 0 : null;
  }
  const value = Number(windowValue.left_percent);
  return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

export function refreshPresentation(busy) {
  return { label: busy ? "Refreshing…" : "Refresh", busy: busy === true };
}

const LOGIN_MESSAGES = {
  queued: "Queued",
  preflight: "Checking provider CLI prerequisite",
  browser_login: "Waiting for Claude browser sign-in",
  verifying_identity: "Verifying account identity",
  publishing: "Publishing the verified account",
  cancelling: "Cancelling and restoring prior credentials",
  complete: "Complete",
  connected: "Account connected",
  cancelled: "Login cancelled; prior credentials restored",
  login_timed_out: "Login timed out; prior credentials restored",
  provider_login_failed: "Claude login failed; prior credentials restored",
  identity_unreadable: "Claude completed but identity could not be verified",
  wrong_identity: "Signed-in identity did not match; credentials restored",
  duplicate_identity: "That identity is already connected",
  claude_cli_missing: "Install Claude Code before connecting",
  claude_upgrade_required: "Update Claude Code to a version with auth login support",
  claude_shared_keychain_conflict: "Login refused: an existing slot uses the legacy shared Keychain item",
  claude_keychain_isolation_missing: "Claude did not create an isolated Keychain item; update Claude Code",
  claude_slot_keychain_occupied: "Login refused: this unused slot name already has a Keychain item",
  device_code: "Open OpenAI and enter the one-time code",
  codex_cli_missing: "Install Codex before connecting",
  codex_upgrade_required: "Update Codex to a version with structured device authentication",
  device_code_expired: "The device code expired; no account was added",
  device_authorization_rejected: "OpenAI rejected the device authorization",
  device_instructions_malformed: "Codex returned unsafe device instructions; login stopped",
  api_key_not_subscription: "API-key authentication has no ChatGPT subscription capacity",
  identity_rejected: "The Codex identity was rejected; credentials restored",
  identity_malformed: "Codex returned an unverifiable identity; credentials restored",
  identity_verification_failed: "Live Codex verification failed; credentials restored",
  internal_error: "Login failed safely; prior credentials were restored",
};

export function loginMessage(code) {
  return LOGIN_MESSAGES[code] || "Login could not be completed safely";
}

export function normalizeDeviceInstructions(value) {
  if (!value || typeof value.verification_url !== "string" ||
      typeof value.user_code !== "string" ||
      !/^[A-Z0-9-]{4,32}$/.test(value.user_code)) return null;
  try {
    const url = new URL(value.verification_url);
    if (url.protocol !== "https:" || url.hostname !== "auth.openai.com" ||
        url.username || url.password || (url.port && url.port !== "443") ||
        url.pathname !== "/codex/device" || url.search || url.hash) return null;
  } catch { return null; }
  return { verification_url: value.verification_url, user_code: value.user_code };
}

export function normalizeBootstrap(raw) {
  if (!raw || raw.bridge?.bridge_schema !== "headroom_desktop_bridge@1") {
    throw new Error("incompatible desktop engine");
  }
  const view = raw.view;
  if (!view || view.schema !== "headroom_desktop_view@1") {
    throw new Error("invalid sanitized desktop view");
  }
  const accounts = Array.isArray(view.accounts) ? view.accounts.map((account) => ({
    name: typeof account?.name === "string" ? account.name : "unknown",
    provider: account?.provider === "claude" || account?.provider === "codex"
      ? account.provider : "unknown",
    identity: typeof account?.identity === "string" ? account.identity : null,
    plan: typeof account?.plan === "string" ? account.plan : "Unknown",
    note: typeof account?.note === "string" ? account.note : null,
    reserved: account?.reserved === true,
    state: VALID_STATES.has(account?.state) ? account.state : "held",
    windows: account?.windows && typeof account.windows === "object"
      ? account.windows : {},
  })) : [];
  const candidates = Array.isArray(view.candidates) ? view.candidates.filter((row) =>
    typeof row?.id === "string" && typeof row?.identity === "string" &&
    (row.provider === "claude" || row.provider === "codex")) : [];
  return {
    bridge: raw.bridge,
    view: { ...view, mode: VALID_MODES.has(view.mode) ? view.mode : "recovery",
      accounts, candidates },
  };
}

function windowRow(label, value) {
  const row = document.createElement("div");
  row.className = `window-row window-${VALID_STATES.has(value?.state) ? value.state : "held"}`;
  const line = document.createElement("div");
  line.className = "window-line";
  const name = document.createElement("span");
  name.textContent = `> ${label.replace("scoped:", "")}`;
  const reading = document.createElement("strong");
  const left = percentLeft(value);
  const reset = Number(value?.resets_at);
  const resetText = Number.isFinite(reset)
    ? ` · resets ${new Date(reset * 1000).toLocaleString()}` : "";
  reading.textContent = `${left === null ? value?.state || "held" : `${Math.round(left)}% left`}${resetText}`;
  line.append(name, reading);
  const meter = document.createElement("div");
  meter.className = "meter";
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-label", `${label} capacity`);
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", "100");
  if (left !== null) meter.setAttribute("aria-valuenow", String(left));
  const fill = document.createElement("i");
  fill.style.width = `${left ?? 0}%`;
  meter.append(fill);
  row.append(line, meter);
  return row;
}

function accountCard(account) {
  const article = document.createElement("article");
  article.className = `account state-${account.state}`;
  const header = document.createElement("header");
  const identity = document.createElement("div");
  const name = document.createElement("h3");
  name.textContent = account.name;
  const detail = document.createElement("p");
  detail.textContent = [account.provider, account.identity, account.plan,
    account.reserved ? "reserved" : null].filter(Boolean).join(" · ");
  identity.append(name, detail);
  const state = document.createElement("span");
  state.className = "state";
  state.textContent = account.state;
  header.append(identity, state);
  const windows = document.createElement("div");
  windows.className = "windows";
  for (const [key, value] of Object.entries(account.windows)) {
    windows.append(windowRow(key, value));
  }
  article.append(header, windows);
  if (account.note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = account.note;
    article.append(note);
  }
  return article;
}

function candidateCard(candidate, invoke, update) {
  const form = document.createElement("form");
  form.className = "candidate";
  const text = document.createElement("span");
  text.textContent = `${candidate.provider} · ${candidate.identity}`;
  const input = document.createElement("input");
  input.required = true;
  input.pattern = "[a-z0-9][a-z0-9_-]{0,31}";
  input.value = `${candidate.provider}-1`;
  input.setAttribute("aria-label", `Slot name for ${candidate.provider}`);
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "Adopt";
  form.append(text, input, button);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    button.textContent = "Adopting…";
    try {
      update(await invoke("desktop_adopt", {
        candidateId: candidate.id, name: input.value,
      }));
    } catch (error) {
      button.textContent = String(error);
    } finally {
      button.disabled = false;
    }
  });
  return form;
}

function providerLoginCard(provider, invoke, update) {
  const form = document.createElement("form");
  form.className = "provider-login";
  const title = document.createElement("strong");
  const label = provider === "claude" ? "Claude" : "Codex";
  title.textContent = `> connect new ${label} login`;
  const fields = document.createElement("div");
  fields.className = "login-fields";
  const name = document.createElement("input");
  name.required = true;
  name.pattern = "[a-z0-9][a-z0-9_-]{0,31}";
  name.value = `${provider}-new`;
  name.placeholder = "slot name";
  name.setAttribute("aria-label", `${label} slot name`);
  const expected = document.createElement("input");
  expected.type = "email";
  expected.placeholder = "expected email (optional)";
  expected.setAttribute("aria-label", `Expected ${label} email, optional`);
  const start = document.createElement("button");
  start.type = "submit";
  start.textContent = "Connect";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.hidden = true;
  const diagnostic = document.createElement("p");
  diagnostic.className = "diagnostic";
  diagnostic.setAttribute("aria-live", "polite");
  const device = document.createElement("div");
  device.className = "device-instructions";
  device.hidden = true;
  fields.append(name, expected, start, cancel);
  form.append(title, fields, device, diagnostic);

  const showInstructions = (raw) => {
    const instructions = normalizeDeviceInstructions(raw);
    if (!instructions) { device.hidden = true; device.replaceChildren(); return; }
    const code = document.createElement("code");
    code.textContent = instructions.user_code;
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = "Open OpenAI";
    open.onclick = () => invoke("desktop_open_device_url", {
      url: instructions.verification_url,
    });
    device.replaceChildren(code, open);
    device.hidden = false;
  };
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    start.disabled = true;
    name.disabled = true;
    expected.disabled = true;
    try {
      const command = provider === "claude"
        ? "desktop_start_claude_login" : "desktop_start_codex_login";
      let job = await invoke(command, {
        name: name.value, expectedEmail: expected.value || null,
      });
      cancel.hidden = false;
      cancel.onclick = async () => {
        cancel.disabled = true;
        job = await invoke("desktop_cancel_login", { jobId: job.job_id });
        diagnostic.textContent = loginMessage(job.progress_code);
      };
      while (job.state === "running" || job.state === "cancelling") {
        diagnostic.textContent = loginMessage(job.progress_code);
        showInstructions(job.instructions);
        await new Promise((resolve) => setTimeout(resolve, 350));
        job = await invoke("desktop_login_status", { jobId: job.job_id });
      }
      diagnostic.textContent = loginMessage(job.result_code);
      showInstructions(null);
      if (job.state === "succeeded" && job.view) update(job.view);
    } catch (error) {
      diagnostic.textContent = String(error);
    } finally {
      start.disabled = false;
      name.disabled = false;
      expected.disabled = false;
      cancel.hidden = true;
      cancel.disabled = false;
    }
  });
  return form;
}

export function renderBootstrap(raw, invoke = null) {
  const value = normalizeBootstrap(raw);
  const { view } = value;
  document.getElementById("engine-badge").textContent = value.bridge.runtime;
  document.body.dataset.savedTheme = view.settings?.theme || "midnight";
  document.getElementById("page-title").textContent = view.settings?.title || "Headroom";
  document.getElementById("summary").textContent =
    `Engine ${value.bridge.product_version} · ${value.bridge.architecture}`;
  const average = typeof view.headline?.avg_5h_left_percent === "number"
    ? view.headline.avg_5h_left_percent : Number.NaN;
  document.getElementById("headline").textContent = view.mode === "recovery"
    ? `Safe recovery required (${view.recovery_code || "unknown"}); no files were changed`
    : Number.isFinite(average) ? `${Math.round(average)}% average five-hour headroom`
      : view.mode === "empty" ? "Adopt an existing login to begin" : "No current five-hour reading";
  document.getElementById("accounts").replaceChildren(...view.accounts.map(accountCard));
  const actions = document.getElementById("actions");
  const update = (nextView) => renderBootstrap({ bridge: value.bridge, view: nextView }, invoke);
  const actionCards = invoke && view.mode !== "recovery" ? [
    ...view.candidates.map((row) => candidateCard(row, invoke, update)),
    providerLoginCard("claude", invoke, update),
    providerLoginCard("codex", invoke, update),
  ] : [];
  actions.replaceChildren(...actionCards);
  const refresh = document.getElementById("refresh");
  refresh.disabled = !invoke || view.mode !== "ready";
  refresh.onclick = invoke ? async () => {
    const pending = refreshPresentation(true);
    refresh.disabled = pending.busy;
    refresh.setAttribute("aria-busy", String(pending.busy));
    refresh.textContent = pending.label;
    try { update(await invoke("desktop_refresh")); }
    catch (error) { document.getElementById("headline").textContent = String(error); }
    finally {
      const settled = refreshPresentation(false);
      refresh.textContent = settled.label;
      refresh.setAttribute("aria-busy", String(settled.busy));
      refresh.disabled = false;
    }
  } : null;
  return value;
}

if (typeof document !== "undefined") {
  try {
    renderBootstrap(window.__HEADROOM_BOOTSTRAP__, window.__TAURI__?.core?.invoke || null);
  } catch (error) {
    document.getElementById("engine-badge").textContent = "unavailable";
    document.getElementById("summary").textContent = error.message;
    document.getElementById("headline").textContent = "The desktop engine did not start safely.";
  }
}
