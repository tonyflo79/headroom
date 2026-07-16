const VALID_STATES = new Set(["current", "limited", "held", "stale"]);
const VALID_MODES = new Set(["ready", "onboarding", "demo", "recovery"]);
const ONBOARDING_STEPS = new Set(["welcome", "providers", "accounts", "demo", "complete"]);
const PROVIDER_STATES = new Set(["unchecked", "ready", "missing", "upgrade_required"]);

export function accountNameError(value, existingNames = []) {
  if (typeof value !== "string" || !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(value)) {
    return "Use lowercase letters, digits, - or _ (32 characters maximum)";
  }
  if (new Set(existingNames).has(value)) return "That slot name is already in use";
  return null;
}

export function suggestedAccountName(provider, existingNames = [], suffix = "1") {
  const base = provider === "claude" || provider === "codex" ? provider : "account";
  const used = new Set(existingNames);
  const preferred = `${base}-${suffix}`;
  if (!used.has(preferred)) return preferred;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base}-${index}`;
    if (!used.has(candidate)) return candidate;
  }
  return `${base}-${Date.now().toString(36).slice(-6)}`;
}

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
  const rawOnboarding = view.onboarding && typeof view.onboarding === "object"
    ? view.onboarding : {};
  const step = ONBOARDING_STEPS.has(rawOnboarding.step)
    ? rawOnboarding.step : (view.mode === "ready" ? "complete" : "welcome");
  const providers = Array.isArray(rawOnboarding.providers)
    ? rawOnboarding.providers.filter((row) =>
      (row?.provider === "claude" || row?.provider === "codex") &&
      PROVIDER_STATES.has(row?.state)).map((row) => ({
        provider: row.provider,
        state: row.state,
        candidate_available: row.candidate_available === true,
        connected_count: Number.isInteger(row.connected_count) && row.connected_count >= 0
          ? row.connected_count : 0,
      })) : [];
  return {
    bridge: raw.bridge,
    view: { ...view, mode: VALID_MODES.has(view.mode) ? view.mode : "recovery",
      accounts, candidates, onboarding: {
        schema: "headroom_desktop_onboarding@1", step,
        resumable: rawOnboarding.resumable === true,
        recovery_code: typeof rawOnboarding.recovery_code === "string"
          ? rawOnboarding.recovery_code : null,
        providers,
      } },
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

function candidateCard(candidate, invoke, update, existingNames = []) {
  const form = document.createElement("form");
  form.className = "candidate";
  const text = document.createElement("span");
  text.textContent = `${candidate.provider} · ${candidate.identity}`;
  const input = document.createElement("input");
  input.required = true;
  input.pattern = "[a-z0-9][a-z0-9_-]{0,31}";
  input.value = suggestedAccountName(candidate.provider, existingNames);
  input.setAttribute("aria-label", `Slot name for ${candidate.provider}`);
  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = "Adopt";
  const diagnostic = document.createElement("p");
  diagnostic.className = "diagnostic inline-diagnostic";
  diagnostic.setAttribute("aria-live", "polite");
  const validate = () => {
    const error = accountNameError(input.value, existingNames);
    input.setCustomValidity(error || "");
    diagnostic.textContent = error || "";
    button.disabled = Boolean(error);
    return !error;
  };
  input.addEventListener("input", validate);
  form.append(text, input, button, diagnostic);
  validate();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!validate()) return;
    button.disabled = true;
    button.textContent = "Adopting…";
    try {
      update(await invoke("desktop_adopt", {
        candidateId: candidate.id, name: input.value,
      }));
    } catch {
      diagnostic.textContent = "Account adoption could not be completed safely";
    } finally {
      button.textContent = "Adopt";
      button.disabled = false;
    }
  });
  return form;
}

function providerLoginCard(provider, invoke, update, existingNames = []) {
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
  name.value = suggestedAccountName(provider, existingNames, "new");
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

  let running = false;
  const validate = () => {
    const error = accountNameError(name.value, existingNames);
    name.setCustomValidity(error || "");
    if (!running) {
      diagnostic.textContent = error || "";
      start.disabled = Boolean(error);
    }
    return !error;
  };
  name.addEventListener("input", validate);
  validate();

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
    if (!validate()) return;
    running = true;
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
    } catch {
      diagnostic.textContent = loginMessage("internal_error");
    } finally {
      running = false;
      start.disabled = false;
      name.disabled = false;
      expected.disabled = false;
      cancel.hidden = true;
      cancel.disabled = false;
    }
  });
  return form;
}

export function onboardingPresentation(onboarding) {
  const step = ONBOARDING_STEPS.has(onboarding?.step) ? onboarding.step : "welcome";
  if (step === "providers") return {
    title: "> provider readiness",
    headline: "Choose which provider accounts to use",
  };
  if (step === "accounts") return {
    title: "> add accounts",
    headline: "Adopt an existing login or connect a new one",
  };
  if (step === "demo") return {
    title: "> demo mode",
    headline: "Bundled sample data · no provider access",
  };
  if (step === "complete") return {
    title: "> setup complete",
    headline: "Your verified dashboard is ready",
  };
  return {
    title: "> welcome to headroom",
    headline: "Review privacy and credential ownership before setup",
  };
}

function actionButton(label, handler, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  button.onclick = async () => {
    button.disabled = true;
    try { await handler(); }
    catch {
      document.getElementById("headline").textContent =
        "Setup action could not be completed safely";
    }
    finally { button.disabled = false; }
  };
  return button;
}

function providerReadinessCard(provider) {
  const card = document.createElement("article");
  card.className = `provider-readiness provider-${provider.state}`;
  const name = document.createElement("strong");
  name.textContent = provider.provider === "claude" ? "Claude" : "Codex";
  const state = document.createElement("span");
  state.className = "state";
  state.textContent = provider.state.replace("_", " ");
  const detail = document.createElement("p");
  const messages = {
    unchecked: "Not checked yet",
    ready: "CLI supports a verified desktop login",
    missing: "CLI not found; demo and the other provider remain available",
    upgrade_required: "Installed CLI must be updated before a new login",
  };
  detail.textContent = messages[provider.state] || messages.unchecked;
  if (provider.candidate_available) detail.textContent += " · existing login available";
  card.append(name, state, detail);
  return card;
}

function prerequisiteCard(provider) {
  const card = document.createElement("article");
  card.className = "provider-prerequisite";
  const label = provider.provider === "claude" ? "Claude Code" : "Codex";
  const title = document.createElement("strong");
  title.textContent = `> ${label} unavailable for a new login`;
  const detail = document.createElement("p");
  detail.textContent = provider.state === "missing"
    ? `Install ${label}, then return to provider readiness.`
    : `Update ${label} to a supported version, then return to provider readiness.`;
  card.append(title, detail);
  return card;
}

function onboardingPanel(view, invoke, update) {
  const onboarding = view.onboarding;
  const presentation = onboardingPresentation(onboarding);
  const panel = document.createElement("section");
  panel.className = "onboarding-panel";
  panel.setAttribute("aria-labelledby", "onboarding-title");
  const title = document.createElement("h3");
  title.id = "onboarding-title";
  title.textContent = presentation.title;
  panel.append(title);

  const run = async (action) => update(await invoke("desktop_onboarding", { action }));
  const controls = document.createElement("div");
  controls.className = "onboarding-controls";
  if (onboarding.step === "welcome") {
    const intro = document.createElement("p");
    intro.textContent = "Headroom keeps its registry and usage snapshots on this Mac.";
    const disclosures = document.createElement("ul");
    for (const copy of [
      "Claude and Codex keep ownership of their own credential files and Keychain items.",
      "After you continue, Headroom may ask provider CLIs for identity and subscription capacity.",
      "Account routing is optional and can be configured after setup.",
      "Raw credentials and provider payloads never enter this dashboard.",
    ]) {
      const item = document.createElement("li");
      item.textContent = copy;
      disclosures.append(item);
    }
    const recovery = onboarding.recovery_code ? document.createElement("p") : null;
    if (recovery) {
      recovery.className = "onboarding-warning";
      recovery.textContent = "Prior setup progress could not be trusted; setup restarted safely.";
    }
    panel.append(intro, disclosures);
    if (recovery) panel.append(recovery);
    controls.append(
      actionButton("Begin setup", () => run("begin"), "primary"),
      actionButton("Explore demo", () => run("demo")),
    );
  } else if (onboarding.step === "providers") {
    const copy = document.createElement("p");
    copy.textContent = "Provider checks reveal only readiness—not paths, versions, or credentials.";
    const grid = document.createElement("div");
    grid.className = "provider-grid";
    grid.append(...onboarding.providers.map(providerReadinessCard));
    panel.append(copy, grid);
    controls.append(
      actionButton("Choose accounts", () => run("accounts"), "primary"),
      actionButton("View demo", () => run("demo")),
      actionButton("Back", () => run("back")),
    );
  } else if (onboarding.step === "accounts") {
    const copy = document.createElement("p");
    copy.textContent = "Add Claude, Codex, both, or use demo mode with neither provider.";
    panel.append(copy);
    controls.append(
      actionButton("View demo", () => run("demo")),
      actionButton("Back", () => run("back")),
    );
  }
  panel.append(controls);
  return panel;
}

function demoPanel(invoke, update) {
  const panel = document.createElement("section");
  panel.className = "demo-panel";
  const title = document.createElement("strong");
  title.textContent = "> sample fleet only";
  const copy = document.createElement("p");
  copy.textContent = "These bundled readings are illustrative. No provider CLI, account, credential, or network read was used.";
  panel.append(title, copy, actionButton("Set up real accounts", async () => {
    update(await invoke("desktop_onboarding", { action: "back" }));
  }, "primary"));
  return panel;
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
  const presentation = onboardingPresentation(view.onboarding);
  document.getElementById("fleet-title").textContent = view.mode === "onboarding"
    ? "$ headroom setup" : view.mode === "demo" ? "$ headroom demo" : "$ headroom status";
  document.getElementById("headline").textContent = view.mode === "recovery"
    ? `Safe recovery required (${view.recovery_code || "unknown"}); no files were changed`
    : view.mode === "onboarding" || view.mode === "demo" ? presentation.headline
      : Number.isFinite(average) ? `${Math.round(average)}% average five-hour headroom`
        : "No current five-hour reading";
  document.getElementById("accounts").replaceChildren(...view.accounts.map(accountCard));
  const actions = document.getElementById("actions");
  const update = (nextView) => renderBootstrap({ bridge: value.bridge, view: nextView }, invoke);
  const existingNames = view.accounts.map((row) => row.name);
  let actionCards = [];
  if (invoke && view.mode === "onboarding") {
    actionCards = [onboardingPanel(view, invoke, update)];
    if (view.onboarding.step === "accounts") {
      actionCards.push(...view.candidates.map((row) =>
        candidateCard(row, invoke, update, existingNames)));
      for (const provider of view.onboarding.providers) {
        actionCards.push(provider.state === "ready"
          ? providerLoginCard(provider.provider, invoke, update, existingNames)
          : prerequisiteCard(provider));
      }
    }
  } else if (invoke && view.mode === "demo") {
    actionCards = [demoPanel(invoke, update)];
  } else if (invoke && view.mode === "ready") {
    actionCards = [
      ...view.candidates.map((row) => candidateCard(row, invoke, update, existingNames)),
      providerLoginCard("claude", invoke, update, existingNames),
      providerLoginCard("codex", invoke, update, existingNames),
    ];
  }
  actions.replaceChildren(...actionCards);
  const refresh = document.getElementById("refresh");
  refresh.disabled = !invoke || view.mode !== "ready";
  refresh.onclick = invoke ? async () => {
    const pending = refreshPresentation(true);
    refresh.disabled = pending.busy;
    refresh.setAttribute("aria-busy", String(pending.busy));
    refresh.textContent = pending.label;
    try { update(await invoke("desktop_refresh")); }
    catch { document.getElementById("headline").textContent =
      "Refresh could not be completed safely"; }
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
