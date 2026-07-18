const VALID_STATES = new Set(["current", "limited", "held", "stale"]);
const FRESHNESS_STATES = new Set(["current", "held", "stale"]);
const VALID_MODES = new Set(["ready", "onboarding", "demo", "recovery"]);
const ONBOARDING_STEPS = new Set(["welcome", "providers", "accounts", "demo", "complete"]);
const PROVIDER_STATES = new Set(["unchecked", "ready", "missing", "upgrade_required"]);
const HOME_KINDS = new Set(["headroom", "adopted"]);
const REAUTH_STATES = new Set(["available", "provider_managed", "keychain_manual"]);
const RECOVERY_ACTIONS = new Set(["external_reauthentication"]);
const VALID_THEMES = new Set(["midnight", "minimal", "chrome", "paper", "terminal"]);
const VALID_TERMINALS = new Set(["terminal", "iterm", "warp"]);
const VALID_SURFACES = new Set(["main", "popover"]);
const REFRESH_INTERVAL_MIN = 60;
const REFRESH_INTERVAL_MAX = 3600;
const ROUTING_SCHEMA = "headroom_desktop_routing@1";
const HANDOFF_HEALTH_SCHEMA = "headroom_handoff_health@1";
const ACTIVITY_SCHEMA = "headroom_daily_burn@1";
const ACTIVITY_PERIODS = ["today", "7d", "30d"];
const ACTIVITY_COVERAGE = new Set(["exact", "partial", "unavailable"]);
const ACTIVITY_STATUS = new Set(["indexing", "refreshing", "ready", "unavailable"]);
const ACTIVITY_WARNINGS = new Set([
  "claude_history_unattributed", "codex_legacy_usage_unavailable",
  "source_read_incomplete",
]);
const ACTIVITY_DRIVERS = new Set([
  "shipping", "research", "review", "video", "planning", "admin",
  "support", "writing", "unlabeled",
]);
const MAX_ACTIVITY_VALUE = 1_000_000_000_000_000;
const UPDATE_SCHEMA = "headroom_desktop_update@1";
const UPDATE_PHASES = new Set([
  "not_checked", "checking", "current", "available", "downloading",
  "ready_to_restart", "failed",
]);
const UPDATE_CODES = new Set([
  "update_not_checked", "update_checking", "update_current", "update_available",
  "update_downloading", "update_installed", "update_metadata_invalid",
  "update_check_failed", "update_changed", "update_verification_or_install_failed",
]);
const HANDOFF_STATES = new Set([
  "configured", "unavailable", "downgraded", "armed",
  "supervision_lost", "loop_guard", "disabled",
]);
const HANDOFF_ACTIONS = new Set([
  "none", "upgrade_engine", "install_claude_cli", "inspect_diagnostics",
  "use_compatible_interactive_launch", "inspect_handoff_health",
  "start_new_session", "enable_handoff", "wait_for_session",
]);
const ROUTING_FAMILIES = new Set(["claude", "opus", "sonnet", "haiku", "fable", "codex"]);
const ROUTING_CODES = new Set([
  "selected", "available", "reserved", "routing_disabled", "leased",
  "quarantined", "infrastructure_unavailable", "cooled_down",
  "capacity_unavailable", "stale_reading", "authentication_required",
  "unverified_reading", "provider_cli_missing",
]);
const ROUTING_ACTIONS = new Set([
  "copy_or_open", "none", "unreserve_account", "enable_routing",
  "close_other_session", "reauthenticate_account", "inspect_diagnostics",
  "wait_for_reset", "refresh_capacity", "refresh_or_reauthenticate",
  "install_provider_cli", "connect_account",
]);
const TRUST_STATES = new Set([
  "verified", "verified_local", "verified_remote", "duplicate_identity", "held",
]);
const TRANSIENT_CODES = new Set([
  "provider_rate_limited", "provider_server_error", "provider_timeout",
  "provider_offline", "provider_unavailable", "codex_provider_backoff",
  "codex_app_server_throttled", "usage_source_rate_limited",
]);

let activeBootstrap = null;
let activeInvoke = null;
let activeRevision = 0;
let activeRoutingPreview = null;
let activityPollTimer = null;
let activeUpdate = null;
let updateInitialized = false;

function boundedDisplayString(value, maximum, { nullable = false } = {}) {
  if (nullable && value === null) return null;
  if (typeof value !== "string" || value.length < 1 || value.length > maximum ||
      /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)) {
    throw new Error("invalid bounded display string");
  }
  return value;
}

export function normalizeUpdate(raw) {
  const expected = [
    "schema", "channel", "current_version", "phase", "available_version", "notes", "code",
  ];
  const keys = Object.keys(raw || {});
  if (keys.length !== expected.length || expected.some((key) => !keys.includes(key)) ||
      raw.schema !== UPDATE_SCHEMA || !new Set(["stable", "prerelease"]).has(raw.channel) ||
      !UPDATE_PHASES.has(raw.phase) || !UPDATE_CODES.has(raw.code)) {
    throw new Error("invalid desktop update contract");
  }
  const currentVersion = boundedDisplayString(raw.current_version, 64);
  const availableVersion = boundedDisplayString(raw.available_version, 64, { nullable: true });
  const notes = boundedDisplayString(raw.notes, 2000, { nullable: true });
  const requiresVersion = new Set(["available", "downloading", "ready_to_restart"]);
  const phaseCodes = {
    not_checked: new Set(["update_not_checked"]), checking: new Set(["update_checking"]),
    current: new Set(["update_current"]), available: new Set(["update_available"]),
    downloading: new Set(["update_downloading"]),
    ready_to_restart: new Set(["update_installed"]),
    failed: new Set([
      "update_metadata_invalid", "update_check_failed", "update_changed",
      "update_verification_or_install_failed",
    ]),
  };
  if ((requiresVersion.has(raw.phase) && availableVersion === null) ||
      (!requiresVersion.has(raw.phase) && raw.phase !== "failed" && availableVersion !== null) ||
      (notes !== null && availableVersion === null) || !phaseCodes[raw.phase].has(raw.code)) {
    throw new Error("invalid desktop update state");
  }
  return {
    schema: UPDATE_SCHEMA, channel: raw.channel, current_version: currentVersion,
    phase: raw.phase, available_version: availableVersion, notes, code: raw.code,
  };
}

function renderUpdate(raw, invoke) {
  const update = normalizeUpdate(raw);
  activeUpdate = update;
  const panel = document.getElementById("update-panel");
  const title = document.getElementById("update-title");
  const notes = document.getElementById("update-notes");
  const install = document.getElementById("update-install");
  const restart = document.getElementById("update-restart");
  const later = document.getElementById("update-later");
  const check = document.getElementById("check-update");
  const status = document.getElementById("update-status");
  const busy = update.phase === "checking" || update.phase === "downloading";
  check.disabled = !invoke || busy;
  status.textContent = update.phase === "failed" ? ({
    update_metadata_invalid: "invalid update metadata",
    update_changed: "update changed · check again",
    update_verification_or_install_failed: "install failed · current app usable",
  })[update.code] || "check unavailable" : ({
    not_checked: "not checked", checking: "checking…", current: "current",
    available: `${update.available_version} available`, downloading: "verifying + installing…",
    ready_to_restart: "restart ready",
  })[update.phase];
  panel.hidden = !["available", "downloading", "ready_to_restart"].includes(update.phase);
  title.textContent = update.phase === "ready_to_restart"
    ? `Headroom ${update.available_version} is installed`
    : update.phase === "downloading" ? `Installing Headroom ${update.available_version}`
      : `Headroom ${update.available_version} is available`;
  notes.textContent = update.notes || "Signed release from the configured Headroom channel.";
  install.hidden = update.phase === "ready_to_restart";
  install.disabled = update.phase !== "available" || !invoke;
  install.textContent = install.dataset.confirmVersion === update.available_version
    ? "Confirm install" : "Install update";
  restart.hidden = update.phase !== "ready_to_restart";
  restart.disabled = update.phase !== "ready_to_restart" || !invoke;
  restart.textContent = restart.dataset.confirmVersion === update.available_version
    ? "Confirm restart" : "Restart Headroom";
  later.hidden = update.phase === "downloading";
  check.onclick = invoke ? async () => {
    try { renderUpdate(await invoke("desktop_check_for_update"), invoke); }
    catch { status.textContent = "check unavailable"; }
  } : null;
  install.onclick = invoke ? async () => {
    if (install.dataset.confirmVersion !== update.available_version) {
      install.dataset.confirmVersion = update.available_version;
      install.textContent = "Confirm install";
      status.textContent = `confirm signed ${update.available_version} install`;
      return;
    }
    delete install.dataset.confirmVersion;
    install.disabled = true;
    try { renderUpdate(await invoke("desktop_install_update", { confirmed: true }), invoke); }
    catch { status.textContent = "install failed · current app unchanged"; }
  } : null;
  restart.onclick = invoke ? async () => {
    if (restart.dataset.confirmVersion !== update.available_version) {
      restart.dataset.confirmVersion = update.available_version;
      restart.textContent = "Confirm restart";
      status.textContent = "confirm restart to finish update";
      return;
    }
    delete restart.dataset.confirmVersion;
    restart.disabled = true;
    try { await invoke("desktop_restart_after_update", { confirmed: true }); }
    catch { restart.disabled = false; status.textContent = "restart unavailable"; }
  } : null;
  later.onclick = () => {
    delete install.dataset.confirmVersion;
    delete restart.dataset.confirmVersion;
    panel.hidden = true;
  };
  return update;
}

function configureUpdater(invoke) {
  if (activeUpdate) renderUpdate(activeUpdate, invoke);
  if (!invoke || updateInitialized) return;
  updateInitialized = true;
  invoke("desktop_update_status")
    .then((update) => renderUpdate(update, invoke))
    .catch(() => { document.getElementById("update-status").textContent = "unavailable"; });
}

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
  const exact = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "medium",
  }).format(new Date(target));
  if (difference <= 0) return { label: "reset due", exact };
  const minutes = Math.max(1, Math.round(difference / 60_000));
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "always" });
  if (minutes >= 1440) {
    return { label: `resets ${formatter.format(Math.round(minutes / 1440), "day")}`, exact };
  }
  if (minutes >= 60) {
    return { label: `resets ${formatter.format(Math.round(minutes / 60), "hour")}`, exact };
  }
  return { label: `resets ${formatter.format(minutes, "minute")}`, exact };
}

export function formatWeeklyReset(epoch) {
  if (epoch === null || epoch === undefined || epoch === "") return "—";
  const value = Number(epoch);
  if (!Number.isFinite(value) || value <= 0) return "—";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short",
  }).format(new Date(value * 1000));
}

export function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat(undefined, {
    style: "percent", maximumFractionDigits: 0,
  }).format(number / 100);
}

export function formatActivityValue(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > MAX_ACTIVITY_VALUE) return "—";
  return new Intl.NumberFormat(undefined, {
    notation: "compact", maximumFractionDigits: value < 1000 ? 0 : 1,
  }).format(value);
}

export function formatActivityMetric(metric) {
  if (!metric || metric.coverage === "unavailable") return "—";
  const value = formatActivityValue(metric.value);
  if (value === "—") return value;
  return metric.coverage === "partial" ? `≥${value}` : value;
}

function unavailableActivity(accounts = []) {
  const metric = () => ({ value: null, coverage: "unavailable" });
  const periods = () => Object.fromEntries(ACTIVITY_PERIODS.map((period) => [period, metric()]));
  const aggregate = () => ({ tokens: periods(), sessions: periods(), calls: periods() });
  return {
    schema: ACTIVITY_SCHEMA,
    timezone: "local time", status: "unavailable", indexed_at: null,
    accounts: accounts.map((account) => ({
      name: account.name, provider: account.provider,
      attribution: "unavailable", tokens: periods(), sessions: periods(),
    })),
    unattributed: { claude_code: aggregate() },
    totals: aggregate(), daily: [], warnings: [],
  };
}

function normalizeActivityMetric(raw) {
  const keys = Object.keys(raw || {});
  if (keys.length !== 2 || !keys.includes("value") || !keys.includes("coverage") ||
      !ACTIVITY_COVERAGE.has(raw.coverage)) throw new Error("invalid activity metric");
  const value = raw.value;
  if (raw.coverage === "unavailable") {
    if (value !== null) throw new Error("invalid activity metric");
  } else if (!Number.isSafeInteger(value) || value < 0 || value > MAX_ACTIVITY_VALUE) {
    throw new Error("invalid activity metric");
  }
  return { value, coverage: raw.coverage };
}

function normalizeActivityPeriods(raw) {
  if (!raw || Object.keys(raw).length !== ACTIVITY_PERIODS.length ||
      ACTIVITY_PERIODS.some((period) => !(period in raw))) {
    throw new Error("invalid activity periods");
  }
  return Object.fromEntries(ACTIVITY_PERIODS.map(
    (period) => [period, normalizeActivityMetric(raw[period])],
  ));
}

export function normalizeActivity(raw, accounts = []) {
  const expected = [
    "schema", "timezone", "status", "indexed_at", "accounts", "unattributed",
    "totals", "daily", "warnings",
  ];
  const keys = Object.keys(raw || {});
  if (!raw || keys.length !== expected.length || expected.some((key) => !keys.includes(key)) ||
      raw.schema !== ACTIVITY_SCHEMA || !Array.isArray(raw.accounts) ||
      raw.accounts.length > 256 || typeof raw.timezone !== "string" ||
      raw.timezone.length < 1 || raw.timezone.length > 64 ||
      !ACTIVITY_STATUS.has(raw.status) ||
      (raw.indexed_at !== null &&
        (!Number.isFinite(raw.indexed_at) || raw.indexed_at < 0)) ||
      !raw.totals || Object.keys(raw.totals).length !== 3 ||
      !raw.unattributed || Object.keys(raw.unattributed).length !== 1 ||
      !raw.unattributed.claude_code ||
      !Array.isArray(raw.daily) || raw.daily.length > 800 ||
      !Array.isArray(raw.warnings) || raw.warnings.length > ACTIVITY_WARNINGS.size ||
      raw.warnings.some((warning) => !ACTIVITY_WARNINGS.has(warning))) {
    throw new Error("invalid activity contract");
  }
  const known = new Map(accounts.map((account) => [account.name, account.provider]));
  const seen = new Set();
  const normalizedAccounts = raw.accounts.map((row) => {
    if (!row || Object.keys(row).length !== 5 ||
        !Object.keys(row).every((key) =>
          ["name", "provider", "attribution", "tokens", "sessions"].includes(key)) ||
        typeof row.name !== "string" || !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(row.name) ||
        !["claude", "codex"].includes(row.provider) || known.get(row.name) !== row.provider ||
        !["exact", "unavailable"].includes(row.attribution) ||
        seen.has(row.name)) throw new Error("invalid activity account");
    seen.add(row.name);
    return {
      name: row.name, provider: row.provider,
      attribution: row.attribution,
      tokens: normalizeActivityPeriods(row.tokens),
      sessions: normalizeActivityPeriods(row.sessions),
    };
  });
  if (seen.size !== known.size) throw new Error("invalid activity accounts");
  const normalizeAggregate = (value) => {
    if (!value || Object.keys(value).length !== 3 ||
        !["tokens", "sessions", "calls"].every((key) => key in value)) {
      throw new Error("invalid activity aggregate");
    }
    return {
      tokens: normalizeActivityPeriods(value.tokens),
      sessions: normalizeActivityPeriods(value.sessions),
      calls: normalizeActivityPeriods(value.calls),
    };
  };
  const daily = raw.daily.map((row) => {
    const dailyKeys = [
      "date", "codex_tokens", "claude_code_tokens", "claude_code_calls",
      "total", "driver", "evidence",
    ];
    if (!row || Object.keys(row).length !== dailyKeys.length ||
        dailyKeys.some((key) => !(key in row)) ||
        typeof row.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(row.date) ||
        !ACTIVITY_DRIVERS.has(row.driver) || typeof row.evidence !== "string" ||
        row.evidence.length > 160 ||
        ["codex_tokens", "claude_code_tokens", "claude_code_calls", "total"]
          .some((key) => !Number.isSafeInteger(row[key]) || row[key] < 0 ||
            row[key] > MAX_ACTIVITY_VALUE) ||
        row.total !== row.codex_tokens + row.claude_code_tokens) {
      throw new Error("invalid activity daily row");
    }
    return { ...row };
  });
  for (let index = 1; index < daily.length; index += 1) {
    if (daily[index - 1].date >= daily[index].date) {
      throw new Error("invalid activity daily order");
    }
  }
  return {
    schema: ACTIVITY_SCHEMA,
    timezone: raw.timezone, status: raw.status, indexed_at: raw.indexed_at,
    accounts: normalizedAccounts,
    unattributed: { claude_code: normalizeAggregate(raw.unattributed.claude_code) },
    totals: normalizeAggregate(raw.totals), daily,
    warnings: [...new Set(raw.warnings)],
  };
}

function validLocalPath(value) {
  return typeof value === "string" && value.length <= 4096 && !value.includes("\0") &&
    (value === "" || value.startsWith("/"));
}

export function validateSettingsDraft(draft) {
  const errors = {};
  const title = typeof draft?.title === "string" ? draft.title : "";
  if (title !== title.trim() || title.length < 1 || title.length > 80 ||
      [...title].some((character) => character.charCodeAt(0) < 32 ||
        character.charCodeAt(0) === 127)) {
    errors.title = "Title must be 1–80 trimmed printable characters";
  }
  if (!VALID_THEMES.has(draft?.theme)) errors.theme = "Choose a supported theme";
  for (const key of ["redact_emails", "auto_handoff", "remember_window",
    "notifications_enabled", "reset_notifications"]) {
    if (typeof draft?.[key] !== "boolean") errors[key] = "Choose on or off";
  }
  const reserve = Number(draft?.reserve_percent);
  if (!Number.isFinite(reserve) || reserve < 0 || reserve > 99) {
    errors.reserve_percent = "Reserve must be between 0 and 99";
  }
  const refresh = Number(draft?.refresh_interval_seconds);
  if (!Number.isInteger(refresh) || refresh < REFRESH_INTERVAL_MIN ||
      refresh > REFRESH_INTERVAL_MAX) {
    errors.refresh_interval_seconds = "Refresh must be a whole number from 60 to 3600";
  }
  if (!VALID_TERMINALS.has(draft?.preferred_terminal)) {
    errors.preferred_terminal = "Choose Terminal, iTerm, or Warp";
  }
  for (const provider of ["claude", "codex"]) {
    const key = `${provider}_path`;
    if (!validLocalPath(draft?.[key])) errors[key] = "Use a blank or absolute local path";
  }
  for (const key of ["notification_threshold", "claude_notification_threshold",
    "codex_notification_threshold"]) {
    if (key !== "notification_threshold" && draft?.[key] === "") continue;
    const threshold = Number(draft?.[key]);
    if (!Number.isInteger(threshold) || threshold < 1 || threshold > 99) {
      errors[key] = "Threshold must be a whole number from 1 to 99";
    }
  }
  return errors;
}

export function settingsPatch(draft) {
  return {
    theme: draft.theme,
    title: draft.title,
    redact_emails: draft.redact_emails,
    reserve_percent: Number(draft.reserve_percent),
    auto_handoff: draft.auto_handoff,
    refresh_interval_seconds: Number(draft.refresh_interval_seconds),
    provider_paths: {
      claude: draft.claude_path || null,
      codex: draft.codex_path || null,
    },
    preferred_terminal: draft.preferred_terminal,
    remember_window: draft.remember_window,
    notifications: {
      enabled: draft.notifications_enabled,
      reset_enabled: draft.reset_notifications,
      global_threshold_percent: Number(draft.notification_threshold),
      provider_threshold_percent: {
        claude: draft.claude_notification_threshold === "" ? null
          : Number(draft.claude_notification_threshold),
        codex: draft.codex_notification_threshold === "" ? null
          : Number(draft.codex_notification_threshold),
      },
    },
  };
}

export function normalizeRoutingPreview(raw) {
  if (!raw || raw.schema !== ROUTING_SCHEMA || !ROUTING_FAMILIES.has(raw.family)) {
    throw new Error("invalid routing preview");
  }
  const provider = raw.provider === "claude" || raw.provider === "codex"
    ? raw.provider : null;
  if (!provider || (raw.family === "codex") !== (provider === "codex")) {
    throw new Error("invalid routing provider");
  }
  if (!Array.isArray(raw.candidates) || raw.candidates.length > 256) {
    throw new Error("invalid routing candidates");
  }
  let selectedCount = 0;
  const candidates = raw.candidates.map((row) => {
    if (!row || typeof row.name !== "string" ||
        !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(row.name) || row.provider !== provider ||
        typeof row.selected !== "boolean" || typeof row.eligible !== "boolean" ||
        !ROUTING_CODES.has(row.code) || !ROUTING_ACTIONS.has(row.action) ||
        typeof row.explanation !== "string" || row.explanation.length < 1 ||
        row.explanation.length > 256) {
      throw new Error("invalid routing candidate");
    }
    if (row.selected) {
      selectedCount += 1;
      if (!row.eligible || row.code !== "selected") {
        throw new Error("invalid routing selection");
      }
    }
    return {
      name: row.name, provider, selected: row.selected, eligible: row.eligible,
      code: row.code, action: row.action, explanation: row.explanation,
    };
  });
  if (selectedCount > 1) throw new Error("invalid routing selection");
  let selected = null;
  if (raw.selected !== null && raw.selected !== undefined) {
    if (typeof raw.selected.name !== "string" || raw.selected.provider !== provider ||
        !candidates.some((row) => row.selected && row.name === raw.selected.name)) {
      throw new Error("invalid routing selection summary");
    }
    selected = { name: raw.selected.name, provider };
  }
  if ((selected === null) !== (selectedCount === 0)) {
    throw new Error("invalid routing selection summary");
  }
  const launchCodes = new Set([...ROUTING_CODES, "launch_ready", "no_provider_accounts"]);
  const launch = raw.launch;
  if (!launch || !["ready", "unavailable"].includes(launch.status) ||
      !launchCodes.has(launch.code) || !ROUTING_ACTIONS.has(launch.action) ||
      typeof launch.explanation !== "string" || launch.explanation.length < 1 ||
      launch.explanation.length > 256 ||
      (launch.status === "ready" && (!selected || launch.code !== "launch_ready"))) {
    throw new Error("invalid routing launch state");
  }
  return {
    schema: ROUTING_SCHEMA, family: raw.family, provider, selected, candidates,
    launch: {
      status: launch.status, code: launch.code,
      explanation: launch.explanation, action: launch.action,
    },
  };
}

export function normalizeHandoffHealth(raw) {
  const keys = new Set(Object.keys(raw || {}));
  const expected = [
    "schema", "configured", "supported", "state", "code", "explanation",
    "action", "active_session", "account", "model", "observed_at",
    "preference_effect",
  ];
  if (!raw || keys.size !== expected.length || expected.some((key) => !keys.has(key)) ||
      raw.schema !== HANDOFF_HEALTH_SCHEMA || typeof raw.configured !== "boolean" ||
      typeof raw.supported !== "boolean" || !HANDOFF_STATES.has(raw.state) ||
      typeof raw.code !== "string" || !/^[a-z0-9_]{1,64}$/.test(raw.code) ||
      typeof raw.explanation !== "string" || raw.explanation.length < 1 ||
      raw.explanation.length > 256 || !HANDOFF_ACTIONS.has(raw.action) ||
      typeof raw.active_session !== "boolean" ||
      (raw.account !== null && (typeof raw.account !== "string" ||
        !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(raw.account))) ||
      (raw.model !== null && (typeof raw.model !== "string" ||
        !/^[a-z0-9_-]{1,32}$/.test(raw.model))) ||
      (raw.observed_at !== null &&
        (!Number.isFinite(raw.observed_at) || raw.observed_at < 0)) ||
      raw.preference_effect !== "next_launch_only" ||
      (!raw.supported && raw.state !== "unavailable") ||
      (raw.active_session && !["configured", "armed", "downgraded",
        "supervision_lost", "loop_guard"].includes(raw.state)) ||
      (raw.active_session && raw.state === "configured" &&
        (raw.code !== "awaiting_session_start" || raw.action !== "wait_for_session")) ||
      (["armed", "downgraded", "supervision_lost", "loop_guard"]
        .includes(raw.state) && !raw.active_session) ||
      (raw.state === "disabled" && raw.configured) ||
      (!raw.configured && !raw.active_session && raw.state !== "disabled" &&
        raw.state !== "unavailable")) {
    throw new Error("invalid handoff health contract");
  }
  return Object.fromEntries(expected.map((key) => [key, raw[key]]));
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
  if (account?.state === "stale") {
    const age = Number(account?.observation_age_seconds);
    const ageCopy = Number.isFinite(age) ? ` (${formatAge(age)} old)` : "";
    return TRANSIENT_CODES.has(account?.diagnostic_code) ? {
      label: "stale",
      action: `Provider unavailable; showing the last verified reading${ageCopy} until automatic retry`,
    } : {
      label: "stale", action: `Last verified reading has aged out${ageCopy}; refresh when online`,
    };
  }
  return {
    label: "held", action: account?.note || "Capacity is not proven; refresh or re-authenticate",
  };
}

export function externalReauthenticationPresentation(account) {
  if (account?.recovery_action !== "external_reauthentication" ||
      account?.state !== "held" || !account?.policy ||
      !["keychain_manual", "provider_managed"].includes(
        account.policy.reauthentication)) return null;
  const provider = account.provider === "codex" ? "Codex" : "Claude";
  const keychain = account.policy.reauthentication === "keychain_manual";
  return {
    label: `Open ${provider} login`,
    warning: keychain
      ? "Keychain-backed macOS login · provider sign-in cannot be rolled back by Headroom"
      : "Provider-managed login · Headroom will not modify or copy its credentials",
    confirmation: `Open ${provider} sign-in for ${account.name}? Complete only the provider-owned login, then return to Headroom and refresh.`,
  };
}

export function externalReauthenticationConfirmation(account, armed = false) {
  const recovery = externalReauthenticationPresentation(account);
  if (!recovery) return null;
  if (!armed) {
    return {
      shouldLaunch: false,
      label: `Confirm ${recovery.label.replace(/^Open /, "")}`,
      message: `Click again to open ${account.provider} sign-in for ${account.name}.`,
    };
  }
  return {
    shouldLaunch: true,
    label: `Opening ${recovery.label.replace(/^Open /, "")}…`,
    message: `Re-proving ${account.name} before opening provider sign-in…`,
  };
}

export function formatAge(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds)));
  if (!Number.isFinite(value)) return "age unknown";
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
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
      observation_age_seconds: Number.isFinite(Number(account?.observation_age_seconds))
        ? Math.max(0, Math.floor(Number(account.observation_age_seconds))) : null,
      trust_state: TRUST_STATES.has(account?.trust_state) ? account.trust_state : null,
      reserved: account?.reserved === true,
      policy,
      recovery_action: RECOVERY_ACTIONS.has(account?.recovery_action)
        ? account.recovery_action : null,
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
  const rawProviderPaths = rawSettings.provider_paths &&
    typeof rawSettings.provider_paths === "object" ? rawSettings.provider_paths : {};
  const providerPaths = {};
  for (const provider of ["claude", "codex"]) {
    if (validLocalPath(rawProviderPaths[provider]) && rawProviderPaths[provider]) {
      providerPaths[provider] = rawProviderPaths[provider];
    }
  }
  const rawNotifications = rawSettings.notifications &&
    typeof rawSettings.notifications === "object" ? rawSettings.notifications : {};
  const rawProviderThresholds = rawNotifications.provider_threshold_percent &&
    typeof rawNotifications.provider_threshold_percent === "object"
    ? rawNotifications.provider_threshold_percent : {};
  const integerInRange = (value, minimum, maximum, fallback) => {
    const number = Number(value);
    return Number.isInteger(number) && number >= minimum && number <= maximum
      ? number : fallback;
  };
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
  const handoff = normalizeHandoffHealth(view.handoff);
  let accountActivity = unavailableActivity(accounts);
  if (view.activity !== undefined) {
    try { accountActivity = normalizeActivity(view.activity, accounts); }
    catch { accountActivity = unavailableActivity(accounts); }
  }
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
        refresh_interval_seconds: integerInRange(rawSettings.refresh_interval_seconds,
          REFRESH_INTERVAL_MIN, REFRESH_INTERVAL_MAX, 300),
        provider_paths: providerPaths,
        preferred_terminal: VALID_TERMINALS.has(rawSettings.preferred_terminal)
          ? rawSettings.preferred_terminal : "terminal",
        remember_window: rawSettings.remember_window !== false,
        notifications: {
          enabled: rawNotifications.enabled === true,
          reset_enabled: rawNotifications.reset_enabled === true,
          global_threshold_percent: integerInRange(
            rawNotifications.global_threshold_percent, 1, 99, 20),
          provider_threshold_percent: Object.fromEntries(
            ["claude", "codex"].flatMap((provider) => {
              const value = integerInRange(rawProviderThresholds[provider], 1, 99, null);
              return value === null ? [] : [[provider, value]];
            }),
          ),
        },
      },
      freshness,
      headline: {
        avg_5h_left_percent: numberOrNull(rawHeadline.avg_5h_left_percent),
        avg_7d_left_percent: numberOrNull(rawHeadline.avg_7d_left_percent),
        current_accounts: numberOrNull(rawHeadline.current_accounts),
        total_accounts: numberOrNull(rawHeadline.total_accounts),
      },
      recovery_code: typeof view.recovery_code === "string" ? view.recovery_code : null,
      handoff, activity: accountActivity, accounts, candidates, onboarding: {
        schema: "headroom_desktop_onboarding@1", step,
        resumable: rawOnboarding.resumable === true,
        recovery_code: typeof rawOnboarding.recovery_code === "string"
          ? rawOnboarding.recovery_code : null,
        providers,
      } },
  };
}

function renderHandoffHealth(handoff, mode) {
  const panel = document.getElementById("handoff-health");
  if (!panel) return;
  panel.hidden = mode !== "ready";
  panel.dataset.state = handoff.state;
  document.getElementById("handoff-state").textContent = handoff.state.replaceAll("_", " ");
  document.getElementById("handoff-code").textContent = handoff.code.replaceAll("_", " ");
  const actions = {
    none: "No action required",
    upgrade_engine: "Update the bundled Headroom engine",
    install_claude_cli: "Install or configure the Claude CLI",
    inspect_diagnostics: "Inspect Headroom diagnostics",
    use_compatible_interactive_launch: "Start Claude from an interactive terminal",
    inspect_handoff_health: "Inspect handoff diagnostics",
    start_new_session: "Start a new Claude session",
    enable_handoff: "Enable automatic handoff for the next launch",
    wait_for_session: "Wait for Claude SessionStart proof",
  };
  document.getElementById("handoff-action").textContent = actions[handoff.action];
  document.getElementById("handoff-explanation").textContent = handoff.explanation;
  const context = [handoff.account, handoff.model].filter(Boolean).join(" · ");
  document.getElementById("handoff-context").textContent = context ||
    (handoff.configured ? "next compatible Claude launch" : "future launches disabled");
  const levels = {
    armed: 5, configured: 3, downgraded: 2, supervision_lost: 1,
    loop_guard: 1, unavailable: 0, disabled: 0,
  };
  panel.dataset.level = String(levels[handoff.state] ?? 0);
}

export function compactAccountWindows(windows) {
  const source = windows && typeof windows === "object" ? windows : {};
  const fableKey = Object.keys(source).find(
    (key) => key.toLowerCase() === "scoped:fable",
  );
  return [
    { label: "5h", value: source["5h"] || null },
    { label: "week", value: source["7d"] || null },
    { label: "Fable", value: fableKey ? source[fableKey] : null },
  ];
}

function windowRow(label, value) {
  const row = document.createElement("div");
  row.className = `window-row window-${VALID_STATES.has(value?.state) ? value.state : "empty"}`;
  const line = document.createElement("div");
  line.className = "window-line";
  const name = document.createElement("span");
  name.textContent = label;
  const reading = document.createElement("strong");
  const left = percentLeft(value);
  const capacity = left === null ? "—" : formatPercent(left);
  reading.textContent = capacity;
  const reset = formatReset(value?.resets_at);
  if (reset.exact) reading.title = `${label} reset: ${reset.exact}`;
  line.append(name, reading);
  const meter = document.createElement("div");
  meter.className = "meter";
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-label", `${label} capacity left`);
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", "100");
  if (left !== null) meter.setAttribute("aria-valuenow", String(left));
  meter.setAttribute("aria-valuetext", left === null ? "not available" : `${capacity} left`);
  const fill = document.createElement("i");
  fill.style.width = `${left ?? 0}%`;
  meter.append(fill);
  row.append(line, meter);
  return row;
}

function weeklyResetRow(value) {
  const row = document.createElement("div");
  row.className = "weekly-reset";
  const label = document.createElement("span");
  label.textContent = "WEEKLY RESET";
  const reading = document.createElement("time");
  reading.textContent = formatWeeklyReset(value?.resets_at);
  const epoch = Number(value?.resets_at);
  if (Number.isFinite(epoch) && epoch > 0) {
    reading.dateTime = new Date(epoch * 1000).toISOString();
  }
  row.append(label, reading);
  return row;
}

function accountActivityRow(activity) {
  const row = document.createElement("div");
  row.className = "account-activity";
  const partial = ACTIVITY_PERIODS.some(
    (period) => activity?.tokens?.[period]?.coverage === "partial");
  row.title = activity?.attribution === "exact" && partial
    ? "Exact recorded events; ≥ marks incomplete provider source coverage"
    : activity?.attribution === "exact"
      ? "Exact provider events grouped by local calendar day"
    : "Historical usage cannot be assigned safely to this account";
  const label = document.createElement("span");
  label.textContent = "TOKENS";
  const metrics = document.createElement("span");
  metrics.textContent = ACTIVITY_PERIODS.map(
    (period) => `${period.toUpperCase()} ${formatActivityMetric(activity?.tokens?.[period])}`,
  ).join(" · ");
  row.append(label, metrics);
  return row;
}

function accountCard(account, activity = null, lifecycle = null, surface = "main") {
  const article = document.createElement("article");
  article.className = `account state-${account.state}`;
  if (account.reserved) article.classList.add("is-reserved");
  article.dataset.provider = account.provider;
  const header = document.createElement("header");
  const identity = document.createElement("div");
  const name = document.createElement("h3");
  name.textContent = account.name;
  const detail = document.createElement("p");
  detail.textContent = [account.provider, account.identity].filter(Boolean).join(" · ");
  identity.append(name, detail);
  const state = document.createElement("span");
  state.className = "state";
  const presentation = accountStatePresentation(account);
  state.textContent = presentation.label;
  state.setAttribute("aria-label", `${presentation.label}: ${presentation.action}`);
  header.append(identity, state);
  const windows = document.createElement("div");
  windows.className = "windows";
  for (const item of compactAccountWindows(account.windows)) {
    windows.append(windowRow(item.label, item.value));
  }
  article.append(
    header, windows, weeklyResetRow(account.windows?.["7d"]),
    accountActivityRow(activity),
  );
  if (surface === "main" && lifecycle && account.policy) {
    article.append(accountLifecyclePanel(account, lifecycle));
  }
  return article;
}

let activeBurnRange = "90";

function burnRowsInRange(rows, range) {
  if (range === "all" || rows.length === 0) return rows;
  const last = new Date(`${rows.at(-1).date}T00:00:00.000Z`);
  const first = new Date(last);
  first.setUTCDate(first.getUTCDate() - Number(range) + 1);
  return rows.filter((row) => new Date(`${row.date}T00:00:00.000Z`) >= first);
}

function logHeatLevel(value, maximum) {
  if (value <= 0 || maximum <= 0) return 0;
  return Math.max(1, Math.min(5,
    Math.ceil((Math.log10(value + 1) / Math.log10(maximum + 1)) * 5)));
}

function isoWeek(date) {
  const value = new Date(`${date}T00:00:00.000Z`);
  const day = (value.getUTCDay() + 6) % 7;
  value.setUTCDate(value.getUTCDate() - day);
  return value.toISOString().slice(0, 10);
}

function renderBurnHeatmap(rows) {
  const target = document.getElementById("burn-heatmap");
  if (!target) return;
  const maximum = Math.max(0, ...rows.map((row) => row.total));
  const cells = rows.map((row) => {
    const cell = document.createElement("span");
    cell.className = `burn-day burn-level-${logHeatLevel(row.total, maximum)}`;
    cell.tabIndex = 0;
    cell.title = `${row.date} · ${formatActivityValue(row.total)} tokens · ${row.driver}`;
    cell.setAttribute("aria-label", cell.title);
    return cell;
  });
  target.replaceChildren(...cells);
}

function renderBurnTrend(rows) {
  const target = document.getElementById("burn-trend");
  if (!target) return;
  const totals = new Map();
  for (const row of rows) {
    const week = isoWeek(row.date);
    totals.set(week, (totals.get(week) || 0) + row.total);
  }
  const weekly = [...totals].map(([week, total]) => ({ week, total }));
  if (!weekly.length) {
    target.replaceChildren();
    return;
  }
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 600 100");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Weekly exact token totals on a logarithmic scale");
  const maximum = Math.max(...weekly.map((row) => Math.log10(row.total + 1)), 1);
  const points = weekly.map((row, index) => {
    const x = weekly.length === 1 ? 300 : (index / (weekly.length - 1)) * 590 + 5;
    const y = 94 - (Math.log10(row.total + 1) / maximum) * 86;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const line = document.createElementNS(namespace, "polyline");
  line.setAttribute("points", points);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "2");
  const title = document.createElementNS(namespace, "title");
  const peak = weekly.reduce((best, row) => row.total > best.total ? row : best);
  title.textContent = `Peak week ${peak.week}: ${formatActivityValue(peak.total)} tokens`;
  svg.append(title, line);
  target.replaceChildren(svg);
}

function renderBurnDrivers(rows) {
  const target = document.getElementById("burn-drivers");
  if (!target) return;
  const groups = new Map();
  for (const row of rows) groups.set(row.driver, (groups.get(row.driver) || 0) + row.total);
  const total = rows.reduce((sum, row) => sum + row.total, 0);
  const items = [...groups].sort((a, b) => b[1] - a[1]).map(([driver, tokens]) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = driver.toUpperCase();
    const value = document.createElement("strong");
    const share = total ? Math.round(tokens / total * 100) : 0;
    value.textContent = `${formatActivityValue(tokens)} · ${share}%`;
    item.append(label, value);
    return item;
  });
  target.replaceChildren(...items);
}

function renderBurnScale(rows) {
  const target = document.getElementById("burn-scale");
  if (!target) return;
  const total = rows.reduce((sum, row) => sum + row.total, 0);
  const words = total * 0.75;
  const values = [
    ["WORDS", formatActivityValue(Math.round(words)), "tokens × 0.75"],
    ["READING", `${formatActivityValue(Math.round(words / 250 / 60))}h`, "250 words/min"],
    ["NOVELS", formatActivityValue(Math.round(words / 90_000)), "90k words/novel"],
  ];
  target.replaceChildren(...values.map(([labelText, valueText, noteText]) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    const value = document.createElement("strong");
    const note = document.createElement("small");
    label.textContent = labelText;
    value.textContent = valueText;
    note.textContent = noteText;
    item.append(label, value, note);
    return item;
  }));
}

function renderBurnTable(rows) {
  const body = document.getElementById("burn-table-body");
  if (!body) return;
  const visible = rows.slice(-30).reverse();
  body.replaceChildren(...visible.map((row) => {
    const index = rows.indexOf(row);
    const averageRows = rows.slice(Math.max(0, index - 6), index + 1);
    const average = averageRows.reduce((sum, item) => sum + item.total, 0) /
      Math.max(1, averageRows.length);
    const tr = document.createElement("tr");
    for (const value of [
      row.date, formatActivityValue(row.total), formatActivityValue(Math.round(average)),
      formatActivityValue(row.codex_tokens), formatActivityValue(row.claude_code_tokens),
      formatActivityValue(row.claude_code_calls), row.driver,
    ]) {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    }
    return tr;
  }));
}

function renderBurnDashboard(activity) {
  const rows = burnRowsInRange(activity?.daily || [], activeBurnRange);
  for (const button of document.querySelectorAll("[data-burn-range]")) {
    button.classList.toggle("active", button.dataset.burnRange === activeBurnRange);
    button.onclick = () => {
      activeBurnRange = button.dataset.burnRange;
      renderBurnDashboard(activity);
    };
  }
  renderBurnHeatmap(rows);
  renderBurnTrend(rows);
  renderBurnDrivers(rows);
  renderBurnScale(rows);
  renderBurnTable(rows);
}

function renderActivitySummary(activity, mode) {
  const panel = document.getElementById("activity-summary");
  if (!panel) return;
  panel.hidden = mode !== "ready";
  const dashboard = document.getElementById("burn-dashboard");
  if (dashboard) dashboard.hidden = mode !== "ready";
  const setPeriods = (prefix, metrics) => {
    for (const period of ACTIVITY_PERIODS) {
      const element = document.getElementById(`${prefix}-${period}`);
      if (element) element.textContent = formatActivityMetric(metrics?.[period]);
    }
  };
  setPeriods("total-tokens", activity?.totals?.tokens);
  setPeriods("total-sessions", activity?.totals?.sessions);
  setPeriods("total-calls", activity?.totals?.calls);
  setPeriods("unattributed-claude", activity?.unattributed?.claude_code?.tokens);
  const status = document.getElementById("activity-status");
  if (status) {
    const label = activity?.status === "indexing" ? "INDEXING EXACT LOCAL LOGS"
      : activity?.status === "refreshing" ? "REFRESHING INDEX"
        : activity?.status === "ready" ? "EXACT LOCAL-DAY INDEX" : "INDEX UNAVAILABLE";
    const warning = activity?.warnings?.includes("source_read_incomplete")
      ? " · SOURCE READ INCOMPLETE"
      : activity?.warnings?.includes("codex_legacy_usage_unavailable")
        ? " · ≥ CODEX LEGACY GAPS" : "";
    status.textContent = `${label} · ${activity?.timezone || "local time"}${warning}`;
  }
  if (mode === "ready" && activity?.status === "indexing" && activeInvoke &&
      activityPollTimer === null) {
    activityPollTimer = setTimeout(() => {
      activityPollTimer = null;
      activeInvoke("desktop_refresh").catch(() => {});
    }, 15_000);
  } else if (activity?.status !== "indexing" && activityPollTimer !== null) {
    clearTimeout(activityPollTimer);
    activityPollTimer = null;
  }
  renderBurnDashboard(activity);
}

function accountLifecyclePanel(account, { invoke, update }) {
  const details = document.createElement("details");
  details.className = "account-lifecycle";
  const summary = document.createElement("summary");
  summary.textContent = "> manage account";
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
    const recovery = externalReauthenticationPresentation(account);
    reauthDiagnostic.textContent = recovery ? "Login required" : "Login healthy";
    if (recovery) {
      let confirmationArmed = false;
      const openLogin = actionButton(recovery.label, async () => {
        const confirmation = externalReauthenticationConfirmation(
          account, confirmationArmed,
        );
        if (!confirmation?.shouldLaunch) {
          confirmationArmed = true;
          openLogin.textContent = confirmation?.label || recovery.label;
          reauthDiagnostic.textContent = confirmation?.message || recovery.warning;
          return;
        }
        confirmationArmed = false;
        openLogin.disabled = true;
        openLogin.textContent = confirmation.label;
        reauthDiagnostic.textContent = confirmation.message;
        try {
          const outcome = await invoke("desktop_open_external_reauthentication", {
            accountName: account.name,
          });
          reauthDiagnostic.textContent =
            `${outcome.terminal} opened for ${outcome.provider} sign-in on ${outcome.account_name}. Complete sign-in, then Refresh.`;
        } catch {
          reauthDiagnostic.textContent =
            "Provider sign-in was refused because the account no longer requires it or is in use";
        } finally {
          openLogin.textContent = recovery.label;
          openLogin.disabled = false;
        }
      }, "primary");
      reauthentication.append(openLogin);
    }
    reauthentication.append(reauthDiagnostic);
  }
  details.append(summary, reauthentication);
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

export function refreshStatePresentation(state, diagnosticCode = null) {
  const code = typeof diagnosticCode === "string" &&
    /^engine_[a-z0-9_]{1,56}$/.test(diagnosticCode) ? ` · ${diagnosticCode}` : "";
  if (state === "refreshing") return {
    label: "REFRESHING · verified readings remain visible while providers respond",
    busy: true,
  };
  if (state === "offline") return {
    label: "OFFLINE · refresh failed; retained readings keep their displayed age and trust state",
    busy: false,
  };
  if (state === "backoff") return {
    label: "BACKOFF · automatic retry is bounded, jittered, and energy-conscious",
    busy: false,
  };
  if (state === "recovering") return {
    label: `RECOVERING · restarting the bundled engine within the bounded policy${code}`,
    busy: true,
  };
  if (state === "degraded") return {
    label: `DEGRADED · repeated engine failures stopped the restart loop safely${code}`,
    busy: false,
  };
  return { label: "", busy: false };
}

function applyRefreshState(state, diagnosticCode = null) {
  const presentation = refreshStatePresentation(state, diagnosticCode);
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
  refresh.textContent = state === "degraded" ? "Retry engine" :
    presentation.busy ? state === "recovering" ? "Recovering…" : "Refreshing…" : "Refresh";
  refresh.disabled = presentation.busy || !activeInvoke ||
    (activeBootstrap?.view.mode !== "ready" && state !== "degraded");
}

function settingsDraftFromForm(form) {
  return {
    theme: form.elements.theme.value,
    title: form.elements.title.value,
    redact_emails: form.elements.redact_emails.checked,
    reserve_percent: form.elements.reserve_percent.value,
    auto_handoff: false,
    refresh_interval_seconds: form.elements.refresh_interval_seconds.value,
    claude_path: form.elements.claude_path.value,
    codex_path: form.elements.codex_path.value,
    preferred_terminal: form.elements.preferred_terminal.value,
    remember_window: form.elements.remember_window.checked,
    notifications_enabled: form.elements.notifications_enabled.checked,
    reset_notifications: form.elements.reset_notifications.checked,
    notification_threshold: form.elements.notification_threshold.value,
    claude_notification_threshold: form.elements.claude_notification_threshold.value,
    codex_notification_threshold: form.elements.codex_notification_threshold.value,
  };
}

function populateSettingsForm(settings) {
  const form = document.getElementById("settings-form");
  const notifications = settings.notifications || {};
  const paths = settings.provider_paths || {};
  form.elements.theme.value = settings.theme;
  form.elements.title.value = settings.title;
  form.elements.redact_emails.checked = settings.redact_emails;
  form.elements.reserve_percent.value = String(settings.reserve_percent);
  form.elements.refresh_interval_seconds.value = String(settings.refresh_interval_seconds);
  form.elements.claude_path.value = paths.claude || "";
  form.elements.codex_path.value = paths.codex || "";
  form.elements.preferred_terminal.value = settings.preferred_terminal;
  form.elements.remember_window.checked = settings.remember_window;
  form.elements.notifications_enabled.checked = notifications.enabled === true;
  form.elements.reset_notifications.checked = notifications.reset_enabled === true;
  form.elements.notification_threshold.value = String(
    notifications.global_threshold_percent ?? 20);
  form.elements.claude_notification_threshold.value =
    notifications.provider_threshold_percent?.claude ?? "";
  form.elements.codex_notification_threshold.value =
    notifications.provider_threshold_percent?.codex ?? "";
  form.dataset.dirty = "false";
}

function applySettingsValidation(form) {
  const errors = validateSettingsDraft(settingsDraftFromForm(form));
  const ids = {
    theme: "settings-theme", title: "settings-title-input",
    reserve_percent: "settings-reserve",
    refresh_interval_seconds: "settings-refresh",
    preferred_terminal: "settings-terminal", claude_path: "settings-claude-path",
    codex_path: "settings-codex-path", notification_threshold: "settings-threshold",
    claude_notification_threshold: "settings-claude-threshold",
    codex_notification_threshold: "settings-codex-threshold",
  };
  for (const [key, id] of Object.entries(ids)) {
    document.getElementById(id).setCustomValidity(errors[key] || "");
  }
  const messages = [...new Set(Object.values(errors))];
  document.getElementById("settings-errors").textContent = messages.join(" · ");
  document.getElementById("settings-save").disabled = messages.length > 0;
  return messages.length === 0;
}

async function openSettingsPanel() {
  if (document.body.dataset.surface !== "main") return;
  const panel = document.getElementById("settings");
  if (activeBootstrap) populateSettingsForm(activeBootstrap.view.settings);
  panel.hidden = false;
  panel.scrollIntoView({ block: "start", behavior: "smooth" });
  applySettingsValidation(document.getElementById("settings-form"));
  document.getElementById("settings-title-input").focus();
  const status = document.getElementById("settings-login-status");
  const checkbox = document.getElementById("settings-launch-at-login");
  checkbox.disabled = !activeInvoke;
  if (!activeInvoke) {
    status.textContent = "Launch at login is unavailable outside the desktop app.";
    return;
  }
  status.textContent = "Checking the macOS login item…";
  try {
    checkbox.checked = await activeInvoke("desktop_launch_at_login_status");
    status.textContent = checkbox.checked
      ? "Enabled · login launches Headroom quietly in the menu bar."
      : "Disabled · Headroom opens only when you launch it.";
  } catch {
    checkbox.disabled = true;
    status.textContent = "The macOS login item status is unavailable.";
  }
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
    controls.push(actionButton("Settings", openSettingsPanel));
  }
  actions.replaceChildren(...controls);
}

function configureSettings(value, invoke) {
  const panel = document.getElementById("settings");
  const form = document.getElementById("settings-form");
  if (panel.hidden || form.dataset.dirty !== "true") {
    populateSettingsForm(value.view.settings);
  }
  form.oninput = () => {
    form.dataset.dirty = "true";
    applySettingsValidation(form);
  };
  form.elements.theme.onchange = invoke ? async () => {
    const theme = form.elements.theme.value;
    if (!VALID_THEMES.has(theme)) return;
    document.body.dataset.theme = theme;
    try {
      await invoke("desktop_set_theme", { theme });
      document.getElementById("settings-errors").textContent =
        `Theme preview applied: ${theme}`;
    } catch {
      form.elements.theme.value = activeBootstrap?.theme || "terminal";
      document.body.dataset.theme = activeBootstrap?.theme || "terminal";
      document.getElementById("settings-errors").textContent =
        "Theme could not be saved safely.";
    }
  } : null;
  form.onsubmit = invoke ? async (event) => {
    event.preventDefault();
    if (!applySettingsValidation(form)) return;
    const save = document.getElementById("settings-save");
    const diagnostic = document.getElementById("settings-errors");
    save.disabled = true;
    diagnostic.textContent = "Validating and committing settings…";
    try {
      await invoke("desktop_update_settings", {
        patch: settingsPatch(settingsDraftFromForm(form)),
      });
      form.dataset.dirty = "false";
      diagnostic.textContent = "Settings saved atomically.";
    } catch {
      diagnostic.textContent =
        "Settings were not changed. Custom provider paths must name executable files.";
    } finally {
      save.disabled = false;
    }
  } : null;
  const login = document.getElementById("settings-launch-at-login");
  login.onchange = invoke ? async () => {
    const requested = login.checked;
    const status = document.getElementById("settings-login-status");
    login.disabled = true;
    status.textContent = requested ? "Enabling the macOS login item…" :
      "Removing the macOS login item…";
    try {
      login.checked = await invoke("desktop_set_launch_at_login", { enabled: requested });
      status.textContent = login.checked
        ? "Enabled · login launches Headroom quietly in the menu bar."
        : "Disabled · the login item was removed.";
    } catch {
      login.checked = !requested;
      status.textContent = "The macOS login item could not be changed.";
    } finally {
      login.disabled = false;
    }
  } : null;
  document.getElementById("close-settings").onclick = () => {
    panel.hidden = true;
  };
}

function renderRoutingPreview(preview, invoke) {
  const result = document.getElementById("routing-result");
  const diagnostic = document.getElementById("routing-diagnostic");
  const selected = document.getElementById("routing-selected");
  const explanation = document.getElementById("routing-launch-explanation");
  const copy = document.getElementById("routing-copy");
  const open = document.getElementById("routing-open");
  result.hidden = false;
  selected.textContent = preview.selected
    ? `${preview.selected.name} // ${preview.provider}` : "No eligible account";
  explanation.textContent = `${preview.launch.explanation} [${preview.launch.code}]`;
  diagnostic.classList.toggle("is-error", preview.launch.status !== "ready");
  diagnostic.textContent = preview.selected
    ? `Engine selected ${preview.selected.name} for ${preview.family}.`
    : `No safe route for ${preview.family}; review the candidate actions below.`;
  const rows = preview.candidates.map((candidate) => {
    const row = document.createElement("div");
    row.className = "routing-candidate";
    row.dataset.code = candidate.code;
    const name = document.createElement("strong");
    name.textContent = `${candidate.selected ? ">" : "-"} ${candidate.name}`;
    const status = document.createElement("span");
    status.textContent = `[${candidate.code.replaceAll("_", " ")}]`;
    const reason = document.createElement("span");
    reason.textContent = `${candidate.explanation} · action: ${candidate.action.replaceAll("_", " ")}`;
    row.append(name, status, reason);
    return row;
  });
  document.getElementById("routing-candidates").replaceChildren(...rows);
  const canLaunch = Boolean(invoke && preview.selected && preview.launch.status === "ready");
  copy.disabled = !canLaunch;
  open.disabled = !canLaunch;
  copy.onclick = canLaunch ? async () => {
    copy.disabled = true;
    diagnostic.classList.remove("is-error");
    diagnostic.textContent = "Re-proving the route before copying…";
    try {
      await invoke("desktop_copy_routing_command", {
        family: preview.family, accountName: preview.selected.name,
      });
      diagnostic.textContent = `Safe ${preview.provider} launch command copied for ${preview.selected.name}.`;
    } catch (error) {
      diagnostic.classList.add("is-error");
      diagnostic.textContent = `Copy refused: ${routingCommandError(error)}`;
    } finally {
      copy.disabled = false;
    }
  } : null;
  open.onclick = canLaunch ? async () => {
    open.disabled = true;
    diagnostic.classList.remove("is-error");
    diagnostic.textContent = "Re-proving the route before opening the terminal…";
    try {
      const outcome = await invoke("desktop_open_routing_launch", {
        family: preview.family, accountName: preview.selected.name,
      });
      diagnostic.textContent =
        `Opened ${outcome.provider} on ${outcome.account_name} in ${outcome.terminal}.`;
    } catch (error) {
      diagnostic.classList.add("is-error");
      diagnostic.textContent = `Launch refused: ${routingCommandError(error)}`;
    } finally {
      open.disabled = false;
    }
  } : null;
}

function routingCommandError(error) {
  const text = String(error || "");
  if (text.includes("provider_cli_missing")) return "install or configure the provider CLI";
  if (text.includes("routing_authentication_required")) return "re-authenticate the selected account";
  if (text.includes("routing_capacity_unavailable")) return "wait for capacity to reset, then preview again";
  if (text.includes("routing_slot_leased")) return "close the other live session, then preview again";
  if (text.includes("routing_selection_changed")) return "the selection changed; preview again";
  if (text.includes("routing_infrastructure_unavailable")) return "protective state is unavailable; inspect diagnostics";
  if (text.includes("preferred terminal")) return "the configured terminal is unavailable";
  return "the engine could not prove this launch safe; preview again";
}

function configureRouting(value, invoke) {
  const family = document.getElementById("routing-family");
  const preview = document.getElementById("routing-preview");
  preview.disabled = !invoke || value.surface !== "main" || value.view.mode !== "ready";
  preview.onclick = invoke ? async () => {
    if (!ROUTING_FAMILIES.has(family.value)) return;
    preview.disabled = true;
    const diagnostic = document.getElementById("routing-diagnostic");
    diagnostic.classList.remove("is-error");
    diagnostic.textContent = `Proving the current ${family.value} route…`;
    try {
      activeRoutingPreview = normalizeRoutingPreview(await invoke(
        "desktop_routing_preview", { family: family.value }));
      renderRoutingPreview(activeRoutingPreview, invoke);
    } catch {
      activeRoutingPreview = null;
      document.getElementById("routing-result").hidden = true;
      diagnostic.classList.add("is-error");
      diagnostic.textContent =
        "Routing preview is unavailable; refresh capacity or inspect diagnostics.";
    } finally {
      preview.disabled = false;
    }
  } : null;
  family.onchange = () => {
    activeRoutingPreview = null;
    document.getElementById("routing-result").hidden = true;
    const diagnostic = document.getElementById("routing-diagnostic");
    diagnostic.classList.remove("is-error");
    diagnostic.textContent = `Preview ${family.value} to compute a fresh engine decision.`;
  };
  if (activeRoutingPreview && activeRoutingPreview.family === family.value) {
    renderRoutingPreview(activeRoutingPreview, invoke);
  }
}

export function renderBootstrap(raw, invoke = null) {
  const value = normalizeBootstrap(raw);
  const { view } = value;
  if (activeBootstrap && value.revision !== activeBootstrap.revision) {
    activeRoutingPreview = null;
  }
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
  renderHandoffHealth(view.handoff, view.mode);
  const average = typeof view.headline?.avg_5h_left_percent === "number"
    ? view.headline.avg_5h_left_percent : Number.NaN;
  const presentation = onboardingPresentation(view.onboarding);
  document.getElementById("fleet-title").textContent = view.mode === "onboarding"
    ? "$ headroom setup" : view.mode === "demo" ? "$ headroom demo" : "$ headroom status";
  document.getElementById("headline").textContent = view.mode === "recovery"
    ? `Safe recovery required (${view.recovery_code || "unknown"}); no files were changed`
    : view.mode === "onboarding" || view.mode === "demo" ? presentation.headline
      : Number.isFinite(average) ? `${formatPercent(average)} average five-hour headroom`
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
  const activityByName = new Map(view.activity.accounts.map((row) => [row.name, row]));
  const cards = view.accounts.map((account) => accountCard(
    account, activityByName.get(account.name), lifecycle, value.surface,
  ));
  if (!cards.length && view.mode === "ready") {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = value.surface === "popover"
      ? "No connected accounts · open Dashboard to begin setup"
      : "No connected accounts · begin setup to add Claude or Codex";
    cards.push(empty);
  }
  document.getElementById("accounts").replaceChildren(...cards);
  renderActivitySummary(view.activity, view.mode);
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
  const addAccount = document.getElementById("add-account");
  addAccount.hidden = actionCards.length === 0;
  addAccount.open = view.mode === "onboarding";
  addAccount.querySelector("summary").textContent = view.mode === "onboarding"
    ? "+ finish account setup" : "+ add account";
  configureSurfaceActions(value.surface, invoke);
  configureSettings(value, invoke);
  configureRouting(value, invoke);
  configureUpdater(invoke);
  const freshnessAge = view.freshness.age_seconds === null
    ? "age unknown" : `${view.freshness.age_seconds}s old`;
  document.getElementById("surface-status").textContent =
    `${view.freshness.state.toUpperCase()} · ${freshnessAge} · ${view.freshness.reason.replaceAll("_", " ")}`;
  const refresh = document.getElementById("refresh");
  refresh.disabled = !invoke || view.mode !== "ready";
  refresh.onclick = invoke ? async () => {
    const retryEngine = document.body.dataset.refreshState === "degraded";
    applyRefreshState(retryEngine ? "recovering" : "refreshing",
      retryEngine ? "engine_manual_retry" : null);
    try { await invoke(retryEngine ? "desktop_retry_engine" : "desktop_refresh"); }
    catch { applyRefreshState("offline"); }
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
  window.__headroomApplyBridge = (bridge) => {
    if (!activeBootstrap || !bridge || typeof bridge !== "object") return;
    renderBootstrap({
      bridge,
      surface: activeBootstrap.surface,
      revision: activeRevision,
      theme: activeBootstrap.theme,
      view: activeBootstrap.view,
    }, activeInvoke);
  };
  window.__headroomSetRefreshState = applyRefreshState;
  window.__headroomApplyUpdate = (update) => {
    try { renderUpdate(update, activeInvoke); }
    catch { document.getElementById("update-status").textContent = "invalid update state"; }
  };
  window.__headroomOpenPanel = (panel) => {
    if (panel === "settings" || panel === "appearance") openSettingsPanel();
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.getElementById("settings").hidden) {
      document.getElementById("settings").hidden = true;
      return;
    }
    if (!event.metaKey || event.ctrlKey || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key === ",") {
      event.preventDefault();
      openSettingsPanel();
    } else if (key === "r") {
      event.preventDefault();
      document.getElementById("refresh").click();
    } else if (key === "w" && activeInvoke) {
      event.preventDefault();
      activeInvoke("desktop_hide_dashboard").catch(() => {});
    } else if (key === "q" && activeInvoke) {
      event.preventDefault();
      activeInvoke("desktop_quit").catch(() => {});
    }
  });
  try {
    const invoke = window.__TAURI__?.core?.invoke || null;
    renderBootstrap(window.__HEADROOM_BOOTSTRAP__, invoke);
    // A stale-activation collection can finish before this module registers
    // its native callback. Reconcile once from the Rust-owned store so that
    // an early revision is never lost; duplicate revisions are ignored.
    if (invoke) {
      invoke("desktop_snapshot")
        .then((snapshot) => window.__headroomApplySnapshot(snapshot))
        .catch(() => {});
    }
  } catch (error) {
    document.getElementById("engine-badge").textContent = "unavailable";
    document.getElementById("summary").textContent = error.message;
    document.getElementById("headline").textContent = "The desktop engine did not start safely.";
  }
}
