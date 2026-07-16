const VALID_STATES = new Set(["current", "limited", "held", "stale"]);
const FRESHNESS_STATES = new Set(["current", "held", "stale"]);
const VALID_MODES = new Set(["ready", "onboarding", "demo", "recovery"]);
const ONBOARDING_STEPS = new Set(["welcome", "providers", "accounts", "demo", "complete"]);
const PROVIDER_STATES = new Set(["unchecked", "ready", "missing", "upgrade_required"]);
const HOME_KINDS = new Set(["headroom", "adopted"]);
const REAUTH_STATES = new Set(["available", "provider_managed", "keychain_manual"]);
const VALID_THEMES = new Set(["midnight", "minimal", "chrome", "paper", "terminal"]);
const VALID_SURFACES = new Set(["main", "popover"]);
const TRUST_STATES = new Set([
  "verified", "verified_local", "verified_remote", "duplicate_identity", "held",
]);

let activeBootstrap = null;
let activeInvoke = null;
let activeRevision = 0;

export function shouldApplySnapshot(currentRevision, incomingRevision) {
  return Number.isInteger(incomingRevision) && incomingRevision > currentRevision;
}

export function shouldApplyCommandResult(baseRevision, currentRevision) {
  return Number.isInteger(baseRevision) && Number.isInteger(currentRevision) &&
    currentRevision <= baseRevision;
}

export function formatReset(epoch, now = Date.now()) {
  const value = Number(epoch);
  if (!Number.isFinite(value)) return { label: "reset unknown", exact: null };
  const target = value * 1000;
  const difference = target - now;
  const exact = new Date(target).toLocaleString();
  if (difference <= 0) return { label: "reset due", exact };
  const minutes = Math.max(1, Math.round(difference / 60_000));
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const remaining = minutes % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (!days && remaining) parts.push(`${remaining}m`);
  return { label: `resets in ${parts.join(" ")}`, exact };
}

export function accountStatePresentation(account) {
  if (account?.reserved) return {
    label: "reserved", action: "Monitored, but excluded from automatic routing",
  };
  if (account?.state === "current") return {
    label: "current", action: "Verified capacity is current",
  };
  if (account?.state === "limited") return {
    label: "limited", action: "Capacity is exhausted; wait for the displayed reset",
  };
  if (account?.state === "stale") return {
    label: "stale", action: "Last verified reading has aged out; refresh when online",
  };
  return {
    label: "held", action: account?.note || "Capacity is not proven; refresh or re-authenticate",
  };
}

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
  reauthenticated: "Account identity verified; prior protective hold cleared",
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

function showDeviceInstructions(device, invoke, raw) {
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
}

async function monitorLoginJob(job, invoke, diagnostic, device) {
  while (job.state === "running" || job.state === "cancelling") {
    diagnostic.textContent = loginMessage(job.progress_code);
    showDeviceInstructions(device, invoke, job.instructions);
    await new Promise((resolve) => setTimeout(resolve, 350));
    job = await invoke("desktop_login_status", { jobId: job.job_id });
  }
  diagnostic.textContent = loginMessage(job.result_code);
  showDeviceInstructions(device, invoke, null);
  return job;
}

export function normalizeBootstrap(raw) {
  if (!raw || raw.bridge?.bridge_schema !== "headroom_desktop_bridge@1") {
    throw new Error("incompatible desktop engine");
  }
  const view = raw.view;
  if (!view || view.schema !== "headroom_desktop_view@1") {
    throw new Error("invalid sanitized desktop view");
  }
  const accounts = Array.isArray(view.accounts) ? view.accounts.map((account) => {
    const rawPolicy = account?.policy && typeof account.policy === "object"
      ? account.policy : null;
    const policy = rawPolicy?.schema === "headroom_account_lifecycle@1" &&
      HOME_KINDS.has(rawPolicy.home_kind) &&
      REAUTH_STATES.has(rawPolicy.reauthentication) ? {
        schema: "headroom_account_lifecycle@1",
        home_kind: rawPolicy.home_kind,
        home_retained_on_remove: rawPolicy.home_retained_on_remove === true,
        rename_keeps_home: rawPolicy.rename_keeps_home === true,
        reauthentication: rawPolicy.reauthentication,
        position: Number.isInteger(rawPolicy.position) && rawPolicy.position >= 0
          ? rawPolicy.position : 0,
        count: Number.isInteger(rawPolicy.count) && rawPolicy.count >= 1
          ? rawPolicy.count : 1,
        can_move_up: rawPolicy.can_move_up === true,
        can_move_down: rawPolicy.can_move_down === true,
        can_remove: rawPolicy.can_remove === true,
      } : null;
    const windows = {};
    if (account?.windows && typeof account.windows === "object") {
      for (const [key, windowValue] of Object.entries(account.windows)) {
        if (typeof key !== "string" || key.length > 64 ||
            !windowValue || typeof windowValue !== "object") continue;
        const state = VALID_STATES.has(windowValue.state) ? windowValue.state : "held";
        const left = Number(windowValue.left_percent);
        const last = Number(windowValue.last_observed_left_percent);
        const resets = Number(windowValue.resets_at);
        windows[key] = {
          state,
          left_percent: Number.isFinite(left) && left >= 0 && left <= 100 ? left : null,
          last_observed_left_percent:
            Number.isFinite(last) && last >= 0 && last <= 100 ? last : null,
          resets_at: Number.isFinite(resets) ? resets : null,
        };
      }
    }
    return {
      name: typeof account?.name === "string" ? account.name : "unknown",
      provider: account?.provider === "claude" || account?.provider === "codex"
        ? account.provider : "unknown",
      identity: typeof account?.identity === "string" ? account.identity : null,
      plan: typeof account?.plan === "string" ? account.plan : "Unknown",
      note: typeof account?.note === "string" ? account.note : null,
      diagnostic_code: typeof account?.diagnostic_code === "string" &&
        /^[a-z0-9_]{1,64}$/.test(account.diagnostic_code)
        ? account.diagnostic_code : null,
      trust_state: TRUST_STATES.has(account?.trust_state) ? account.trust_state : null,
      reserved: account?.reserved === true,
      policy,
      state: VALID_STATES.has(account?.state) ? account.state : "held",
      windows,
    };
  }) : [];
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
  const rawSettings = view.settings && typeof view.settings === "object" ? view.settings : {};
  const settingsTheme = VALID_THEMES.has(rawSettings.theme) ? rawSettings.theme : "terminal";
  const rawFreshness = view.freshness && typeof view.freshness === "object"
    ? view.freshness : {};
  const freshness = {
    state: FRESHNESS_STATES.has(rawFreshness.state) ? rawFreshness.state : "held",
    age_seconds: Number.isFinite(Number(rawFreshness.age_seconds))
      ? Math.max(0, Math.floor(Number(rawFreshness.age_seconds))) : null,
    reason: typeof rawFreshness.reason === "string" ? rawFreshness.reason : "unknown",
  };
  const rawHeadline = view.headline && typeof view.headline === "object" ? view.headline : {};
  const numberOrNull = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
  return {
    bridge: {
      bridge_schema: raw.bridge.bridge_schema,
      product_version: typeof raw.bridge.product_version === "string"
        ? raw.bridge.product_version : "unknown",
      architecture: typeof raw.bridge.architecture === "string"
        ? raw.bridge.architecture : "unknown",
      runtime: raw.bridge.runtime === "frozen" ? "frozen" : "unavailable",
    },
    revision: Number.isInteger(raw.revision) && raw.revision >= 0 ? raw.revision : 0,
    surface: VALID_SURFACES.has(raw.surface) ? raw.surface : "main",
    theme: VALID_THEMES.has(raw.theme) ? raw.theme : settingsTheme,
    view: {
      schema: "headroom_desktop_view@1",
      mode: VALID_MODES.has(view.mode) ? view.mode : "recovery",
      settings: {
        title: typeof rawSettings.title === "string" ? rawSettings.title : "Headroom",
        theme: settingsTheme,
        redact_emails: rawSettings.redact_emails !== false,
        reserve_percent: numberOrNull(rawSettings.reserve_percent) ?? 0,
        auto_handoff: rawSettings.auto_handoff !== false,
      },
      freshness,
      headline: {
        avg_5h_left_percent: numberOrNull(rawHeadline.avg_5h_left_percent),
        avg_7d_left_percent: numberOrNull(rawHeadline.avg_7d_left_percent),
        current_accounts: numberOrNull(rawHeadline.current_accounts),
        total_accounts: numberOrNull(rawHeadline.total_accounts),
      },
      recovery_code: typeof view.recovery_code === "string" ? view.recovery_code : null,
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
  if (label.startsWith("scoped:")) row.classList.add("model-scoped");
  const line = document.createElement("div");
  line.className = "window-line";
  const name = document.createElement("span");
  name.textContent = `> ${label.replace("scoped:", "")}`;
  const reading = document.createElement("strong");
  const left = percentLeft(value);
  const reset = formatReset(value?.resets_at);
  const last = Number(value?.last_observed_left_percent);
  const capacity = left === null
    ? Number.isFinite(last) ? `${value?.state || "held"} · last ${Math.round(last)}% left`
      : value?.state || "held"
    : `${Math.round(left)}% left`;
  reading.textContent = `${capacity} · ${reset.label}`;
  if (reset.exact) reading.title = `Exact local reset: ${reset.exact}`;
  line.append(name, reading);
  const meter = document.createElement("div");
  meter.className = "meter";
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-label", `${label} capacity`);
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", "100");
  if (left !== null) meter.setAttribute("aria-valuenow", String(left));
  meter.setAttribute("aria-valuetext", capacity);
  const fill = document.createElement("i");
  fill.style.width = `${left ?? 0}%`;
  meter.append(fill);
  row.append(line, meter);
  return row;
}

function accountCard(account, lifecycle = null, surface = "main") {
  const article = document.createElement("article");
  article.className = `account state-${account.state}`;
  if (account.reserved) article.classList.add("is-reserved");
  article.dataset.provider = account.provider;
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
  const presentation = accountStatePresentation(account);
  state.textContent = presentation.label;
  state.setAttribute("aria-label", `${presentation.label}: ${presentation.action}`);
  header.append(identity, state);
  const semantics = document.createElement("p");
  semantics.className = "account-semantics";
  semantics.textContent = [
    presentation.action,
    account.trust_state ? `trust ${account.trust_state.replaceAll("_", " ")}` : "trust unverified",
    account.diagnostic_code ? `code ${account.diagnostic_code}` : null,
  ].filter(Boolean).join(" · ");
  const windows = document.createElement("div");
  windows.className = "windows";
  for (const [key, value] of Object.entries(account.windows)) {
    windows.append(windowRow(key, value));
  }
  article.append(header, semantics, windows);
  if (account.note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = account.note;
    article.append(note);
  }
  if (surface === "main" && lifecycle && account.policy) {
    article.append(accountLifecyclePanel(account, lifecycle));
  }
  return article;
}

function accountLifecyclePanel(account, { invoke, update, existingNames }) {
  const details = document.createElement("details");
  details.className = "account-lifecycle";
  const summary = document.createElement("summary");
  summary.textContent = "> manage account";
  const ownership = document.createElement("p");
  ownership.className = "ownership-note";
  ownership.textContent = account.policy.home_kind === "headroom"
    ? "Headroom-managed home · rename keeps its credential path · removal unregisters only"
    : "Provider-managed adopted home · Headroom never changes or deletes its credentials";
  const controls = document.createElement("div");
  controls.className = "lifecycle-controls";
  const diagnostic = document.createElement("p");
  diagnostic.className = "diagnostic lifecycle-diagnostic";
  diagnostic.setAttribute("aria-live", "polite");
  const mutate = async (payload) => {
    diagnostic.textContent = "Applying locked account change…";
    try {
      update(await invoke("desktop_account_action", {
        name: account.name, ...payload,
      }));
    } catch {
      diagnostic.textContent = "Account change was refused or rolled back safely";
    }
  };

  controls.append(actionButton(
    account.reserved ? "Unreserve" : "Reserve",
    () => mutate({
      action: account.reserved ? "unreserve" : "reserve",
      reserved: !account.reserved,
    }),
  ));
  const up = actionButton("Move up", () => mutate({ action: "move_up" }));
  up.disabled = !account.policy.can_move_up;
  const down = actionButton("Move down", () => mutate({ action: "move_down" }));
  down.disabled = !account.policy.can_move_down;
  controls.append(up, down);

  const rename = document.createElement("form");
  rename.className = "lifecycle-form";
  const renameInput = document.createElement("input");
  renameInput.value = account.name;
  renameInput.setAttribute("aria-label", `New slot name for ${account.name}`);
  const renameButton = document.createElement("button");
  renameButton.type = "submit";
  renameButton.textContent = "Rename";
  const validateRename = () => {
    const names = existingNames.filter((name) => name !== account.name);
    const error = renameInput.value === account.name
      ? "Choose a different slot name" : accountNameError(renameInput.value, names);
    renameInput.setCustomValidity(error || "");
    renameButton.disabled = Boolean(error);
    diagnostic.textContent = error || "";
    return !error;
  };
  renameInput.addEventListener("input", validateRename);
  rename.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (validateRename()) {
      await mutate({ action: "rename", newName: renameInput.value });
    }
  });
  rename.append(renameInput, renameButton);
  validateRename();

  const reauthentication = document.createElement("div");
  reauthentication.className = "reauthentication";
  const reauthDiagnostic = document.createElement("p");
  reauthDiagnostic.className = "diagnostic";
  reauthDiagnostic.setAttribute("aria-live", "polite");
  const device = document.createElement("div");
  device.className = "device-instructions";
  device.hidden = true;
  if (account.policy.reauthentication === "available") {
    const reauth = actionButton("Re-authenticate", async () => {
      reauth.disabled = true;
      cancel.hidden = false;
      try {
        let job = await invoke("desktop_start_reauthentication", {
          name: account.name,
        });
        cancel.onclick = async () => {
          cancel.disabled = true;
          job = await invoke("desktop_cancel_login", { jobId: job.job_id });
          reauthDiagnostic.textContent = loginMessage(job.progress_code);
        };
        job = await monitorLoginJob(job, invoke, reauthDiagnostic, device);
        if (job.state === "succeeded" && job.view) update(job.view);
      } catch {
        reauthDiagnostic.textContent =
          "Re-authentication failed safely; prior credentials were restored";
      } finally {
        reauth.disabled = false;
        cancel.hidden = true;
        cancel.disabled = false;
      }
    }, "primary");
    const cancel = actionButton("Cancel", async () => {});
    cancel.hidden = true;
    reauthentication.append(reauth, cancel, device, reauthDiagnostic);
  } else {
    reauthDiagnostic.textContent = account.policy.reauthentication === "keychain_manual"
      ? "Keychain-backed Claude login: re-authenticate in Claude, then refresh here"
      : "Adopted home: re-authenticate in the provider that owns this login";
    reauthentication.append(reauthDiagnostic);
  }

  const removal = document.createElement("form");
  removal.className = "lifecycle-form removal-form";
  const confirmation = document.createElement("input");
  confirmation.placeholder = `type ${account.name} to confirm`;
  confirmation.setAttribute("aria-label", `Type ${account.name} to confirm removal`);
  const remove = document.createElement("button");
  remove.type = "submit";
  remove.textContent = "Remove";
  remove.className = "danger";
  const validateRemoval = () => {
    remove.disabled = !account.policy.can_remove || confirmation.value !== account.name;
  };
  confirmation.addEventListener("input", validateRemoval);
  removal.addEventListener("submit", async (event) => {
    event.preventDefault();
    validateRemoval();
    if (!remove.disabled) {
      await mutate({ action: "remove", confirmation: confirmation.value });
    }
  });
  removal.append(confirmation, remove);
  validateRemoval();
  const removalCopy = document.createElement("p");
  removalCopy.className = "ownership-note";
  removalCopy.textContent = account.policy.can_remove
    ? "Removal keeps the provider home and credentials on disk."
    : "The final connected account cannot be removed.";

  details.append(summary, ownership, controls, rename, reauthentication,
    removal, removalCopy, diagnostic);
  return details;
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
      job = await monitorLoginJob(job, invoke, diagnostic, device);
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

export function refreshStatePresentation(state) {
  if (state === "refreshing") return {
    label: "REFRESHING · verified readings remain visible while providers respond",
    busy: true,
  };
  if (state === "offline") return {
    label: "OFFLINE · refresh failed; retained readings keep their displayed age and trust state",
    busy: false,
  };
  return { label: "", busy: false };
}

function applyRefreshState(state) {
  const presentation = refreshStatePresentation(state);
  document.body.dataset.refreshState = state;
  const status = document.getElementById("surface-status");
  if (presentation.label) status.textContent = presentation.label;
  else if (activeBootstrap) {
    const freshness = activeBootstrap.view.freshness;
    const age = freshness.age_seconds === null ? "age unknown" : `${freshness.age_seconds}s old`;
    status.textContent =
      `${freshness.state.toUpperCase()} · ${age} · ${freshness.reason.replaceAll("_", " ")}`;
  }
  const refresh = document.getElementById("refresh");
  refresh.setAttribute("aria-busy", String(presentation.busy));
  refresh.textContent = presentation.busy ? "Refreshing…" : "Refresh";
  refresh.disabled = presentation.busy || !activeInvoke ||
    activeBootstrap?.view.mode !== "ready";
}

function openAppearancePanel() {
  if (document.body.dataset.surface !== "main") return;
  const panel = document.getElementById("appearance");
  panel.hidden = false;
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  document.getElementById("theme-picker").focus();
}

function configureSurfaceActions(surface, invoke) {
  const actions = document.getElementById("surface-actions");
  const controls = [];
  if (!invoke) {
    actions.replaceChildren();
    return;
  }
  if (surface === "popover") {
    controls.push(
      actionButton("Dashboard", () => invoke("desktop_show_dashboard"), "primary"),
      actionButton("Settings", () => invoke("desktop_show_settings")),
      actionButton("Quit", () => invoke("desktop_quit"), "danger"),
    );
  } else {
    controls.push(actionButton("Appearance", openAppearancePanel));
  }
  actions.replaceChildren(...controls);
}

function configureAppearance(value, invoke) {
  const picker = document.getElementById("theme-picker");
  picker.value = value.theme;
  picker.oninput = invoke ? async () => {
    if (!VALID_THEMES.has(picker.value)) return;
    document.body.dataset.theme = picker.value;
    try { await invoke("desktop_set_theme", { theme: picker.value }); }
    catch { picker.value = activeBootstrap?.theme || "terminal"; }
  } : null;
  document.getElementById("close-appearance").onclick = () => {
    document.getElementById("appearance").hidden = true;
  };
}

export function renderBootstrap(raw, invoke = null) {
  const value = normalizeBootstrap(raw);
  const { view } = value;
  activeBootstrap = value;
  activeInvoke = invoke;
  activeRevision = Math.max(activeRevision, value.revision);
  document.body.dataset.surface = value.surface;
  document.body.dataset.theme = value.theme;
  document.body.dataset.snapshotState = view.freshness.state;
  document.getElementById("engine-badge").textContent = value.bridge.runtime;
  document.body.dataset.savedTheme = view.settings?.theme || "terminal";
  document.getElementById("revision").textContent = `revision ${value.revision}`;
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
  const update = (nextView) => {
    // Native commands publish a newer, revisioned snapshot to both surfaces
    // before their raw view response resolves. Do not let that unrevisioned
    // response repaint the initiating surface with its older closure state.
    if (!shouldApplyCommandResult(value.revision, activeRevision)) return activeBootstrap;
    return renderBootstrap({
      bridge: value.bridge,
      view: nextView,
      revision: value.revision,
      theme: value.theme,
      surface: value.surface,
    }, invoke);
  };
  const existingNames = view.accounts.map((row) => row.name);
  const lifecycle = invoke && view.mode === "ready" && value.surface === "main"
    ? { invoke, update, existingNames } : null;
  const cards = view.accounts.map((account) => accountCard(account, lifecycle, value.surface));
  if (!cards.length && view.mode === "ready") {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = value.surface === "popover"
      ? "No connected accounts · open Dashboard to begin setup"
      : "No connected accounts · begin setup to add Claude or Codex";
    cards.push(empty);
  }
  document.getElementById("accounts").replaceChildren(...cards);
  const actions = document.getElementById("actions");
  let actionCards = [];
  if (value.surface === "main" && invoke && view.mode === "onboarding") {
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
  } else if (value.surface === "main" && invoke && view.mode === "demo") {
    actionCards = [demoPanel(invoke, update)];
  } else if (value.surface === "main" && invoke && view.mode === "ready") {
    actionCards = [
      ...view.candidates.map((row) => candidateCard(row, invoke, update, existingNames)),
      providerLoginCard("claude", invoke, update, existingNames),
      providerLoginCard("codex", invoke, update, existingNames),
    ];
  }
  actions.replaceChildren(...actionCards);
  configureSurfaceActions(value.surface, invoke);
  configureAppearance(value, invoke);
  const freshnessAge = view.freshness.age_seconds === null
    ? "age unknown" : `${view.freshness.age_seconds}s old`;
  document.getElementById("surface-status").textContent =
    `${view.freshness.state.toUpperCase()} · ${freshnessAge} · ${view.freshness.reason.replaceAll("_", " ")}`;
  const refresh = document.getElementById("refresh");
  refresh.disabled = !invoke || view.mode !== "ready";
  refresh.onclick = invoke ? async () => {
    applyRefreshState("refreshing");
    try { update(await invoke("desktop_refresh")); }
    catch { applyRefreshState("offline"); }
    finally {
      if (document.body.dataset.refreshState !== "offline") applyRefreshState("current");
    }
  } : null;
  return value;
}

if (typeof document !== "undefined") {
  window.__headroomApplySnapshot = (snapshot) => {
    if (!activeBootstrap || !shouldApplySnapshot(activeRevision, snapshot?.revision)) return;
    renderBootstrap({
      bridge: activeBootstrap.bridge,
      surface: activeBootstrap.surface,
      revision: snapshot.revision,
      theme: snapshot.theme,
      view: snapshot.view,
    }, activeInvoke);
  };
  window.__headroomSetRefreshState = applyRefreshState;
  window.__headroomOpenPanel = (panel) => {
    if (panel === "appearance") openAppearancePanel();
  };
  try {
    renderBootstrap(window.__HEADROOM_BOOTSTRAP__, window.__TAURI__?.core?.invoke || null);
  } catch (error) {
    document.getElementById("engine-badge").textContent = "unavailable";
    document.getElementById("summary").textContent = error.message;
    document.getElementById("headline").textContent = "The desktop engine did not start safely.";
  }
}
