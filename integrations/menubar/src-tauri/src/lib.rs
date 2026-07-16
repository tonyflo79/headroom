//! Headroom desktop shell.
//!
//! The default app is a self-contained native window. It starts the bundled
//! Headroom engine as a sidecar, performs a versioned JSON-lines handshake,
//! injects a sanitized fixture snapshot before page scripts run, and never
//! opens an HTTP listener. The legacy loopback viewer helpers remain in this
//! module while the later menu-bar slice is migrated onto the same bridge.

#![allow(dead_code)] // Removed when the legacy popover helpers move onto the bridge.

use std::{
    collections::VecDeque,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::TcpStream,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use tauri::{
    async_runtime::Receiver,
    image::Image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::PageLoadEvent,
    AppHandle, Manager, PhysicalPosition, PhysicalSize, Url, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_autostart::ManagerExt as AutostartManagerExt;
use tauri_plugin_positioner::{Position, WindowExt};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

#[cfg(unix)]
use std::{
    os::{
        fd::AsRawFd,
        unix::{
            fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
            net::{UnixListener, UnixStream},
        },
    },
    thread,
};

mod icon;

/// Default widget URL; the fleet dashboard serves `/widget` on this port.
const DEFAULT_WIDGET_URL: &str = "http://127.0.0.1:8377/widget";
/// Env var that overrides the widget URL (validated: loopback http only).
const WIDGET_URL_ENV: &str = "HEADROOM_WIDGET_URL";
const WINDOW_LABEL: &str = "popover";
const WINDOW_WIDTH: f64 = 360.0;
/// Initial height; the popover is resized to fit the fleet each time it opens
/// (see fit_popover_height) so no account is ever clipped below the fold.
const WINDOW_HEIGHT: f64 = 640.0;
/// Preferred popover height. Fixed, not content-fitted: the panel scrolls
/// internally, so every account (including the Codex rows at the bottom) is
/// reachable without the window ever running off the screen. Clamped down on
/// short displays by fit_popover_height.
const POPOVER_HEIGHT: f64 = 720.0;
const POPOVER_HEIGHT_MIN: f64 = 320.0;
/// TCP connect + HTTP response-read budget for the reachability probe.
const PROBE_TIMEOUT: Duration = Duration::from_millis(600);
/// How often the background watcher retries while the fallback page is shown.
const RETRY_INTERVAL: Duration = Duration::from_secs(3);
/// Clicking the tray icon while the panel is open fires focus-loss (hide)
/// first and the click event second; suppress the immediate re-open.
const REOPEN_SUPPRESS: Duration = Duration::from_millis(350);
/// How often the tray icon's battery level re-reads the widget feed.
const ICON_INTERVAL: Duration = Duration::from_secs(60);
/// Corner radius of the popover panel (matches the system panels).
const PANEL_RADIUS: f64 = 14.0;
/// Reject a runaway feed body instead of buffering it (the real feed is a
/// few KB).
const FEED_MAX_BYTES: usize = 256 * 1024;
/// Wall-clock bound for one whole feed exchange — a drip-feeding endpoint
/// must not hold a worker thread open indefinitely.
const FEED_DEADLINE: Duration = Duration::from_secs(6);

const DESKTOP_WINDOW_LABEL: &str = "main";
const DESKTOP_POPOVER_LABEL: &str = "desktop-popover";
const DESKTOP_BRIDGE_SCHEMA: &str = "headroom_desktop_bridge@1";
const DESKTOP_VIEW_SCHEMA: &str = "headroom_desktop_view@1";
const SIDECAR_STARTUP_TIMEOUT: Duration = Duration::from_secs(12);
const SIDECAR_REQUEST_TIMEOUT: Duration = Duration::from_secs(90);
const MAX_BRIDGE_FRAME_BYTES: usize = 1024 * 1024;
const DESKTOP_POPOVER_WIDTH: f64 = 420.0;
const DESKTOP_POPOVER_HEIGHT: f64 = 680.0;
const WINDOW_STATE_SCHEMA: &str = "headroom_desktop_window@1";
const DESKTOP_THEMES: [&str; 5] = ["midnight", "minimal", "chrome", "paper", "terminal"];
const COLLECTION_IDLE_INTERVAL: Duration = Duration::from_secs(300);
const COLLECTION_INTERVAL_MIN_SECONDS: u64 = 60;
const COLLECTION_INTERVAL_MAX_SECONDS: u64 = 3600;
const COLLECTION_RETRY_BASE: Duration = Duration::from_secs(5);
const COLLECTION_RETRY_CAP: Duration = Duration::from_secs(300);
const COLLECTION_TICK: Duration = Duration::from_secs(15);
const ENGINE_RESTART_BASE: Duration = Duration::from_secs(2);
const ENGINE_RESTART_CAP: Duration = Duration::from_secs(60);
const ENGINE_DEGRADED_COOLDOWN: Duration = Duration::from_secs(300);
const ENGINE_RESTART_LIMIT: u32 = 3;
const ENGINE_CRASH_WINDOW: Duration = Duration::from_secs(300);
const ENGINE_STABLE_RESET: Duration = Duration::from_secs(300);
const ENGINE_WATCHDOG_TICK: Duration = Duration::from_secs(2);
#[cfg(unix)]
const SINGLETON_LOCK_PATH: &str = "/tmp/dev_headroom_menubar.lock";
#[cfg(unix)]
const SINGLETON_SOCKET_PATH: &str = "/tmp/dev_headroom_menubar.sock";

struct BridgeSession {
    child: CommandChild,
    events: Receiver<CommandEvent>,
}

#[cfg(unix)]
struct SingletonPrimary {
    lock: File,
    listener: UnixListener,
    socket_path: PathBuf,
}

#[cfg(unix)]
struct SingletonState {
    _lock: File,
    socket_path: PathBuf,
    listener: Mutex<Option<UnixListener>>,
}

#[cfg(unix)]
impl Drop for SingletonState {
    fn drop(&mut self) {
        self.cleanup_endpoint();
    }
}

#[cfg(unix)]
impl SingletonState {
    fn cleanup_endpoint(&self) {
        let _ = remove_owned_stale_socket(&self.socket_path);
    }
}

#[cfg(unix)]
enum SingletonClaim {
    Primary(SingletonPrimary),
    Secondary,
}

#[cfg(unix)]
fn notify_primary(socket_path: &PathBuf) -> bool {
    UnixStream::connect(socket_path).is_ok()
}

#[cfg(unix)]
fn remove_owned_stale_socket(socket_path: &PathBuf) -> Result<(), String> {
    let metadata = match fs::symlink_metadata(socket_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err("singleton activation endpoint is unavailable".into()),
    };
    if !metadata.file_type().is_socket()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.nlink() != 1
    {
        return Err("singleton activation endpoint is not safely owned".into());
    }
    fs::remove_file(socket_path)
        .map_err(|_| "stale singleton activation endpoint could not be removed".to_string())
}

#[cfg(unix)]
fn claim_singleton() -> Result<SingletonClaim, String> {
    let lock_path = PathBuf::from(SINGLETON_LOCK_PATH);
    let socket_path = PathBuf::from(SINGLETON_SOCKET_PATH);
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&lock_path)
        .map_err(|_| "singleton lock could not be opened safely".to_string())?;
    lock.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|_| "singleton lock could not be made private".to_string())?;
    let metadata = lock
        .metadata()
        .map_err(|_| "singleton lock metadata is unavailable".to_string())?;
    if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() } || metadata.nlink() != 1
    {
        return Err("singleton lock is not safely owned".into());
    }
    let claimed = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0;
    if !claimed {
        for _ in 0..40 {
            if notify_primary(&socket_path) {
                return Ok(SingletonClaim::Secondary);
            }
            thread::sleep(Duration::from_millis(50));
        }
        return Err("existing Headroom instance did not accept activation".into());
    }
    remove_owned_stale_socket(&socket_path)?;
    let listener = UnixListener::bind(&socket_path)
        .map_err(|_| "singleton activation endpoint could not be created".to_string())?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))
        .map_err(|_| "singleton activation endpoint could not be made private".to_string())?;
    listener
        .set_nonblocking(true)
        .map_err(|_| "singleton activation endpoint could not become non-blocking".to_string())?;
    Ok(SingletonClaim::Primary(SingletonPrimary {
        lock,
        listener,
        socket_path,
    }))
}

#[cfg(unix)]
fn start_singleton_listener(app: &AppHandle, listener: UnixListener) -> Result<(), String> {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let Ok(listener) = tokio::net::UnixListener::from_std(listener) else {
            eprintln!("headroom-desktop: singleton activation listener could not start");
            return;
        };
        while listener.accept().await.is_ok() {
            let _ = show_desktop_window(&app, None);
            request_scheduled_collection(&app, "activation");
        }
    });
    Ok(())
}

#[derive(Clone)]
struct DesktopEngine {
    session: Arc<Mutex<Option<BridgeSession>>>,
    recovery: Arc<Mutex<EngineRecoveryPolicy>>,
    starting: Arc<AtomicBool>,
}

impl Default for DesktopEngine {
    fn default() -> Self {
        Self {
            session: Arc::new(Mutex::new(None)),
            recovery: Arc::new(Mutex::new(EngineRecoveryPolicy::default())),
            starting: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[derive(Default)]
struct EngineRecoveryPolicy {
    crash_times: VecDeque<Instant>,
    retry_at: Option<Instant>,
    degraded: bool,
    running_since: Option<Instant>,
    last_failure_code: Option<&'static str>,
}

impl EngineRecoveryPolicy {
    fn prune_crashes(&mut self, now: Instant) {
        while self
            .crash_times
            .front()
            .is_some_and(|seen| now.duration_since(*seen) > ENGINE_CRASH_WINDOW)
        {
            self.crash_times.pop_front();
        }
    }

    fn record_failure(&mut self, now: Instant, entropy: u64, code: &'static str) -> Duration {
        self.prune_crashes(now);
        self.crash_times.push_back(now);
        self.running_since = None;
        self.last_failure_code = Some(code);
        if self.crash_times.len() as u32 >= ENGINE_RESTART_LIMIT {
            self.degraded = true;
            self.retry_at = Some(now + ENGINE_DEGRADED_COOLDOWN);
            return ENGINE_DEGRADED_COOLDOWN;
        }
        let exponent = (self.crash_times.len() as u32).saturating_sub(1).min(5);
        let raw = (ENGINE_RESTART_BASE * (1u32 << exponent)).min(ENGINE_RESTART_CAP);
        let spread = raw.as_millis() as u64 / 5;
        let offset = if spread == 0 {
            0
        } else {
            (entropy % (spread * 2 + 1)) as i64 - spread as i64
        };
        let millis = (raw.as_millis() as i64 + offset)
            .clamp(1, ENGINE_RESTART_CAP.as_millis() as i64) as u64;
        let delay = Duration::from_millis(millis);
        self.retry_at = Some(now + delay);
        delay
    }

    fn admits_restart(&self, now: Instant) -> bool {
        self.retry_at.is_none_or(|retry_at| now >= retry_at)
    }

    fn record_started(&mut self, now: Instant) {
        self.running_since = Some(now);
        self.retry_at = None;
        self.degraded = false;
    }

    fn record_success(&mut self, now: Instant) {
        self.retry_at = None;
        if self
            .running_since
            .is_some_and(|started| now.duration_since(started) >= ENGINE_STABLE_RESET)
        {
            self.crash_times.clear();
            self.last_failure_code = None;
        }
    }

    fn allow_manual_retry(&mut self) -> bool {
        if !self.degraded {
            return false;
        }
        self.degraded = false;
        self.retry_at = None;
        true
    }
}

#[derive(Default)]
struct CollectionPolicy {
    failure_count: u32,
    retry_at: Option<Instant>,
    last_success_at: Option<Instant>,
}

impl CollectionPolicy {
    fn record_success(&mut self, now: Instant) {
        self.failure_count = 0;
        self.retry_at = None;
        self.last_success_at = Some(now);
    }

    fn record_failure(&mut self, now: Instant, entropy: u64) -> Duration {
        self.failure_count = self.failure_count.saturating_add(1);
        let multiplier = 1u32 << self.failure_count.saturating_sub(1).min(6);
        let raw = (COLLECTION_RETRY_BASE * multiplier).min(COLLECTION_RETRY_CAP);
        let spread = raw.as_secs() / 5;
        let offset = if spread == 0 {
            0
        } else {
            (entropy % (spread * 2 + 1)) as i64 - spread as i64
        };
        let seconds =
            (raw.as_secs() as i64 + offset).clamp(1, COLLECTION_RETRY_CAP.as_secs() as i64) as u64;
        let delay = Duration::from_secs(seconds);
        self.retry_at = Some(now + delay);
        delay
    }

    fn due(&self, now: Instant, interval: Duration) -> bool {
        match self.retry_at {
            Some(retry_at) => now >= retry_at,
            None => self
                .last_success_at
                .is_none_or(|last| now.duration_since(last) >= interval),
        }
    }
}

struct CollectionScheduler {
    enabled: AtomicBool,
    running: AtomicBool,
    interval_seconds: AtomicU64,
    policy: Mutex<CollectionPolicy>,
}

impl Default for CollectionScheduler {
    fn default() -> Self {
        Self {
            enabled: AtomicBool::new(false),
            running: AtomicBool::new(false),
            interval_seconds: AtomicU64::new(COLLECTION_IDLE_INTERVAL.as_secs()),
            policy: Mutex::new(CollectionPolicy::default()),
        }
    }
}

struct StartupRouting {
    login_launch: bool,
    decision_complete: AtomicBool,
}

#[derive(Clone)]
struct DesktopSnapshot {
    revision: u64,
    theme: String,
    view: serde_json::Value,
}

#[derive(Default)]
struct DesktopStore {
    current: Mutex<Option<DesktopSnapshot>>,
}

impl DesktopStore {
    fn replace_view(&self, view: serde_json::Value) -> Result<DesktopSnapshot, String> {
        validate_desktop_view(&view)?;
        let mut current = self
            .current
            .lock()
            .map_err(|_| "desktop snapshot store is unavailable".to_string())?;
        let revision = current.as_ref().map_or(1, |snapshot| snapshot.revision + 1);
        // The engine owns the authoritative, validated preference. Every
        // settings mutation returns a fresh view, so theme propagation must
        // follow that view instead of retaining the previous in-memory value.
        let theme = configured_theme(&view).to_string();
        let snapshot = DesktopSnapshot {
            revision,
            theme,
            view,
        };
        *current = Some(snapshot.clone());
        Ok(snapshot)
    }

    fn set_theme(&self, theme: &str) -> Result<DesktopSnapshot, String> {
        if !DESKTOP_THEMES.contains(&theme) {
            return Err("desktop theme is invalid".into());
        }
        let mut current = self
            .current
            .lock()
            .map_err(|_| "desktop snapshot store is unavailable".to_string())?;
        let previous = current
            .as_ref()
            .ok_or_else(|| "desktop snapshot is unavailable".to_string())?;
        let snapshot = DesktopSnapshot {
            revision: previous.revision + 1,
            theme: theme.to_string(),
            view: previous.view.clone(),
        };
        *current = Some(snapshot.clone());
        Ok(snapshot)
    }

    fn snapshot(&self) -> Result<DesktopSnapshot, String> {
        self.current
            .lock()
            .map_err(|_| "desktop snapshot store is unavailable".to_string())?
            .clone()
            .ok_or_else(|| "desktop snapshot is unavailable".to_string())
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct WindowPlacement {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

struct DesktopUiState {
    placement: Mutex<Option<WindowPlacement>>,
    last_tray_rect: Mutex<Option<tauri::Rect>>,
    popover_opened_at: Mutex<Option<Instant>>,
    remember_window: AtomicBool,
    window_preference_seen: AtomicBool,
}

impl Default for DesktopUiState {
    fn default() -> Self {
        Self {
            placement: Mutex::new(None),
            last_tray_rect: Mutex::new(None),
            popover_opened_at: Mutex::new(None),
            remember_window: AtomicBool::new(true),
            window_preference_seen: AtomicBool::new(false),
        }
    }
}

struct AppState {
    /// Validated loopback widget URL. Never changes after startup.
    widget_url: Url,
    /// True once the widget page finished loading in the webview.
    widget_loaded: AtomicBool,
    /// Result of the most recent reachability probe.
    last_probe_ok: AtomicBool,
    /// When the panel was last hidden because it lost focus.
    last_auto_hide: Mutex<Option<Instant>>,
    /// The tray icon's screen rect from its most recent event, for anchoring
    /// the panel like a native menu-bar popover (right edges aligned).
    last_tray_rect: Mutex<Option<tauri::Rect>>,
}

/// Resolve the widget URL from the environment, falling back to the default
/// if it is missing or fails loopback validation.
fn resolve_widget_url() -> Url {
    let default = Url::parse(DEFAULT_WIDGET_URL).expect("default widget URL is valid");
    let Some(raw) = std::env::var(WIDGET_URL_ENV)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
    else {
        return default;
    };
    match Url::parse(&raw) {
        Ok(url) if url.scheme() == "http" && is_acceptable_widget_host(&url) => {
            canonicalize_loopback(url)
        }
        Ok(_) => {
            eprintln!(
                "headroom-menubar: {WIDGET_URL_ENV}={raw} is not a loopback http URL; \
                 using {DEFAULT_WIDGET_URL}"
            );
            default
        }
        Err(err) => {
            eprintln!(
                "headroom-menubar: {WIDGET_URL_ENV}={raw} is not a valid URL ({err}); \
                 using {DEFAULT_WIDGET_URL}"
            );
            default
        }
    }
}

/// NUMERIC-loopback-only host check: a literal `127.0.0.0/8` or `::1`
/// address. A hostname — `localhost` included — is NOT accepted here: names
/// go through the OS resolver, and a poisoned hosts file or DNS entry could
/// answer with a public IP.
fn is_loopback_host(url: &Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let host = host.trim_start_matches('[').trim_end_matches(']');
    host.parse::<std::net::IpAddr>()
        .map(|ip| ip.is_loopback())
        .unwrap_or(false)
}

/// Accepted spellings for the env override: a numeric loopback literal, or
/// the literal `localhost` name — which is then PINNED to `127.0.0.1` by
/// `canonicalize_loopback` without ever consulting the resolver.
fn is_acceptable_widget_host(url: &Url) -> bool {
    url.host_str()
        .is_some_and(|host| host.eq_ignore_ascii_case("localhost"))
        || is_loopback_host(url)
}

/// Pin a `localhost` host to the numeric IPv4 loopback literal. After this,
/// nothing — the probe, webview navigation, or "Open in Browser" — carries a
/// resolvable name.
fn canonicalize_loopback(mut url: Url) -> Url {
    if url
        .host_str()
        .is_some_and(|host| host.eq_ignore_ascii_case("localhost"))
    {
        url.set_host(Some("127.0.0.1"))
            .expect("http URL accepts a loopback IP host");
    }
    url
}

/// URL of the bundled offline-fallback page (`dist/index.html`) as served by
/// Tauri's embedded asset protocol. Windows uses `http://tauri.localhost`,
/// macOS/Linux use `tauri://localhost` (`useHttpsScheme` is left at its
/// default of `false`).
fn fallback_page_url() -> Url {
    let raw = if cfg!(windows) {
        "http://tauri.localhost/index.html"
    } else {
        "tauri://localhost/index.html"
    };
    Url::parse(raw).expect("static fallback URL is valid")
}

/// True when `url` IS this platform's bundled fallback document. Tauri
/// simplifies `WebviewUrl::App("index.html")` to the asset-origin BASE for
/// the initial navigation (`tauri://localhost` with an empty path), while
/// our explicit fallback navigations use `/index.html` — so the base, its
/// `/` form, and `/index.html` are the three spellings of this ONE document.
/// Nothing else on the origin passes (no other assets, ports, queries, or
/// the other platform's asset form: on macOS/Linux `http://tauri.localhost`
/// is ordinary HTTP to a resolvable hostname).
fn is_bundled_page(url: &Url) -> bool {
    let fallback = fallback_page_url();
    url.scheme() == fallback.scheme()
        && url.host_str() == fallback.host_str()
        && url.port() == fallback.port()
        && matches!(url.path(), "" | "/" | "/index.html")
        && url.query().is_none()
        && url.fragment().is_none()
}

/// Navigation policy for the webview: ONLY the bundled fallback page, the
/// initial blank document (`about:blank` exactly — no other `about:` URL),
/// and the ONE configured widget document (scheme, numeric loopback host,
/// port, path, and query all equal). Everything else — remote origins,
/// other loopback origins, other paths on the widget origin — is refused.
fn navigation_allowed(widget: &Url, url: &Url) -> bool {
    if url.as_str() == "about:blank" || is_bundled_page(url) {
        return true;
    }
    url.scheme() == widget.scheme()
        && is_loopback_host(url)
        && url.host_str() == widget.host_str()
        && url.port_or_known_default() == widget.port_or_known_default()
        && url.path() == widget.path()
        && url.query() == widget.query()
}

/// Cheap HTTP reachability probe, run off the UI thread. A bare TCP connect
/// is not enough: an `ssh -L` listener accepts even when the remote side is
/// down, so require at least the start of an HTTP response.
fn server_reachable(url: &Url) -> bool {
    let Some(host) = url.host_str() else {
        return false;
    };
    let host = host.trim_start_matches('[').trim_end_matches(']');
    // numeric loopback only — the probe NEVER performs a resolver lookup
    let Ok(ip) = host.parse::<std::net::IpAddr>() else {
        return false;
    };
    if !ip.is_loopback() {
        return false;
    }
    let port = url.port_or_known_default().unwrap_or(80);
    let addr = std::net::SocketAddr::new(ip, port);
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, PROBE_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!(
        "HEAD {} HTTP/1.1\r\nHost: {}:{}\r\nConnection: close\r\n\r\n",
        url.path(),
        host,
        port
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 5];
    stream.read_exact(&mut buf).is_ok() && &buf == b"HTTP/"
}

/// GET a small same-origin document from the widget server over the same
/// numeric-loopback-only raw socket the reachability probe uses (never a
/// resolver lookup, never a non-loopback address). Returns the response body
/// for an exact 200 with no transfer-encoding tricks; None on anything else.
/// The whole exchange is bounded by a wall-clock deadline — a drip-feeding
/// endpoint can't hold the worker beyond it.
fn fetch_loopback(url: &Url, path: &str) -> Option<String> {
    let host = url.host_str()?;
    let host = host.trim_start_matches('[').trim_end_matches(']');
    let ip: std::net::IpAddr = host.parse().ok()?;
    if !ip.is_loopback() {
        return None;
    }
    let port = url.port_or_known_default().unwrap_or(80);
    let deadline = Instant::now() + FEED_DEADLINE;
    let mut stream =
        TcpStream::connect_timeout(&std::net::SocketAddr::new(ip, port), PROBE_TIMEOUT).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(1))).ok()?;
    stream.set_write_timeout(Some(PROBE_TIMEOUT)).ok()?;
    // bracket IPv6 literals: an unbracketed `Host: ::1:8377` is malformed
    let host_header = if host.contains(':') {
        format!("[{host}]:{port}")
    } else {
        format!("{host}:{port}")
    };
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    let mut raw = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        if Instant::now() >= deadline {
            return None;
        }
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&chunk[..n]);
                if raw.len() > FEED_MAX_BYTES {
                    return None;
                }
            }
            // a 1s read timeout loops back to the deadline check above
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut => {}
            Err(_) => return None,
        }
    }
    let text = String::from_utf8(raw).ok()?;
    let (head, body) = text.split_once("\r\n\r\n")?;
    let status_line = head.lines().next()?;
    // exact status parse: "HTTP/1.x 200 ..." — not a prefix match
    let mut parts = status_line.split(' ');
    let version_ok = matches!(parts.next(), Some("HTTP/1.1") | Some("HTTP/1.0"));
    if !version_ok || parts.next() != Some("200") {
        return None;
    }
    if head.to_ascii_lowercase().contains("transfer-encoding") {
        return None;
    }
    Some(body.to_owned())
}

/// The fleet's average 5h battery as a fraction, from `/widget.json` on the
/// widget server — applying the SAME fail-closed feed contract as the
/// JavaScript clients before trusting a single number: exact schema, a
/// current freshness block with sane timing (no future evaluation beyond a
/// small NTP tolerance, age within the snapshot window), and only live
/// (current/limited) accounts' 5h windows. A current window contributes its
/// left_percent, a limited one an honest 0; held/stale never count. `None`
/// when anything is off — the icon then shows the no-reading dash.
fn fetch_avg_battery(widget: &Url) -> Option<f32> {
    let body = fetch_loopback(widget, "/widget.json")?;
    let value: serde_json::Value = serde_json::from_str(&body).ok()?;
    if value.get("schema").and_then(|s| s.as_str()) != Some("headroom_widget@1") {
        return None;
    }
    let freshness = value.get("freshness")?;
    if freshness.get("state").and_then(|s| s.as_str()) != Some("current") {
        return None;
    }
    let evaluated_at = freshness.get("evaluated_at").and_then(|v| v.as_f64())?;
    let age = freshness.get("age_seconds").and_then(|v| v.as_f64())?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs_f64();
    if !evaluated_at.is_finite() || !age.is_finite() || age < 0.0 {
        return None;
    }
    if evaluated_at > now + 10.0 {
        return None; // future evaluation beyond NTP tolerance: never trust
    }
    if age + (now - evaluated_at).max(0.0) > 900.0 {
        return None; // outside the snapshot freshness window: held, not live
    }
    let accounts = value.get("accounts")?.as_array()?;
    let mut sum = 0.0f64;
    let mut count = 0u32;
    for account in accounts {
        let account_state = account.get("state").and_then(|s| s.as_str());
        if !matches!(account_state, Some("current") | Some("limited")) {
            continue;
        }
        let window = account.get("windows").and_then(|w| w.get("5h"));
        let state = window.and_then(|w| w.get("state")).and_then(|s| s.as_str());
        match state {
            Some("current") => {
                let Some(left) = window
                    .and_then(|w| w.get("left_percent"))
                    .and_then(|v| v.as_f64())
                else {
                    continue;
                };
                if (0.0..=100.0).contains(&left) {
                    sum += left;
                    count += 1;
                }
            }
            Some("limited") => {
                count += 1; // an exhausted tank is an honest 0, not missing
            }
            _ => {}
        }
    }
    (count > 0).then(|| (sum / f64::from(count) / 100.0) as f32)
}

/// One icon fetch in flight at a time: manual Refresh spam and a slow feed
/// must not accumulate worker threads.
static ICON_FETCH_ACTIVE: AtomicBool = AtomicBool::new(false);

/// Redraw the tray icon (and tooltip) from the latest feed reading.
fn update_tray_icon(app: &AppHandle) {
    if ICON_FETCH_ACTIVE.swap(true, Ordering::SeqCst) {
        return; // a fetch is already running; it will paint the result
    }
    let level = fetch_avg_battery(&app.state::<AppState>().widget_url);
    ICON_FETCH_ACTIVE.store(false, Ordering::SeqCst);
    let Some(tray) = app.tray_by_id("headroom-tray") else {
        return;
    };
    let (rgba, width, height) = icon::tray_icon_rgba(level);
    let _ = tray.set_icon(Some(Image::new_owned(rgba, width, height)));
    let _ = tray.set_icon_as_template(true);
    let tooltip = match level {
        Some(level) => format!("headroom — avg 5h battery {}%", (level * 100.0).round()),
        None => "headroom — no live reading".to_owned(),
    };
    let _ = tray.set_tooltip(Some(tooltip));
}

/// Escape a string for embedding inside a double-quoted JS string literal.
fn js_string_literal(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '<' => out.push_str("\\u003c"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Tell the fallback page (if it is the current document) what state we are
/// in: `"probing"` or `"down"`. No-op on any other page.
fn push_fallback_status(window: &WebviewWindow, status: &str) {
    let script = format!(
        "window.__headroomStatus && window.__headroomStatus({});",
        js_string_literal(status)
    );
    let _ = window.eval(&script);
}

/// Probe the widget server and point the webview at the right document:
/// widget page when reachable, bundled fallback when not. `force` reloads
/// the widget page even if it is already loaded (menu "Refresh").
///
/// Blocking (network probe) — always call from a worker thread.
fn sync_view(app: &AppHandle, force: bool) {
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
        return;
    };
    let state = app.state::<AppState>();
    let reachable = server_reachable(&state.widget_url);
    state.last_probe_ok.store(reachable, Ordering::SeqCst);
    let loaded = state.widget_loaded.load(Ordering::SeqCst);

    if reachable {
        if force || !loaded {
            let _ = window.navigate(state.widget_url.clone());
        }
    } else if !loaded {
        // Nothing ever loaded (cold start with the tunnel down): the bundled
        // fallback with its tunnel hint is the only useful view.
        let _ = window.navigate(fallback_page_url());
        push_fallback_status(&window, "down");
    }
    // else: the widget page is already showing — KEEP IT. The page demotes
    // itself (staleness banner, held tones) through its own failed feed
    // fetches; a probe failure must never replace live content with an
    // error page (the last known reading beats a dead-end screen).
}

/// Run `sync_view` on a worker thread so tray/menu handlers never block the
/// UI thread on a network probe.
fn sync_view_async(app: &AppHandle, force: bool) {
    let app = app.clone();
    std::thread::spawn(move || sync_view(&app, force));
}

/// Toggle the popover like a native menu-bar panel.
fn toggle_popover(app: &AppHandle) {
    let Some(window) = app.get_webview_window(WINDOW_LABEL) else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
        return;
    }
    let state = app.state::<AppState>();
    // If the click that re-focused the tray icon just auto-hid the panel,
    // this click means "close" — don't instantly re-open it.
    if let Some(hidden_at) = *state
        .last_auto_hide
        .lock()
        .expect("last_auto_hide poisoned")
    {
        if hidden_at.elapsed() < REOPEN_SUPPRESS {
            return;
        }
    }
    // Size the panel to the current fleet BEFORE anchoring, so every account
    // fits instead of the last rows (often the Codex slots) falling below a
    // fixed-height fold. Clamped to the screen; the page scrolls if a very
    // large fleet still exceeds the clamp.
    fit_popover_height(&window);
    // Anchor like a native menu-bar popover: the panel's RIGHT edge aligns
    // with the icon's right edge, directly below the menu bar (macOS). The
    // positioner presets can't express right-edge alignment, so the panel is
    // placed from the tray rect captured off the click event; when no rect
    // has been seen yet (or off macOS), fall back to the platform-aware
    // constrained TrayCenter.
    if !anchor_below_tray(&window, app) {
        let _ = window.move_window_constrained(Position::TrayCenter);
    }
    let _ = window.show();
    let _ = window.set_focus();
    sync_view_async(app, false);
}

/// Set the popover to its fixed preferred height, clamped so it always fits
/// on the current display (leaving room below the menu bar). The panel
/// scrolls internally for the rest of the fleet — nothing is ever off-screen
/// or clipped, which is what a native menu-bar dropdown does.
fn fit_popover_height(window: &WebviewWindow) {
    let max_height = window
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            let scale = monitor.scale_factor();
            // leave the menu bar + a small bottom margin
            monitor.size().to_logical::<f64>(scale).height - 80.0
        })
        .unwrap_or(POPOVER_HEIGHT);
    let height = POPOVER_HEIGHT.min(max_height).max(POPOVER_HEIGHT_MIN);
    let _ = window.set_size(tauri::LogicalSize::new(WINDOW_WIDTH, height));
}

/// Place the panel right-edge-aligned under the tray icon (macOS layout).
/// Returns false when the placement can't be computed.
fn anchor_below_tray(window: &WebviewWindow, app: &AppHandle) -> bool {
    if !cfg!(target_os = "macos") {
        return false;
    }
    let rect = *app
        .state::<AppState>()
        .last_tray_rect
        .lock()
        .expect("last_tray_rect poisoned");
    let Some(rect) = rect else {
        return false;
    };
    let scale = window.scale_factor().unwrap_or(1.0);
    let to_logical_pos = |value: tauri::Position| match value {
        tauri::Position::Physical(p) => (f64::from(p.x) / scale, f64::from(p.y) / scale),
        tauri::Position::Logical(p) => (p.x, p.y),
    };
    let to_logical_size = |value: tauri::Size| match value {
        tauri::Size::Physical(s) => (f64::from(s.width) / scale, f64::from(s.height) / scale),
        tauri::Size::Logical(s) => (s.width, s.height),
    };
    let (tray_x, tray_y) = to_logical_pos(rect.position);
    let (tray_w, tray_h) = to_logical_size(rect.size);
    let mut x = tray_x + tray_w - WINDOW_WIDTH; // right edges aligned
    let y = tray_y + tray_h + 5.0; // just below the menu bar
                                   // clamp against the monitor that CONTAINS the tray icon — the hidden
                                   // window's current monitor may be a different display entirely
    let monitor = monitor_containing(window, tray_x, tray_y)
        .or_else(|| window.current_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let monitor_scale = monitor.scale_factor();
        let position = monitor.position().to_logical::<f64>(monitor_scale);
        let size = monitor.size().to_logical::<f64>(monitor_scale);
        x = x
            .max(position.x + 8.0)
            .min(position.x + size.width - WINDOW_WIDTH - 8.0);
    }
    window
        .set_position(tauri::LogicalPosition::new(x, y))
        .is_ok()
}

/// The monitor whose logical bounds contain the given point, if any.
#[cfg(target_os = "macos")]
fn monitor_containing(window: &WebviewWindow, x: f64, y: f64) -> Option<tauri::Monitor> {
    for monitor in window.available_monitors().ok()? {
        let scale = monitor.scale_factor();
        let position = monitor.position().to_logical::<f64>(scale);
        let size = monitor.size().to_logical::<f64>(scale);
        if x >= position.x
            && x < position.x + size.width
            && y >= position.y
            && y < position.y + size.height
        {
            return Some(monitor);
        }
    }
    None
}

/// Build the hidden popover webview window.
fn build_popover(app: &AppHandle, widget_url: &Url) -> tauri::Result<WebviewWindow> {
    // The window.open stub is non-configurable, so no page script can delete
    // it to restore the native popup path: new windows are denied outright.
    //
    // On macOS the window itself provides the native panel chrome (HUD
    // vibrancy + rounded corners via window effects), so the widget page's
    // own wall — the gradient, grid, and blobs behind the glass card — is
    // stripped with injected CSS and the glass card sits directly on the
    // real material, like a system popover. Elsewhere the page keeps its
    // bundled wall.
    let embed_css = if cfg!(target_os = "macos") {
        // The system's popover material provides the frost; the page lays a
        // dark tint OVER it so the panel stays legible on light content
        // behind the window — the same trick the built-in dark panels use
        // (pure vibrancy alone washes out ~40% white-through on a white
        // page). Tint alpha tuned to match the system Bluetooth/Wi-Fi
        // panels' density. backdrop-filter must stay off — it forces an
        // opaque layer in WKWebView and blacks out window transparency.
        "html,body{background:transparent !important}\
         .hr{background:transparent !important;padding:0 !important}\
         .hr-wall{display:none !important}\
         .hr-pop{width:100% !important;max-width:none !important;\
                 min-height:100vh;border:0 !important;\
                 border-radius:0 !important;\
                 background:rgba(13,16,24,.62) !important;\
                 backdrop-filter:none !important;\
                 -webkit-backdrop-filter:none !important;\
                 box-shadow:none !important}"
    } else {
        ""
    };
    let init_script = format!(
        "window.__HEADROOM_WIDGET_URL__ = {widget};\n\
         Object.defineProperty(window, \"open\", {{\n\
           value: function () {{ return null; }},\n\
           writable: false, configurable: false\n\
         }});\n\
         addEventListener(\"DOMContentLoaded\", function () {{\n\
           var css = {css};\n\
           if (!css || location.href !== window.__HEADROOM_WIDGET_URL__) return;\n\
           var style = document.createElement(\"style\");\n\
           style.textContent = css;\n\
           document.head.appendChild(style);\n\
         }});",
        widget = js_string_literal(widget_url.as_str()),
        css = js_string_literal(embed_css)
    );
    let navigation_widget = widget_url.clone();
    let builder =
        WebviewWindowBuilder::new(app, WINDOW_LABEL, WebviewUrl::App("index.html".into()))
            .title("headroom")
            .inner_size(WINDOW_WIDTH, WINDOW_HEIGHT)
            .visible(false)
            .decorations(false)
            .resizable(false)
            .maximizable(false)
            .minimizable(false)
            .closable(false)
            .always_on_top(true)
            .skip_taskbar(true)
            .visible_on_all_workspaces(true)
            .transparent(true);
    // The system's adaptive popover material provides the standard dropdown
    // background; the page paints nothing over it. Dark theme pinned so the
    // widget's ink colors always sit on the dark material.
    #[cfg(target_os = "macos")]
    let builder = builder.theme(Some(tauri::Theme::Dark)).effects(
        tauri::utils::config::WindowEffectsConfig {
            effects: vec![tauri::utils::WindowEffect::Popover],
            state: Some(tauri::utils::WindowEffectState::Active),
            radius: Some(PANEL_RADIUS),
            color: None,
        },
    );
    builder
        .initialization_script(&init_script)
        .on_navigation(move |url| {
            let allowed = navigation_allowed(&navigation_widget, url);
            if !allowed {
                eprintln!(
                    "headroom-menubar: blocked navigation outside the widget document: {url}"
                );
            }
            allowed
        })
        .on_page_load(|window, payload| {
            if payload.event() != PageLoadEvent::Finished {
                return;
            }
            let state = window.app_handle().state::<AppState>();
            let on_widget = {
                let current = payload.url();
                let widget = &state.widget_url;
                current.scheme() == widget.scheme()
                    && current.host_str() == widget.host_str()
                    && current.port_or_known_default() == widget.port_or_known_default()
            };
            state.widget_loaded.store(on_widget, Ordering::SeqCst);
            if !on_widget && !state.last_probe_ok.load(Ordering::SeqCst) {
                push_fallback_status(&window, "down");
            }
        })
        .build()
}

/// Round the window's contentView layer so EVERYTHING in the window — the
/// native material AND the webview above it — is clipped to the popover
/// radius. (The effect view's own radius cannot clip the webview, which is
/// why CSS-only rounding left square material corners peeking out.) The
/// window shadow is recomputed from the clipped shape.
#[cfg(target_os = "macos")]
fn round_window_corners(window: &WebviewWindow, radius: f64) {
    use objc2::msg_send;
    use objc2::runtime::{AnyObject, Bool};
    let Ok(ns_window) = window.ns_window() else {
        return;
    };
    let ns_window = ns_window.cast::<AnyObject>();
    if ns_window.is_null() {
        return;
    }
    unsafe {
        let content_view: *mut AnyObject = msg_send![ns_window, contentView];
        if content_view.is_null() {
            return;
        }
        let _: () = msg_send![content_view, setWantsLayer: Bool::YES];
        let layer: *mut AnyObject = msg_send![content_view, layer];
        if layer.is_null() {
            return;
        }
        let _: () = msg_send![layer, setCornerRadius: radius];
        let _: () = msg_send![layer, setMasksToBounds: Bool::YES];
        let _: () = msg_send![ns_window, invalidateShadow];
    }
}

/// Build the tray icon with its context menu.
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let refresh = MenuItem::with_id(app, "refresh", "Refresh", true, None::<&str>)?;
    let open_browser =
        MenuItem::with_id(app, "open-browser", "Open in Browser", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Headroom", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&refresh, &open_browser, &quit])?;

    // Startup icon: the battery-head with no reading yet; the feed watcher
    // fills the level in as soon as /widget.json answers.
    let (rgba, width, height) = icon::tray_icon_rgba(None);
    TrayIconBuilder::with_id("headroom-tray")
        .icon(Image::new_owned(rgba, width, height))
        .icon_as_template(true)
        .tooltip("headroom")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "refresh" => {
                sync_view_async(app, true);
                let icon_app = app.clone();
                std::thread::spawn(move || update_tray_icon(&icon_app));
            }
            "open-browser" => {
                let url = app.state::<AppState>().widget_url.clone();
                if let Err(err) = open::that_detached(url.as_str()) {
                    eprintln!("headroom-menubar: failed to open browser: {err}");
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            // Feed the positioner so Position::Tray* knows where the icon is.
            tauri_plugin_positioner::on_tray_event(tray.app_handle(), &event);
            // Remember the icon's rect for native-style right-edge anchoring.
            if let TrayIconEvent::Click { rect, .. }
            | TrayIconEvent::Enter { rect, .. }
            | TrayIconEvent::Move { rect, .. } = &event
            {
                let app = tray.app_handle();
                *app.state::<AppState>()
                    .last_tray_rect
                    .lock()
                    .expect("last_tray_rect poisoned") = Some(*rect);
            }
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_popover(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

fn bridge_request(id: &str, command: &str, args: serde_json::Value) -> Vec<u8> {
    let request = serde_json::json!({
        "schema": DESKTOP_BRIDGE_SCHEMA,
        "id": id,
        "command": command,
        "args": args,
    });
    let mut frame = serde_json::to_vec(&request).expect("static bridge request serializes");
    frame.push(b'\n');
    frame
}

fn parse_bridge_response(frame: &[u8], expected_id: &str) -> Result<serde_json::Value, String> {
    if frame.len() > MAX_BRIDGE_FRAME_BYTES {
        return Err("desktop engine returned an oversized response".into());
    }
    let value: serde_json::Value = serde_json::from_slice(frame)
        .map_err(|_| "desktop engine returned an invalid response".to_string())?;
    if value.get("schema").and_then(serde_json::Value::as_str) != Some(DESKTOP_BRIDGE_SCHEMA)
        || value.get("id").and_then(serde_json::Value::as_str) != Some(expected_id)
    {
        return Err("desktop engine returned an incompatible response".into());
    }
    if value.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
        let code = value
            .pointer("/error/code")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("unknown_error");
        return Err(format!("desktop engine rejected request ({code})"));
    }
    value
        .get("result")
        .cloned()
        .ok_or_else(|| "desktop engine response is missing its result".to_string())
}

fn validate_desktop_view(view: &serde_json::Value) -> Result<(), String> {
    if view.get("schema").and_then(serde_json::Value::as_str) != Some(DESKTOP_VIEW_SCHEMA)
        || !view
            .get("accounts")
            .is_some_and(serde_json::Value::is_array)
    {
        return Err("bundled desktop engine returned an invalid snapshot".into());
    }
    Ok(())
}

fn configured_theme(view: &serde_json::Value) -> &'static str {
    view.pointer("/settings/theme")
        .and_then(serde_json::Value::as_str)
        .and_then(|theme| DESKTOP_THEMES.iter().copied().find(|known| *known == theme))
        .unwrap_or("terminal")
}

fn validate_desktop_bootstrap(
    handshake: &serde_json::Value,
    view: &serde_json::Value,
) -> Result<(), String> {
    if handshake.get("product").and_then(serde_json::Value::as_str) != Some("headroom")
        || handshake
            .get("bridge_schema")
            .and_then(serde_json::Value::as_str)
            != Some(DESKTOP_BRIDGE_SCHEMA)
        || handshake.get("runtime").and_then(serde_json::Value::as_str) != Some("frozen")
    {
        return Err("bundled desktop engine is incompatible".into());
    }
    let supports_desktop = handshake
        .get("capabilities")
        .and_then(serde_json::Value::as_array)
        .is_some_and(|values| {
            [
                "discover",
                "adopt",
                "refresh",
                "claude_login",
                "codex_device_login",
                "onboarding",
                "account_lifecycle",
                "reauthentication",
                "resilient_collection",
                "validated_settings",
            ]
            .iter()
            .all(|name| values.iter().any(|value| value == name))
        });
    if !supports_desktop {
        return Err("bundled desktop engine lacks the required capability".into());
    }
    validate_desktop_view(view)
}

fn engine_failure_code(error: &str) -> &'static str {
    match error {
        "bundled desktop engine startup timed out" => "engine_startup_timeout",
        "bundled desktop engine stopped during startup" => "engine_startup_exited",
        "bundled desktop engine did not accept the handshake"
        | "bundled desktop engine did not accept the discovery request" => {
            "engine_startup_pipe_failed"
        }
        "desktop engine request timed out" => "engine_request_timeout",
        "desktop engine stopped unexpectedly" => "engine_exited_mid_request",
        "desktop engine communication failed" => "engine_communication_failed",
        _ if error.contains("incompatible") || error.contains("required capability") => {
            "engine_incompatible"
        }
        _ => "engine_start_failed",
    }
}

fn desktop_startup_handshake() -> serde_json::Value {
    serde_json::json!({
        "product": "headroom",
        "product_version": "starting",
        "bridge_schema": DESKTOP_BRIDGE_SCHEMA,
        "architecture": std::env::consts::ARCH,
        "runtime": "unavailable",
    })
}

fn desktop_startup_view() -> serde_json::Value {
    serde_json::json!({
        "schema": DESKTOP_VIEW_SCHEMA,
        "mode": "recovery",
        "recovery_code": "engine_starting",
        "accounts": [],
        "candidates": [],
        "freshness": {
            "state": "held", "age_seconds": null,
            "reason": "engine_starting",
        },
        "headline": {
            "avg_5h_left_percent": null, "avg_7d_left_percent": null,
            "current_accounts": 0, "total_accounts": 0,
        },
        "settings": {
            "title": "Headroom", "theme": "terminal",
            "redact_emails": true, "reserve_percent": 0,
            "auto_handoff": true, "refresh_interval_seconds": 300,
            "provider_paths": {}, "preferred_terminal": "terminal",
            "remember_window": true,
            "notifications": {
                "enabled": false, "reset_enabled": false,
                "global_threshold_percent": 20,
                "provider_threshold_percent": {},
            },
        },
    })
}

fn bootstrap_sidecar(
    app: &AppHandle,
) -> Result<(serde_json::Value, serde_json::Value, BridgeSession), String> {
    let command = app
        .shell()
        .sidecar("headroom-engine")
        .map_err(|_| "bundled desktop engine could not be resolved".to_string())?;
    let (mut events, mut child) = command
        .spawn()
        .map_err(|_| "bundled desktop engine could not be started".to_string())?;
    if child
        .write(&bridge_request(
            "startup-handshake",
            "handshake",
            serde_json::json!({"accepted_schemas": [DESKTOP_BRIDGE_SCHEMA]}),
        ))
        .is_err()
    {
        let _ = child.kill();
        return Err("bundled desktop engine did not accept the handshake".into());
    }
    if child
        .write(&bridge_request(
            "startup-view",
            "discover",
            serde_json::json!({}),
        ))
        .is_err()
    {
        let _ = child.kill();
        return Err("bundled desktop engine did not accept the discovery request".into());
    }

    let deadline = Instant::now() + SIDECAR_STARTUP_TIMEOUT;
    let mut handshake = None;
    let mut view = None;
    while handshake.is_none() || view.is_none() {
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            let _ = child.kill();
            return Err("bundled desktop engine startup timed out".into());
        };
        let next = tauri::async_runtime::block_on(async {
            tokio::time::timeout(remaining, events.recv()).await
        });
        match next {
            Ok(Some(CommandEvent::Stdout(frame))) => {
                let parsed = if handshake.is_none() {
                    parse_bridge_response(&frame, "startup-handshake")
                } else {
                    parse_bridge_response(&frame, "startup-view")
                };
                match parsed {
                    Ok(value) if handshake.is_none() => handshake = Some(value),
                    Ok(value) => view = Some(value),
                    Err(error) => {
                        let _ = child.kill();
                        return Err(error);
                    }
                }
            }
            Ok(Some(CommandEvent::Stderr(bytes))) => {
                // stderr may eventually include provider output. Record only
                // its size so credentials or account details cannot leak.
                eprintln!(
                    "headroom-desktop: engine diagnostic ({} bytes)",
                    bytes.len()
                );
            }
            Ok(Some(CommandEvent::Terminated(_))) | Ok(None) => {
                return Err("bundled desktop engine stopped during startup".into());
            }
            Ok(Some(CommandEvent::Error(_))) => {
                let _ = child.kill();
                return Err("bundled desktop engine communication failed".into());
            }
            Ok(Some(_)) => {}
            Err(_) => {
                let _ = child.kill();
                return Err("bundled desktop engine startup timed out".into());
            }
        }
    }

    let handshake = handshake.expect("startup loop collected handshake");
    let view = view.expect("startup loop collected desktop view");
    if let Err(error) = validate_desktop_bootstrap(&handshake, &view) {
        let _ = child.kill();
        return Err(error);
    }
    Ok((handshake, view, BridgeSession { child, events }))
}

fn desktop_initialization_script(
    handshake: &serde_json::Value,
    snapshot: &DesktopSnapshot,
    surface: &str,
) -> String {
    let payload = serde_json::json!({
        "bridge": handshake,
        "view": snapshot.view,
        "revision": snapshot.revision,
        "theme": snapshot.theme,
        "surface": surface,
    });
    let payload_json = serde_json::to_string(&payload).expect("desktop bootstrap serializes");
    let payload_literal = serde_json::to_string(&payload_json).expect("JSON string serializes");
    format!(
        "Object.defineProperty(window,'__HEADROOM_BOOTSTRAP__',{{value:JSON.parse({payload_literal}),writable:false,configurable:false}});"
    )
}

fn build_desktop_window(
    app: &AppHandle,
    handshake: &serde_json::Value,
    snapshot: &DesktopSnapshot,
    visible: bool,
) -> tauri::Result<WebviewWindow> {
    let script = desktop_initialization_script(handshake, snapshot, "main");
    WebviewWindowBuilder::new(
        app,
        DESKTOP_WINDOW_LABEL,
        WebviewUrl::App("index.html".into()),
    )
    .title("Headroom")
    .inner_size(900.0, 650.0)
    .min_inner_size(680.0, 480.0)
    .resizable(true)
    .visible(visible)
    .initialization_script(&script)
    .on_navigation(|url| url.as_str() == "about:blank" || is_bundled_page(url))
    .build()
}

fn build_desktop_popover(
    app: &AppHandle,
    handshake: &serde_json::Value,
    snapshot: &DesktopSnapshot,
) -> tauri::Result<WebviewWindow> {
    let script = desktop_initialization_script(handshake, snapshot, "popover");
    let builder = WebviewWindowBuilder::new(
        app,
        DESKTOP_POPOVER_LABEL,
        WebviewUrl::App("index.html".into()),
    )
    .title("Headroom")
    .inner_size(DESKTOP_POPOVER_WIDTH, DESKTOP_POPOVER_HEIGHT)
    .visible(false)
    .decorations(false)
    .resizable(false)
    .maximizable(false)
    .minimizable(false)
    .closable(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .visible_on_all_workspaces(true)
    .transparent(true);
    #[cfg(target_os = "macos")]
    let builder = builder.theme(Some(tauri::Theme::Dark)).effects(
        tauri::utils::config::WindowEffectsConfig {
            effects: vec![tauri::utils::WindowEffect::Popover],
            state: Some(tauri::utils::WindowEffectState::Active),
            radius: Some(PANEL_RADIUS),
            color: None,
        },
    );
    builder
        .initialization_script(&script)
        .on_navigation(|url| url.as_str() == "about:blank" || is_bundled_page(url))
        .build()
}

fn snapshot_envelope(snapshot: &DesktopSnapshot) -> serde_json::Value {
    serde_json::json!({
        "revision": snapshot.revision,
        "theme": snapshot.theme,
        "view": snapshot.view,
    })
}

fn collection_enabled(view: &serde_json::Value) -> bool {
    view.get("mode").and_then(serde_json::Value::as_str) == Some("ready")
        && view
            .get("accounts")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|accounts| !accounts.is_empty())
}

fn configured_refresh_interval(view: &serde_json::Value) -> Duration {
    let seconds = view
        .pointer("/settings/refresh_interval_seconds")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(COLLECTION_IDLE_INTERVAL.as_secs())
        .clamp(
            COLLECTION_INTERVAL_MIN_SECONDS,
            COLLECTION_INTERVAL_MAX_SECONDS,
        );
    Duration::from_secs(seconds)
}

fn login_launch_requires_window(view: &serde_json::Value) -> bool {
    matches!(
        view.get("mode").and_then(serde_json::Value::as_str),
        Some("onboarding" | "recovery")
    )
}

fn finish_login_launch_routing(app: &AppHandle, view: &serde_json::Value) {
    let Some(startup) = app.try_state::<StartupRouting>() else {
        return;
    };
    if !startup.login_launch || startup.decision_complete.swap(true, Ordering::SeqCst) {
        return;
    }
    if login_launch_requires_window(view) {
        let _ = show_desktop_window(app, Some("settings"));
    }
}

fn apply_window_preference(app: &AppHandle, view: &serde_json::Value) {
    let remember = view
        .pointer("/settings/remember_window")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(true);
    let ui = app.state::<DesktopUiState>();
    let previous = ui.remember_window.swap(remember, Ordering::SeqCst);
    let seen = ui.window_preference_seen.swap(true, Ordering::SeqCst);
    if remember || (seen && !previous) {
        return;
    }
    if let Ok(path) = window_state_path(app) {
        let _ = fs::remove_file(path);
    }
    if let Some(window) = app.get_webview_window(DESKTOP_WINDOW_LABEL) {
        let _ = window.center();
        if let (Ok(position), Ok(size)) = (window.outer_position(), window.inner_size()) {
            if let Ok(mut placement) = ui.placement.lock() {
                *placement = Some(WindowPlacement {
                    x: position.x,
                    y: position.y,
                    width: size.width,
                    height: size.height,
                });
            }
        }
    }
}

fn show_login_launch_recovery(app: &AppHandle) {
    let Some(startup) = app.try_state::<StartupRouting>() else {
        return;
    };
    if startup.login_launch && !startup.decision_complete.swap(true, Ordering::SeqCst) {
        let _ = show_desktop_window(app, None);
    }
}

fn push_snapshot_to_windows(app: &AppHandle, snapshot: &DesktopSnapshot) {
    let payload = serde_json::to_string(&snapshot_envelope(snapshot))
        .expect("sanitized desktop snapshot serializes");
    let payload_literal = serde_json::to_string(&payload).expect("snapshot string serializes");
    let script = format!(
        "window.__headroomApplySnapshot&&window.__headroomApplySnapshot(JSON.parse({payload_literal}));"
    );
    for label in [DESKTOP_WINDOW_LABEL, DESKTOP_POPOVER_LABEL] {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.eval(&script);
        }
    }
    update_desktop_tray_icon(app, &snapshot.view);
}

fn push_bridge_to_windows(app: &AppHandle, handshake: &serde_json::Value) {
    let payload = serde_json::to_string(handshake).expect("sanitized bridge handshake serializes");
    let payload_literal = serde_json::to_string(&payload).expect("bridge string serializes");
    let script = format!(
        "window.__headroomApplyBridge&&window.__headroomApplyBridge(JSON.parse({payload_literal}));"
    );
    for label in [DESKTOP_WINDOW_LABEL, DESKTOP_POPOVER_LABEL] {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.eval(&script);
        }
    }
}

fn publish_desktop_view(
    app: &AppHandle,
    view: serde_json::Value,
) -> Result<DesktopSnapshot, String> {
    let snapshot = app.state::<DesktopStore>().replace_view(view)?;
    if let Some(scheduler) = app.try_state::<CollectionScheduler>() {
        scheduler
            .enabled
            .store(collection_enabled(&snapshot.view), Ordering::SeqCst);
        scheduler.interval_seconds.store(
            configured_refresh_interval(&snapshot.view).as_secs(),
            Ordering::SeqCst,
        );
    }
    apply_window_preference(app, &snapshot.view);
    push_snapshot_to_windows(app, &snapshot);
    finish_login_launch_routing(app, &snapshot.view);
    Ok(snapshot)
}

fn publish_result_view(app: &AppHandle, value: &serde_json::Value) -> Result<(), String> {
    if value.get("schema").and_then(serde_json::Value::as_str) == Some(DESKTOP_VIEW_SCHEMA) {
        publish_desktop_view(app, value.clone())?;
    } else if value
        .get("view")
        .and_then(|view| view.get("schema"))
        .and_then(serde_json::Value::as_str)
        == Some(DESKTOP_VIEW_SCHEMA)
    {
        publish_desktop_view(app, value["view"].clone())?;
    }
    Ok(())
}

fn bootstrap_desktop_engine(app: &AppHandle, engine: &DesktopEngine) -> Result<(), String> {
    let missing = engine
        .session
        .lock()
        .map_err(|_| "desktop engine state is unavailable".to_string())?
        .is_none();
    if !missing {
        return Ok(());
    }
    {
        let recovery = engine
            .recovery
            .lock()
            .map_err(|_| "desktop engine recovery is unavailable".to_string())?;
        if !recovery.admits_restart(Instant::now()) {
            return Err(if recovery.degraded {
                "desktop engine is safely degraded".into()
            } else {
                "desktop engine restart is in backoff".into()
            });
        }
    }
    match bootstrap_sidecar(app) {
        Ok((handshake, view, session)) => {
            let mut guard = engine
                .session
                .lock()
                .map_err(|_| "desktop engine state is unavailable".to_string())?;
            if guard.is_none() {
                *guard = Some(session);
            } else {
                let _ = session.child.kill();
            }
            drop(guard);
            if let Ok(mut recovery) = engine.recovery.lock() {
                recovery.record_started(Instant::now());
            }
            push_bridge_to_windows(app, &handshake);
            publish_desktop_view(app, view).map(|_| ())
        }
        Err(error) => {
            if let Ok(mut recovery) = engine.recovery.lock() {
                recovery.record_failure(
                    Instant::now(),
                    collection_entropy(),
                    engine_failure_code(&error),
                );
            }
            Err(error)
        }
    }
}

fn ensure_desktop_engine(app: &AppHandle, engine: &DesktopEngine) -> Result<(), String> {
    if engine.starting.swap(true, Ordering::SeqCst) {
        return Err("desktop engine restart is already running".into());
    }
    let outcome = bootstrap_desktop_engine(app, engine);
    engine.starting.store(false, Ordering::SeqCst);
    outcome
}

fn request_desktop_engine(
    engine: &DesktopEngine,
    command: &str,
    args: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let mut guard = engine
        .session
        .lock()
        .map_err(|_| "desktop engine state is unavailable".to_string())?;
    let outcome: Result<serde_json::Value, (String, bool)> = (|| {
        let session = guard
            .as_mut()
            .ok_or_else(|| ("desktop engine is not running".to_string(), false))?;
        let request_id = format!("desktop-{command}");
        session
            .child
            .write(&bridge_request(&request_id, command, args))
            .map_err(|_| {
                (
                    "desktop engine did not accept the request".to_string(),
                    true,
                )
            })?;
        let deadline = Instant::now() + SIDECAR_REQUEST_TIMEOUT;
        loop {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or_else(|| ("desktop engine request timed out".to_string(), true))?;
            let next = tauri::async_runtime::block_on(async {
                tokio::time::timeout(remaining, session.events.recv()).await
            });
            match next {
                Ok(Some(CommandEvent::Stdout(frame))) => {
                    return parse_bridge_response(&frame, &request_id).map_err(|error| {
                        let fatal = !error.starts_with("desktop engine rejected request (");
                        (error, fatal)
                    });
                }
                Ok(Some(CommandEvent::Stderr(bytes))) => {
                    eprintln!(
                        "headroom-desktop: engine diagnostic ({} bytes)",
                        bytes.len()
                    );
                }
                Ok(Some(CommandEvent::Terminated(_))) | Ok(None) => {
                    return Err(("desktop engine stopped unexpectedly".into(), true));
                }
                Ok(Some(CommandEvent::Error(_))) => {
                    return Err(("desktop engine communication failed".into(), true));
                }
                Ok(Some(_)) => {}
                Err(_) => {
                    return Err(("desktop engine request timed out".into(), true));
                }
            }
        }
    })();
    match outcome {
        Ok(value) => {
            if let Ok(mut recovery) = engine.recovery.lock() {
                recovery.record_success(Instant::now());
            }
            Ok(value)
        }
        Err((error, fatal)) => {
            // A malformed, late, or missing response makes frame ordering
            // unknowable. Retire that session so a later request cannot
            // accidentally consume the prior response. A well-formed bridge
            // rejection is a business error and leaves the engine usable.
            if fatal {
                if let Some(session) = guard.take() {
                    let _ = session.child.kill();
                }
                if let Ok(mut recovery) = engine.recovery.lock() {
                    recovery.record_failure(
                        Instant::now(),
                        collection_entropy(),
                        engine_failure_code(&error),
                    );
                }
            }
            Err(error)
        }
    }
}

async fn engine_command(
    app: AppHandle,
    engine: DesktopEngine,
    command: &'static str,
    args: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let worker = engine.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        request_desktop_engine(&worker, command, args)
    })
    .await
    .map_err(|_| "desktop engine task failed".to_string())?;
    let missing = engine
        .session
        .lock()
        .map(|session| session.is_none())
        .unwrap_or(true);
    if result.is_err() && missing && !engine.starting.load(Ordering::SeqCst) {
        let (state, code, retry) = engine.recovery.lock().map_or(
            ("degraded", Some("engine_state_unavailable"), None),
            |policy| {
                (
                    if policy.degraded {
                        "degraded"
                    } else {
                        "recovering"
                    },
                    policy.last_failure_code,
                    policy
                        .retry_at
                        .and_then(|at| at.checked_duration_since(Instant::now())),
                )
            },
        );
        set_engine_state_windows(&app, state, code);
        if state != "degraded" {
            schedule_engine_restart(app, retry.unwrap_or(Duration::from_millis(100)));
        }
    }
    result
}

#[tauri::command]
async fn desktop_discover(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "discover",
        serde_json::json!({}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_onboarding(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    action: String,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "onboarding",
        serde_json::json!({"action": action}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_account_action(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    action: String,
    name: String,
    new_name: Option<String>,
    reserved: Option<bool>,
    confirmation: Option<String>,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "account_action",
        serde_json::json!({
            "action": action,
            "name": name,
            "new_name": new_name,
            "reserved": reserved,
            "confirmation": confirmation,
        }),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_adopt(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    candidate_id: String,
    name: String,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "adopt",
        serde_json::json!({"candidate_id": candidate_id, "name": name}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_refresh(app: AppHandle) -> Result<serde_json::Value, String> {
    request_scheduled_collection(&app, "manual");
    Ok(app.state::<DesktopStore>().snapshot()?.view)
}

#[tauri::command]
async fn desktop_start_claude_login(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    name: String,
    expected_email: Option<String>,
) -> Result<serde_json::Value, String> {
    engine_command(
        app,
        state.inner().clone(),
        "start_claude_login",
        serde_json::json!({"name": name, "expected_email": expected_email}),
    )
    .await
}

#[tauri::command]
async fn desktop_start_codex_login(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    name: String,
    expected_email: Option<String>,
) -> Result<serde_json::Value, String> {
    engine_command(
        app,
        state.inner().clone(),
        "start_codex_login",
        serde_json::json!({"name": name, "expected_email": expected_email}),
    )
    .await
}

#[tauri::command]
async fn desktop_start_reauthentication(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    name: String,
) -> Result<serde_json::Value, String> {
    engine_command(
        app,
        state.inner().clone(),
        "start_reauthentication",
        serde_json::json!({"name": name}),
    )
    .await
}

fn verified_device_url(raw: &str) -> Option<Url> {
    let url = Url::parse(raw).ok()?;
    if url.scheme() == "https"
        && url.host_str() == Some("auth.openai.com")
        && url.username().is_empty()
        && url.password().is_none()
        && matches!(url.port(), None | Some(443))
        && url.path() == "/codex/device"
        && url.query().is_none()
        && url.fragment().is_none()
    {
        Some(url)
    } else {
        None
    }
}

#[tauri::command]
async fn desktop_open_device_url(url: String) -> Result<(), String> {
    let verified = verified_device_url(&url)
        .ok_or_else(|| "device authorization URL is invalid".to_string())?;
    open::that_detached(verified.as_str())
        .map_err(|_| "device authorization URL could not be opened".to_string())
}

#[tauri::command]
async fn desktop_login_status(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    job_id: String,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "login_status",
        serde_json::json!({"job_id": job_id}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_cancel_login(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    job_id: String,
) -> Result<serde_json::Value, String> {
    let value = engine_command(
        app.clone(),
        state.inner().clone(),
        "cancel_login",
        serde_json::json!({"job_id": job_id}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

async fn update_desktop_settings(
    app: AppHandle,
    engine: DesktopEngine,
    patch: serde_json::Value,
) -> Result<serde_json::Value, String> {
    if !patch.is_object() {
        return Err("desktop settings patch is invalid".into());
    }
    let value = engine_command(
        app.clone(),
        engine,
        "update_settings",
        serde_json::json!({"patch": patch}),
    )
    .await?;
    publish_result_view(&app, &value)?;
    Ok(value)
}

#[tauri::command]
async fn desktop_set_theme(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    theme: String,
) -> Result<serde_json::Value, String> {
    update_desktop_settings(
        app,
        state.inner().clone(),
        serde_json::json!({"theme": theme}),
    )
    .await
}

#[tauri::command]
async fn desktop_update_settings(
    app: AppHandle,
    state: tauri::State<'_, DesktopEngine>,
    patch: serde_json::Value,
) -> Result<serde_json::Value, String> {
    update_desktop_settings(app, state.inner().clone(), patch).await
}

#[tauri::command]
fn desktop_launch_at_login_status(app: AppHandle) -> Result<bool, String> {
    app.autolaunch()
        .is_enabled()
        .map_err(|_| "launch at login status is unavailable".to_string())
}

#[tauri::command]
fn desktop_set_launch_at_login(app: AppHandle, enabled: bool) -> Result<bool, String> {
    let manager = app.autolaunch();
    let result = if enabled {
        manager.enable()
    } else {
        manager.disable()
    };
    result.map_err(|_| "launch at login could not be updated".to_string())?;
    let actual = manager
        .is_enabled()
        .map_err(|_| "launch at login status is unavailable".to_string())?;
    if actual != enabled {
        return Err("launch at login did not reach the requested state".into());
    }
    Ok(actual)
}

#[tauri::command]
fn desktop_snapshot(state: tauri::State<'_, DesktopStore>) -> Result<serde_json::Value, String> {
    Ok(snapshot_envelope(&state.snapshot()?))
}

#[tauri::command]
fn desktop_retry_engine(app: AppHandle) -> Result<serde_json::Value, String> {
    let engine = app.state::<DesktopEngine>().inner().clone();
    let allowed = engine
        .recovery
        .lock()
        .map_err(|_| "desktop engine recovery is unavailable".to_string())?
        .allow_manual_retry();
    if !allowed {
        return Err("desktop engine manual retry is available only in degraded mode".into());
    }
    if let Ok(mut policy) = app.state::<CollectionScheduler>().policy.lock() {
        policy.retry_at = None;
    }
    set_engine_state_windows(&app, "recovering", Some("engine_manual_retry"));
    start_desktop_engine_async(&app);
    Ok(snapshot_envelope(&app.state::<DesktopStore>().snapshot()?))
}

fn show_desktop_window(app: &AppHandle, panel: Option<&str>) -> Result<(), String> {
    let window = app
        .get_webview_window(DESKTOP_WINDOW_LABEL)
        .ok_or_else(|| "desktop window is unavailable".to_string())?;
    window
        .show()
        .map_err(|_| "desktop window could not be shown".to_string())?;
    let _ = window.unminimize();
    window
        .set_focus()
        .map_err(|_| "desktop window could not be focused".to_string())?;
    if let Some(panel) = panel {
        let panel = js_string_literal(panel);
        let _ = window.eval(&format!(
            "window.__headroomOpenPanel&&window.__headroomOpenPanel({panel});"
        ));
    }
    if let Some(popover) = app.get_webview_window(DESKTOP_POPOVER_LABEL) {
        let _ = popover.hide();
    }
    Ok(())
}

#[tauri::command]
fn desktop_show_dashboard(app: AppHandle) -> Result<(), String> {
    show_desktop_window(&app, None)
}

#[tauri::command]
fn desktop_show_settings(app: AppHandle) -> Result<(), String> {
    show_desktop_window(&app, Some("settings"))
}

#[tauri::command]
fn desktop_hide_dashboard(app: AppHandle) -> Result<(), String> {
    save_window_placement(&app)?;
    app.get_webview_window(DESKTOP_WINDOW_LABEL)
        .ok_or_else(|| "desktop window is unavailable".to_string())?
        .hide()
        .map_err(|_| "desktop window could not be hidden".to_string())
}

#[tauri::command]
fn desktop_quit(app: AppHandle) {
    app.exit(0);
}

fn stop_desktop_engine(app: &AppHandle) {
    let Some(mut session) = app
        .state::<DesktopEngine>()
        .session
        .lock()
        .expect("desktop engine mutex poisoned")
        .take()
    else {
        return;
    };
    let _ = session.child.write(&bridge_request(
        "desktop-shutdown",
        "shutdown",
        serde_json::json!({}),
    ));
    let deadline = Instant::now() + Duration::from_secs(6);
    let mut terminated = false;
    while let Some(remaining) = deadline.checked_duration_since(Instant::now()) {
        let next = tauri::async_runtime::block_on(async {
            tokio::time::timeout(remaining, session.events.recv()).await
        });
        match next {
            Ok(Some(CommandEvent::Stdout(frame))) => {
                let _ = parse_bridge_response(&frame, "desktop-shutdown");
            }
            Ok(Some(CommandEvent::Terminated(_))) | Ok(None) => {
                terminated = true;
                break;
            }
            Err(_) => break,
            Ok(Some(_)) => {}
        }
    }
    if !terminated {
        let _ = session.child.kill();
    }
}

fn desktop_average_level(view: &serde_json::Value) -> Option<f32> {
    let value = view
        .pointer("/headline/avg_5h_left_percent")
        .and_then(serde_json::Value::as_f64)?;
    (value.is_finite() && (0.0..=100.0).contains(&value)).then_some((value / 100.0) as f32)
}

fn update_desktop_tray_icon(app: &AppHandle, view: &serde_json::Value) {
    let Some(tray) = app.tray_by_id("headroom-tray") else {
        return;
    };
    let level = desktop_average_level(view);
    let (rgba, width, height) = icon::tray_icon_rgba(level);
    let _ = tray.set_icon(Some(Image::new_owned(rgba, width, height)));
    let _ = tray.set_icon_as_template(true);
    let tooltip = level.map_or_else(
        || "Headroom — no current five-hour reading".to_string(),
        |level| {
            format!(
                "Headroom — {}% average five-hour headroom",
                (level * 100.0).round()
            )
        },
    );
    let _ = tray.set_tooltip(Some(tooltip));
}

fn set_refresh_state_windows(app: &AppHandle, state: &str) {
    let state = js_string_literal(state);
    let script =
        format!("window.__headroomSetRefreshState&&window.__headroomSetRefreshState({state});");
    for label in [DESKTOP_WINDOW_LABEL, DESKTOP_POPOVER_LABEL] {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.eval(&script);
        }
    }
}

fn set_engine_state_windows(app: &AppHandle, state: &str, code: Option<&str>) {
    let state = js_string_literal(state);
    let code = code.map(js_string_literal).unwrap_or_else(|| "null".into());
    let script = format!(
        "window.__headroomSetRefreshState&&window.__headroomSetRefreshState({state},{code});"
    );
    for label in [DESKTOP_WINDOW_LABEL, DESKTOP_POPOVER_LABEL] {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.eval(&script);
        }
    }
}

fn collection_view_outcome(view: &serde_json::Value) -> (bool, bool) {
    let accounts = view
        .get("accounts")
        .and_then(serde_json::Value::as_array)
        .cloned()
        .unwrap_or_default();
    if accounts.is_empty() {
        return (true, false);
    }
    let codes: Vec<&str> = accounts
        .iter()
        .filter_map(|account| {
            account
                .get("diagnostic_code")
                .and_then(serde_json::Value::as_str)
        })
        .collect();
    let transient = codes.iter().any(|code| {
        matches!(
            *code,
            "provider_rate_limited"
                | "provider_server_error"
                | "provider_timeout"
                | "provider_offline"
                | "provider_unavailable"
                | "malformed_provider_response"
                | "codex_provider_backoff"
                | "codex_app_server_throttled"
                | "usage_source_rate_limited"
        )
    });
    let has_usable = accounts.iter().any(|account| {
        matches!(
            account.get("state").and_then(serde_json::Value::as_str),
            Some("current" | "limited")
        )
    });
    let offline = !has_usable
        && codes
            .iter()
            .any(|code| matches!(*code, "provider_offline" | "provider_unavailable"));
    (!transient, offline)
}

fn collection_entropy() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos() as u64)
}

fn engine_recovery_state(engine: &DesktopEngine) -> &'static str {
    engine
        .recovery
        .lock()
        .map(|policy| {
            if policy.degraded {
                "degraded"
            } else {
                "recovering"
            }
        })
        .unwrap_or("degraded")
}

fn engine_recovery_status(engine: &DesktopEngine) -> (&'static str, Option<&'static str>) {
    engine
        .recovery
        .lock()
        .map(|policy| {
            (
                if policy.degraded {
                    "degraded"
                } else {
                    "recovering"
                },
                policy.last_failure_code,
            )
        })
        .unwrap_or(("degraded", Some("engine_state_unavailable")))
}

#[cfg(unix)]
fn sidecar_process_alive(session: &BridgeSession) -> bool {
    let result = unsafe { libc::kill(session.child.pid() as i32, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(not(unix))]
fn sidecar_process_alive(_session: &BridgeSession) -> bool {
    true
}

fn schedule_engine_restart(app: AppHandle, delay: Duration) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(delay).await;
        start_desktop_engine_async(&app);
    });
}

fn start_desktop_engine_async(app: &AppHandle) {
    let engine = app.state::<DesktopEngine>().inner().clone();
    let has_session = engine
        .session
        .lock()
        .map(|session| session.is_some())
        .unwrap_or(false);
    if has_session
        || engine
            .starting
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
    {
        return;
    }
    let (_, code) = engine_recovery_status(&engine);
    set_engine_state_windows(app, "recovering", code);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let bootstrap_app = app.clone();
        let bootstrap_engine = engine.clone();
        let outcome = tauri::async_runtime::spawn_blocking(move || {
            bootstrap_desktop_engine(&bootstrap_app, &bootstrap_engine)
        })
        .await
        .map_err(|_| "desktop engine restart task failed".to_string())
        .and_then(|result| result);
        engine.starting.store(false, Ordering::SeqCst);
        match outcome {
            Ok(()) => {
                let enabled = app
                    .state::<CollectionScheduler>()
                    .enabled
                    .load(Ordering::SeqCst);
                if enabled {
                    request_scheduled_collection(&app, "engine_restart");
                } else {
                    set_refresh_state_windows(&app, "current");
                }
            }
            Err(_) => {
                show_login_launch_recovery(&app);
                let (state, code, retry) = engine.recovery.lock().map_or(
                    ("degraded", Some("engine_state_unavailable"), None),
                    |policy| {
                        (
                            if policy.degraded {
                                "degraded"
                            } else {
                                "recovering"
                            },
                            policy.last_failure_code,
                            policy.retry_at.and_then(|retry_at| {
                                retry_at.checked_duration_since(Instant::now())
                            }),
                        )
                    },
                );
                set_engine_state_windows(&app, state, code);
                if state != "degraded" {
                    schedule_engine_restart(app, retry.unwrap_or(Duration::from_millis(100)));
                }
            }
        }
    });
}

fn start_engine_watchdog(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(ENGINE_WATCHDOG_TICK);
        interval.tick().await;
        loop {
            interval.tick().await;
            let engine = app.state::<DesktopEngine>().inner().clone();
            let dead = engine
                .session
                .lock()
                .map(|session| {
                    session
                        .as_ref()
                        .is_some_and(|session| !sidecar_process_alive(session))
                })
                .unwrap_or(false);
            if !dead {
                if let Ok(mut recovery) = engine.recovery.lock() {
                    recovery.record_success(Instant::now());
                }
                continue;
            }
            let removed = engine
                .session
                .lock()
                .ok()
                .and_then(|mut session| session.take())
                .is_some();
            if !removed {
                continue;
            }
            let (delay, degraded, code) = engine.recovery.lock().map_or(
                (
                    ENGINE_DEGRADED_COOLDOWN,
                    true,
                    Some("engine_state_unavailable"),
                ),
                |mut policy| {
                    let delay = policy.record_failure(
                        Instant::now(),
                        collection_entropy(),
                        "engine_unexpected_exit",
                    );
                    (delay, policy.degraded, policy.last_failure_code)
                },
            );
            set_engine_state_windows(&app, if degraded { "degraded" } else { "recovering" }, code);
            if !degraded {
                schedule_engine_restart(app.clone(), delay);
            }
        }
    });
}

fn request_scheduled_collection(app: &AppHandle, trigger: &str) {
    let scheduler = app.state::<CollectionScheduler>();
    if !scheduler.enabled.load(Ordering::SeqCst) {
        return;
    }
    let now = Instant::now();
    let (in_backoff, recently_current) = scheduler.policy.lock().map_or((false, false), |policy| {
        (
            policy.retry_at.is_some_and(|retry_at| now < retry_at),
            policy.retry_at.is_none()
                && policy
                    .last_success_at
                    .is_some_and(|last| now.duration_since(last) < Duration::from_secs(60)),
        )
    });
    if trigger == "wake" && recently_current {
        return;
    }
    if in_backoff
        && !matches!(
            trigger,
            "wake" | "connectivity" | "engine_restart" | "engine_retry"
        )
    {
        set_refresh_state_windows(app, "backoff");
        return;
    }
    if scheduler.running.swap(true, Ordering::SeqCst) {
        return;
    }
    set_refresh_state_windows(
        app,
        if matches!(trigger, "engine_restart" | "engine_retry") {
            "recovering"
        } else {
            "refreshing"
        },
    );
    let app = app.clone();
    let engine = app.state::<DesktopEngine>().inner().clone();
    tauri::async_runtime::spawn(async move {
        let bootstrap_app = app.clone();
        let bootstrap_engine = engine.clone();
        let ensured = tauri::async_runtime::spawn_blocking(move || {
            ensure_desktop_engine(&bootstrap_app, &bootstrap_engine)
        })
        .await
        .map_err(|_| "desktop engine restart task failed".to_string())
        .and_then(|result| result);
        let result = match ensured {
            Ok(()) => {
                engine_command(
                    app.clone(),
                    engine.clone(),
                    "refresh",
                    serde_json::json!({}),
                )
                .await
            }
            Err(error) => Err(error),
        };
        let now = Instant::now();
        let (settled, diagnostic) = match result {
            Ok(view) => {
                let published = publish_result_view(&app, &view).is_ok();
                let current_view = view
                    .get("view")
                    .filter(|nested| nested.get("schema").is_some())
                    .unwrap_or(&view);
                let (success, offline) = collection_view_outcome(current_view);
                if published && success {
                    if let Ok(mut policy) = app.state::<CollectionScheduler>().policy.lock() {
                        policy.record_success(now);
                    }
                    ("current", None)
                } else {
                    if let Ok(mut policy) = app.state::<CollectionScheduler>().policy.lock() {
                        policy.record_failure(now, collection_entropy());
                    }
                    if offline {
                        ("offline", None)
                    } else {
                        ("backoff", None)
                    }
                }
            }
            Err(_) => {
                if let Ok(mut policy) = app.state::<CollectionScheduler>().policy.lock() {
                    policy.record_failure(now, collection_entropy());
                }
                let (state, code) = engine_recovery_status(&engine);
                (state, code)
            }
        };
        app.state::<CollectionScheduler>()
            .running
            .store(false, Ordering::SeqCst);
        if diagnostic.is_some() {
            set_engine_state_windows(&app, settled, diagnostic);
        } else {
            set_refresh_state_windows(&app, settled);
        }
    });
}

fn start_collection_scheduler(app: &AppHandle, snapshot: &DesktopSnapshot) {
    let enabled = collection_enabled(&snapshot.view);
    let scheduler = app.state::<CollectionScheduler>();
    scheduler.enabled.store(enabled, Ordering::SeqCst);
    scheduler.interval_seconds.store(
        configured_refresh_interval(&snapshot.view).as_secs(),
        Ordering::SeqCst,
    );
    if enabled {
        let now = Instant::now();
        let freshness_current = snapshot
            .view
            .pointer("/freshness/state")
            .and_then(serde_json::Value::as_str)
            == Some("current");
        let age = snapshot
            .view
            .pointer("/freshness/age_seconds")
            .and_then(serde_json::Value::as_u64);
        if freshness_current {
            if let Ok(mut policy) = app.state::<CollectionScheduler>().policy.lock() {
                policy.last_success_at = age
                    .and_then(|seconds| now.checked_sub(Duration::from_secs(seconds)))
                    .or(Some(now));
            }
        } else {
            request_scheduled_collection(app, "activation");
        }
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(COLLECTION_TICK);
        interval.tick().await;
        loop {
            interval.tick().await;
            let due = app
                .state::<CollectionScheduler>()
                .policy
                .lock()
                .map(|policy| {
                    let seconds = app
                        .state::<CollectionScheduler>()
                        .interval_seconds
                        .load(Ordering::SeqCst);
                    policy.due(Instant::now(), Duration::from_secs(seconds))
                })
                .unwrap_or(false);
            if due {
                request_scheduled_collection(&app, "schedule");
            }
        }
    });
}

fn toggle_desktop_popover(app: &AppHandle) {
    let Some(window) = app.get_webview_window(DESKTOP_POPOVER_LABEL) else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
        return;
    }
    let max_height = window
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| {
            monitor
                .size()
                .to_logical::<f64>(monitor.scale_factor())
                .height
                - 80.0
        })
        .unwrap_or(DESKTOP_POPOVER_HEIGHT);
    let height = DESKTOP_POPOVER_HEIGHT.min(max_height).max(320.0);
    let _ = window.set_size(tauri::LogicalSize::new(DESKTOP_POPOVER_WIDTH, height));
    let _ = window.move_window_constrained(Position::TrayCenter);
    *app.state::<DesktopUiState>()
        .popover_opened_at
        .lock()
        .expect("desktop popover state poisoned") = Some(Instant::now());
    let _ = window.show();
    let _ = window.set_focus();
}

fn build_desktop_tray(app: &AppHandle) -> tauri::Result<()> {
    let dashboard = MenuItem::with_id(app, "dashboard", "Open Dashboard", true, None::<&str>)?;
    let refresh = MenuItem::with_id(app, "refresh", "Refresh", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Headroom", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&dashboard, &refresh, &settings, &quit])?;
    let snapshot = app.state::<DesktopStore>().snapshot().ok();
    let level = snapshot
        .as_ref()
        .and_then(|snapshot| desktop_average_level(&snapshot.view));
    let (rgba, width, height) = icon::tray_icon_rgba(level);
    TrayIconBuilder::with_id("headroom-tray")
        .icon(Image::new_owned(rgba, width, height))
        .icon_as_template(true)
        .tooltip("Headroom")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "dashboard" => {
                let _ = show_desktop_window(app, None);
            }
            "refresh" => request_scheduled_collection(app, "manual"),
            "settings" => {
                let _ = show_desktop_window(app, Some("settings"));
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            tauri_plugin_positioner::on_tray_event(tray.app_handle(), &event);
            if let TrayIconEvent::Click { rect, .. }
            | TrayIconEvent::Enter { rect, .. }
            | TrayIconEvent::Move { rect, .. } = &event
            {
                *tray
                    .app_handle()
                    .state::<DesktopUiState>()
                    .last_tray_rect
                    .lock()
                    .expect("desktop tray state poisoned") = Some(*rect);
            }
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_desktop_popover(tray.app_handle());
            }
        })
        .build(app)?;
    Ok(())
}

fn window_state_path(app: &AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|directory| directory.join("window-state.json"))
        .map_err(|_| "desktop window state directory is unavailable".to_string())
}

fn valid_window_placement(value: &serde_json::Value) -> Option<WindowPlacement> {
    if value.get("schema").and_then(serde_json::Value::as_str) != Some(WINDOW_STATE_SCHEMA) {
        return None;
    }
    let number = |key: &str| value.get(key).and_then(serde_json::Value::as_i64);
    let placement = WindowPlacement {
        x: i32::try_from(number("x")?).ok()?,
        y: i32::try_from(number("y")?).ok()?,
        width: u32::try_from(number("width")?).ok()?,
        height: u32::try_from(number("height")?).ok()?,
    };
    ((640..=6000).contains(&placement.width)
        && (440..=4000).contains(&placement.height)
        && (-50_000..=50_000).contains(&placement.x)
        && (-50_000..=50_000).contains(&placement.y))
    .then_some(placement)
}

fn placement_intersects_monitor(placement: WindowPlacement, monitor: &tauri::Monitor) -> bool {
    let monitor_position = monitor.position();
    let monitor_size = monitor.size();
    let left = placement.x.max(monitor_position.x);
    let top = placement.y.max(monitor_position.y);
    let right = (i64::from(placement.x) + i64::from(placement.width))
        .min(i64::from(monitor_position.x) + i64::from(monitor_size.width));
    let bottom = (i64::from(placement.y) + i64::from(placement.height))
        .min(i64::from(monitor_position.y) + i64::from(monitor_size.height));
    i64::from(left) + 120 <= right && i64::from(top) + 80 <= bottom
}

fn load_window_placement(app: &AppHandle) -> Option<WindowPlacement> {
    let path = window_state_path(app).ok()?;
    let metadata = fs::symlink_metadata(&path).ok()?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > 8192 {
        return None;
    }
    let value: serde_json::Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    valid_window_placement(&value)
}

fn save_window_placement(app: &AppHandle) -> Result<(), String> {
    if !app
        .state::<DesktopUiState>()
        .remember_window
        .load(Ordering::SeqCst)
    {
        if let Ok(path) = window_state_path(app) {
            let _ = fs::remove_file(path);
        }
        return Ok(());
    }
    let Some(placement) = *app
        .state::<DesktopUiState>()
        .placement
        .lock()
        .map_err(|_| "desktop window state is unavailable".to_string())?
    else {
        return Ok(());
    };
    let path = window_state_path(app)?;
    let directory = path
        .parent()
        .ok_or_else(|| "desktop window state path is invalid".to_string())?;
    fs::create_dir_all(directory)
        .map_err(|_| "desktop window state directory could not be created".to_string())?;
    #[cfg(unix)]
    fs::set_permissions(directory, fs::Permissions::from_mode(0o700))
        .map_err(|_| "desktop window state permissions could not be set".to_string())?;
    let temporary = directory.join(format!(".window-state-{}.tmp", std::process::id()));
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    options.mode(0o600);
    let mut file = options
        .open(&temporary)
        .map_err(|_| "desktop window state could not be prepared".to_string())?;
    let value = serde_json::json!({
        "schema": WINDOW_STATE_SCHEMA,
        "x": placement.x,
        "y": placement.y,
        "width": placement.width,
        "height": placement.height,
    });
    serde_json::to_writer(&mut file, &value)
        .map_err(|_| "desktop window state could not be encoded".to_string())?;
    file.write_all(b"\n")
        .and_then(|_| file.sync_all())
        .map_err(|_| "desktop window state could not be committed".to_string())?;
    fs::rename(&temporary, &path)
        .map_err(|_| "desktop window state could not be published".to_string())?;
    Ok(())
}

fn restore_window_placement(window: &WebviewWindow) {
    let app = window.app_handle();
    let restored = load_window_placement(app).filter(|placement| {
        window
            .available_monitors()
            .map(|monitors| {
                monitors
                    .iter()
                    .any(|monitor| placement_intersects_monitor(*placement, monitor))
            })
            .unwrap_or(false)
    });
    if let Some(placement) = restored {
        let _ = window.set_size(PhysicalSize::new(placement.width, placement.height));
        let _ = window.set_position(PhysicalPosition::new(placement.x, placement.y));
    }
    let placement = (|| {
        let position = window.outer_position().ok()?;
        let size = window.inner_size().ok()?;
        Some(WindowPlacement {
            x: position.x,
            y: position.y,
            width: size.width,
            height: size.height,
        })
    })();
    *app.state::<DesktopUiState>()
        .placement
        .lock()
        .expect("desktop window state poisoned") = placement;
}

pub fn run() {
    let login_launch = std::env::args_os()
        .any(|argument| argument == std::ffi::OsStr::new("--headroom-login-launch"));
    #[cfg(unix)]
    let SingletonPrimary {
        lock,
        listener,
        socket_path,
    } = match claim_singleton() {
        Ok(SingletonClaim::Primary(primary)) => primary,
        Ok(SingletonClaim::Secondary) => return,
        Err(error) => {
            eprintln!("headroom-desktop: {error}");
            return;
        }
    };
    let builder = tauri::Builder::default();
    #[cfg(unix)]
    let builder = builder.manage(SingletonState {
        _lock: lock,
        socket_path,
        listener: Mutex::new(Some(listener)),
    });
    let app = builder
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .arg("--headroom-login-launch")
                .app_name("Headroom")
                .build(),
        )
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_positioner::init())
        .manage(DesktopEngine::default())
        .manage(DesktopStore::default())
        .manage(DesktopUiState::default())
        .manage(CollectionScheduler::default())
        .manage(StartupRouting {
            login_launch,
            decision_complete: AtomicBool::new(false),
        })
        .invoke_handler(tauri::generate_handler![
            desktop_discover,
            desktop_onboarding,
            desktop_account_action,
            desktop_adopt,
            desktop_refresh,
            desktop_start_claude_login,
            desktop_start_codex_login,
            desktop_start_reauthentication,
            desktop_login_status,
            desktop_cancel_login,
            desktop_open_device_url,
            desktop_snapshot,
            desktop_retry_engine,
            desktop_set_theme,
            desktop_update_settings,
            desktop_launch_at_login_status,
            desktop_set_launch_at_login,
            desktop_show_dashboard,
            desktop_show_settings,
            desktop_hide_dashboard,
            desktop_quit
        ])
        .setup(|app| {
            #[cfg(unix)]
            {
                let listener = app
                    .state::<SingletonState>()
                    .listener
                    .lock()
                    .map_err(|_| std::io::Error::other("singleton listener is unavailable"))?
                    .take()
                    .ok_or_else(|| std::io::Error::other("singleton listener already started"))?;
                start_singleton_listener(app.handle(), listener).map_err(std::io::Error::other)?;
            }
            // Build a safe recovery shell immediately. The frozen handshake
            // runs on the blocking pool and later replaces this projection;
            // startup lifecycle work never stalls the main UI thread.
            let handshake = desktop_startup_handshake();
            let snapshot = app
                .state::<DesktopStore>()
                .replace_view(desktop_startup_view())
                .map_err(std::io::Error::other)?;
            let login_launch = app.state::<StartupRouting>().login_launch;
            let main = build_desktop_window(app.handle(), &handshake, &snapshot, !login_launch)?;
            restore_window_placement(&main);
            let popover = build_desktop_popover(app.handle(), &handshake, &snapshot)?;
            #[cfg(target_os = "macos")]
            round_window_corners(&popover, PANEL_RADIUS);
            build_desktop_tray(app.handle())?;
            update_desktop_tray_icon(app.handle(), &snapshot.view);
            start_collection_scheduler(app.handle(), &snapshot);
            start_engine_watchdog(app.handle());
            start_desktop_engine_async(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == DESKTOP_WINDOW_LABEL {
                let ui = window.app_handle().state::<DesktopUiState>();
                match event {
                    WindowEvent::Moved(position) => {
                        if let Ok(mut placement) = ui.placement.lock() {
                            let current = placement.get_or_insert(WindowPlacement {
                                x: position.x,
                                y: position.y,
                                width: 900,
                                height: 650,
                            });
                            current.x = position.x;
                            current.y = position.y;
                        }
                    }
                    WindowEvent::Resized(size) => {
                        if let Ok(mut placement) = ui.placement.lock() {
                            let current = placement.get_or_insert(WindowPlacement {
                                x: 0,
                                y: 0,
                                width: size.width,
                                height: size.height,
                            });
                            current.width = size.width;
                            current.height = size.height;
                        }
                    }
                    WindowEvent::CloseRequested { api, .. } => {
                        api.prevent_close();
                        let _ = save_window_placement(window.app_handle());
                        let _ = window.hide();
                    }
                    _ => {}
                }
            } else if window.label() == DESKTOP_POPOVER_LABEL
                && matches!(event, WindowEvent::Focused(false))
            {
                let just_opened = window
                    .app_handle()
                    .state::<DesktopUiState>()
                    .popover_opened_at
                    .lock()
                    .ok()
                    .and_then(|opened| *opened)
                    .is_some_and(|opened| opened.elapsed() < REOPEN_SUPPRESS);
                if !just_opened {
                    let _ = window.hide();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Headroom desktop app");

    app.run(|handle, event| {
        if matches!(&event, tauri::RunEvent::Resumed) {
            request_scheduled_collection(handle, "wake");
        }
        #[cfg(target_os = "macos")]
        if matches!(&event, tauri::RunEvent::Reopen { .. }) {
            let _ = show_desktop_window(handle, None);
            request_scheduled_collection(handle, "activation");
        }
        if matches!(&event, tauri::RunEvent::Exit) {
            let _ = save_window_placement(handle);
            stop_desktop_engine(handle);
            #[cfg(unix)]
            handle.state::<SingletonState>().cleanup_endpoint();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).expect("test URL parses")
    }

    #[cfg(unix)]
    #[test]
    fn owned_singleton_endpoint_cleanup_is_idempotent() {
        let path = std::env::temp_dir().join(format!(
            "headroom-singleton-test-{}-{}.sock",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock is after epoch")
                .as_nanos()
        ));
        let _listener = UnixListener::bind(&path).expect("test socket binds");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .expect("test socket permissions update");

        remove_owned_stale_socket(&path).expect("owned test socket is removed");
        remove_owned_stale_socket(&path).expect("missing test socket is already clean");
        assert!(!path.exists());
    }

    #[test]
    fn numeric_loopback_hosts_are_accepted() {
        assert!(is_loopback_host(&url("http://127.0.0.1:8377/widget")));
        assert!(is_loopback_host(&url("http://127.0.0.53:8377/widget")));
        assert!(is_loopback_host(&url("http://[::1]:8377/widget")));
    }

    #[test]
    fn hostnames_are_rejected_even_localhost() {
        // `localhost` goes through the resolver — a poisoned hosts file or
        // DNS entry could answer with a public IP, so the name is never
        // trusted at probe/navigation time (it is pinned at startup instead)
        assert!(!is_loopback_host(&url("http://localhost:8377/widget")));
        assert!(!is_loopback_host(&url("http://192.168.1.10:8377/widget")));
        assert!(!is_loopback_host(&url("http://example.com/widget")));
        assert!(!is_loopback_host(&url("http://10.0.0.1/")));
        assert!(!is_loopback_host(&url("http://[fe80::1]:8377/")));
    }

    #[test]
    fn localhost_is_pinned_to_numeric_loopback() {
        assert!(is_acceptable_widget_host(&url(
            "http://localhost:8377/widget"
        )));
        assert!(is_acceptable_widget_host(&url(
            "http://127.0.0.1:8377/widget"
        )));
        assert!(!is_acceptable_widget_host(&url("http://example.com:8377/")));
        assert_eq!(
            canonicalize_loopback(url("http://localhost:8377/widget")).as_str(),
            "http://127.0.0.1:8377/widget"
        );
        assert_eq!(
            canonicalize_loopback(url("http://LOCALHOST:8377/widget")).as_str(),
            "http://127.0.0.1:8377/widget"
        );
        // already-numeric URLs pass through untouched
        assert_eq!(
            canonicalize_loopback(url("http://127.0.0.53:9000/x")).as_str(),
            "http://127.0.0.53:9000/x"
        );
    }

    #[test]
    fn navigation_policy_is_exact_widget_document_or_bundled_only() {
        let widget = url("http://127.0.0.1:8377/widget");
        assert!(navigation_allowed(&widget, &widget));
        assert!(navigation_allowed(&widget, &fallback_page_url()));
        // the INITIAL navigation: tauri simplifies WebviewUrl::App to the
        // asset-origin base — both base spellings must pass or the popover
        // never loads its own fallback document at startup
        if cfg!(windows) {
            assert!(navigation_allowed(&widget, &url("http://tauri.localhost")));
            assert!(navigation_allowed(&widget, &url("http://tauri.localhost/")));
        } else {
            assert!(navigation_allowed(&widget, &url("tauri://localhost")));
            assert!(navigation_allowed(&widget, &url("tauri://localhost/")));
        }
        assert!(navigation_allowed(&widget, &url("about:blank")));
        // the bundled exception is the EXACT platform fallback document,
        // never its origin: other assets, ports, or the other platform's
        // asset-protocol form are all refused
        assert!(!navigation_allowed(
            &widget,
            &url("tauri://localhost/main.js")
        ));
        assert!(!navigation_allowed(
            &widget,
            &url("http://tauri.localhost:9000/not-index?x=1")
        ));
        if cfg!(windows) {
            assert!(!navigation_allowed(
                &widget,
                &url("tauri://localhost/index.html")
            ));
        } else {
            assert!(!navigation_allowed(
                &widget,
                &url("http://tauri.localhost/index.html")
            ));
        }
        // NOT the configured document: other paths, ports, queries, origins
        assert!(!navigation_allowed(&widget, &url("http://127.0.0.1:8377/")));
        assert!(!navigation_allowed(
            &widget,
            &url("http://127.0.0.1:8377/widget?size=small")
        ));
        assert!(!navigation_allowed(
            &widget,
            &url("http://127.0.0.1:9999/widget")
        ));
        assert!(!navigation_allowed(
            &widget,
            &url("http://127.0.0.53:8377/widget")
        ));
        assert!(!navigation_allowed(
            &widget,
            &url("http://localhost:8377/widget")
        ));
        // only the blank initial document, not other about: URLs
        assert!(!navigation_allowed(&widget, &url("about:srcdoc")));
        assert!(!navigation_allowed(&widget, &url("https://example.com/")));
        assert!(!navigation_allowed(&widget, &url("http://example.com/")));
        assert!(!navigation_allowed(
            &widget,
            &url("https://127.0.0.1:8377/widget")
        ));
        assert!(!navigation_allowed(&widget, &url("file:///etc/passwd")));
        assert!(!navigation_allowed(
            &widget,
            &url("http://127.0.0.1.evil.com/widget")
        ));
        // a widget URL carrying a query allows exactly that document
        let sized = url("http://127.0.0.1:8377/widget?size=small");
        assert!(navigation_allowed(&sized, &sized));
        assert!(!navigation_allowed(
            &sized,
            &url("http://127.0.0.1:8377/widget")
        ));
    }

    #[test]
    fn js_string_literal_escapes() {
        assert_eq!(js_string_literal("plain"), "\"plain\"");
        assert_eq!(
            js_string_literal("a\"b\\c\n<script>"),
            "\"a\\\"b\\\\c\\n\\u003cscript>\""
        );
    }

    #[test]
    fn desktop_bridge_response_requires_schema_id_and_success() {
        let valid = serde_json::json!({
            "schema": DESKTOP_BRIDGE_SCHEMA,
            "id": "startup-handshake",
            "ok": true,
            "result": {"product": "headroom"}
        });
        assert_eq!(
            parse_bridge_response(
                serde_json::to_string(&valid).unwrap().as_bytes(),
                "startup-handshake"
            )
            .unwrap()["product"],
            "headroom"
        );
        assert!(parse_bridge_response(b"not json", "startup-handshake").is_err());
        assert!(parse_bridge_response(
            br#"{"schema":"headroom_desktop_bridge@1","id":"wrong","ok":true,"result":{}}"#,
            "startup-handshake"
        )
        .is_err());
    }

    #[test]
    fn desktop_bootstrap_requires_onboarding_and_lifecycle_capabilities() {
        let view = serde_json::json!({
            "schema": DESKTOP_VIEW_SCHEMA,
            "accounts": [],
        });
        let capabilities = [
            "discover",
            "adopt",
            "refresh",
            "claude_login",
            "codex_device_login",
            "onboarding",
            "account_lifecycle",
            "reauthentication",
            "resilient_collection",
            "validated_settings",
        ];
        let compatible = serde_json::json!({
            "product": "headroom",
            "bridge_schema": DESKTOP_BRIDGE_SCHEMA,
            "runtime": "frozen",
            "capabilities": capabilities,
        });
        assert!(validate_desktop_bootstrap(&compatible, &view).is_ok());
        let incompatible = serde_json::json!({
            "product": "headroom",
            "bridge_schema": DESKTOP_BRIDGE_SCHEMA,
            "runtime": "frozen",
            "capabilities": capabilities[..9],
        });
        assert!(validate_desktop_bootstrap(&incompatible, &view).is_err());
    }

    #[test]
    fn desktop_bootstrap_script_uses_a_json_string_literal() {
        let handshake = serde_json::json!({
            "product": "headroom",
            "product_version": "0.4.0</script><script>bad()</script>",
            "bridge_schema": DESKTOP_BRIDGE_SCHEMA
        });
        let view = serde_json::json!({"schema": DESKTOP_VIEW_SCHEMA, "accounts": []});
        let snapshot = DesktopSnapshot {
            revision: 7,
            theme: "terminal".into(),
            view,
        };
        let script = desktop_initialization_script(&handshake, &snapshot, "popover");
        assert!(script.contains("Object.defineProperty"));
        assert!(script.contains("writable:false"));
        assert!(!script.contains("JSON.parse({\"bridge\""));
    }

    #[test]
    fn desktop_store_revisions_one_ordered_snapshot_for_both_surfaces() {
        let store = DesktopStore::default();
        let first = store
            .replace_view(serde_json::json!({
                "schema": DESKTOP_VIEW_SCHEMA,
                "settings": {"theme": "midnight"},
                "accounts": [{"name": "codex"}, {"name": "claude"}],
            }))
            .unwrap();
        assert_eq!(first.revision, 1);
        assert_eq!(first.theme, "midnight");
        assert_eq!(first.view["accounts"][0]["name"], "codex");
        let themed = store.set_theme("terminal").unwrap();
        assert_eq!(themed.revision, 2);
        assert_eq!(themed.theme, "terminal");
        assert_eq!(themed.view, first.view);
        let persisted = store
            .replace_view(serde_json::json!({
                "schema": DESKTOP_VIEW_SCHEMA,
                "settings": {"theme": "paper"},
                "accounts": [],
            }))
            .unwrap();
        assert_eq!(persisted.revision, 3);
        assert_eq!(persisted.theme, "paper");
        assert!(store.set_theme("remote-css").is_err());
    }

    #[test]
    fn collection_policy_is_bounded_jittered_and_single_schedule_aware() {
        let started = Instant::now();
        let mut policy = CollectionPolicy::default();
        let interval = Duration::from_secs(120);
        let first = policy.record_failure(started, 0);
        assert!(first >= Duration::from_secs(4));
        assert!(first <= Duration::from_secs(6));
        assert!(!policy.due(started, interval));
        assert!(policy.due(started + first, interval));
        for entropy in 1..20 {
            let delay = policy.record_failure(started, entropy);
            assert!(delay <= COLLECTION_RETRY_CAP);
        }
        policy.record_success(started);
        assert!(!policy.due(started + interval - Duration::from_secs(1), interval));
        assert!(policy.due(started + interval, interval));
    }

    #[test]
    fn configured_collection_interval_is_bounded() {
        assert_eq!(
            configured_refresh_interval(&serde_json::json!({
                "settings": {"refresh_interval_seconds": 420}
            })),
            Duration::from_secs(420)
        );
        assert_eq!(
            configured_refresh_interval(&serde_json::json!({
                "settings": {"refresh_interval_seconds": 5}
            })),
            Duration::from_secs(COLLECTION_INTERVAL_MIN_SECONDS)
        );
        assert_eq!(
            configured_refresh_interval(&serde_json::json!({
                "settings": {"refresh_interval_seconds": 99_999}
            })),
            Duration::from_secs(COLLECTION_INTERVAL_MAX_SECONDS)
        );
    }

    #[test]
    fn login_launch_stays_hidden_except_for_required_operator_action() {
        assert!(!login_launch_requires_window(
            &serde_json::json!({"mode": "ready"})
        ));
        assert!(!login_launch_requires_window(
            &serde_json::json!({"mode": "demo"})
        ));
        assert!(login_launch_requires_window(
            &serde_json::json!({"mode": "onboarding"})
        ));
        assert!(login_launch_requires_window(
            &serde_json::json!({"mode": "recovery"})
        ));
    }

    #[test]
    fn repeated_engine_failures_enter_and_leave_bounded_degraded_state() {
        let started = Instant::now();
        let mut policy = EngineRecoveryPolicy::default();
        let first = policy.record_failure(started, 0, "engine_startup_exited");
        assert!(first >= Duration::from_millis(1600));
        assert!(first <= Duration::from_millis(2400));
        assert!(!policy.degraded);
        assert!(!policy.admits_restart(started));
        policy.record_failure(started, 1, "engine_startup_timeout");
        policy.record_failure(started, 2, "engine_unexpected_exit");
        assert!(policy.degraded);
        assert_eq!(policy.last_failure_code, Some("engine_unexpected_exit"));
        assert!(!policy.admits_restart(started));
        assert!(policy.admits_restart(started + ENGINE_DEGRADED_COOLDOWN));
        assert!(policy.allow_manual_retry());
        policy.record_started(started + ENGINE_DEGRADED_COOLDOWN);
        policy.record_success(started + ENGINE_DEGRADED_COOLDOWN);
        assert!(!policy.degraded);
        assert!(policy.admits_restart(started));
        assert_eq!(policy.crash_times.len(), 3);
        policy.record_success(started + ENGINE_DEGRADED_COOLDOWN + ENGINE_STABLE_RESET);
        assert!(policy.crash_times.is_empty());
        assert!(policy.last_failure_code.is_none());
    }

    #[test]
    fn engine_failures_and_startup_shell_use_stable_fail_closed_contracts() {
        assert_eq!(
            engine_failure_code("bundled desktop engine startup timed out"),
            "engine_startup_timeout"
        );
        assert_eq!(
            engine_failure_code("bundled desktop engine stopped during startup"),
            "engine_startup_exited"
        );
        assert_eq!(
            engine_failure_code("desktop engine stopped unexpectedly"),
            "engine_exited_mid_request"
        );
        assert_eq!(
            engine_failure_code("desktop engine request timed out"),
            "engine_request_timeout"
        );
        let view = desktop_startup_view();
        assert!(validate_desktop_view(&view).is_ok());
        assert_eq!(view["mode"], "recovery");
        assert_eq!(view["recovery_code"], "engine_starting");
        assert!(view["accounts"].as_array().unwrap().is_empty());
        assert_eq!(desktop_startup_handshake()["runtime"], "unavailable");
    }

    #[test]
    fn collection_outcome_distinguishes_capacity_offline_and_auth_holds() {
        assert!(!collection_enabled(
            &serde_json::json!({"mode": "onboarding", "accounts": []})
        ));
        assert!(collection_enabled(&serde_json::json!({
            "mode": "ready", "accounts": [{"name": "a"}]
        })));
        let current = serde_json::json!({"accounts": [{"state": "current"}]});
        assert_eq!(collection_view_outcome(&current), (true, false));
        let offline = serde_json::json!({"accounts": [{
            "state": "stale", "diagnostic_code": "provider_offline"
        }]});
        assert_eq!(collection_view_outcome(&offline), (false, true));
        let partial = serde_json::json!({"accounts": [
            {"state": "current"},
            {"state": "stale", "diagnostic_code": "provider_timeout"}
        ]});
        assert_eq!(collection_view_outcome(&partial), (false, false));
        let malformed = serde_json::json!({"accounts": [{
            "state": "held", "diagnostic_code": "malformed_provider_response"
        }]});
        assert_eq!(collection_view_outcome(&malformed), (false, false));
        let auth = serde_json::json!({"accounts": [{
            "state": "held", "diagnostic_code": "provider_auth_rejected"
        }]});
        assert_eq!(collection_view_outcome(&auth), (true, false));
    }

    #[test]
    fn window_placement_validation_refuses_unsafe_geometry() {
        let valid = serde_json::json!({
            "schema": WINDOW_STATE_SCHEMA,
            "x": -1200,
            "y": 40,
            "width": 1800,
            "height": 1300,
        });
        assert_eq!(
            valid_window_placement(&valid),
            Some(WindowPlacement {
                x: -1200,
                y: 40,
                width: 1800,
                height: 1300,
            })
        );
        assert!(valid_window_placement(&serde_json::json!({
            "schema": WINDOW_STATE_SCHEMA,
            "x": 0,
            "y": 0,
            "width": 120,
            "height": 80,
        }))
        .is_none());
        assert!(valid_window_placement(&serde_json::json!({
            "schema": "other",
            "x": 0,
            "y": 0,
            "width": 900,
            "height": 650,
        }))
        .is_none());
    }

    #[test]
    fn device_authorization_url_is_exactly_allowlisted() {
        assert!(verified_device_url("https://auth.openai.com/codex/device").is_some());
        assert!(verified_device_url("http://auth.openai.com/codex/device").is_none());
        assert!(verified_device_url("https://auth.openai.com.evil.test/codex/device").is_none());
        assert!(verified_device_url("https://user@auth.openai.com/codex/device").is_none());
        assert!(verified_device_url("https://auth.openai.com/other").is_none());
        assert!(verified_device_url("https://auth.openai.com/codex/device?next=evil").is_none());
    }
}
