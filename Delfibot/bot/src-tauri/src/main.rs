// Prevents the additional console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Delfi - Tauri shell.
//!
//! The Tauri shell is a viewer. The bot is a separate long-running
//! daemon (`delfi-sidecar`) supervised by launchd via a user
//! LaunchAgent at ~/Library/LaunchAgents/com.delfi.bot.plist. The
//! user explicitly requires the bot to run 24/7 - closing this
//! window must NOT stop trading.
//!
//! Responsibilities of this binary, in order:
//!
//! 1. Resolve the platform-specific app-data dir for the SQLite DB
//!    and the daemon's port file at `<app-data>/sidecar.port`.
//! 2. Try to attach to a running daemon: read the port file, TCP-
//!    probe `127.0.0.1:<port>`. If reachable, store that port for
//!    the front-end and we're done.
//! 3. Fallback (dev mode, or first launch before install.sh has
//!    bootstrapped the LaunchAgent): spawn `delfi-sidecar` (or
//!    `python main.py` in dev with `DELFI_DEV_PYTHON`) directly,
//!    read its stdout for `DELFI_LOCAL_API_READY <port>`.
//! 4. Expose the port via `get_api_port` to the React UI.
//! 5. On app exit, in DEV kill the spawned child (avoid orphaning
//!    `python main.py` between dev iterations); in RELEASE leave it
//!    alone - launchd is the lifecycle owner.
//!
//! There is no auth between Tauri and the sidecar. We bind to 127.0.0.1
//! and trust everything on the loopback interface, on the assumption
//! that any process running as the user could already read the SQLite
//! DB and OS keychain entries directly.

use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;
use tauri::async_runtime;
use tauri::path::BaseDirectory;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// Shared runtime state for the API connection. Exposed to the JS layer
/// through the `get_api_port` command.
struct ApiState {
    port: Mutex<Option<u16>>,
    /// Sidecar child handle. Only populated when the GUI took the
    /// fallback spawn path (no launchd-owned daemon was running on
    /// startup). In dev we use it to kill the child on app exit; in
    /// release we drop it without killing. `Mutex<Option<...>>`
    /// because `CommandChild::kill` consumes self.
    child: Mutex<Option<CommandChild>>,
}

#[derive(Debug, Serialize)]
struct ApiPort {
    port: u16,
    ready: bool,
}

#[tauri::command]
fn get_api_port(state: tauri::State<ApiState>) -> ApiPort {
    let guard = state.port.lock().unwrap();
    match *guard {
        Some(p) => ApiPort { port: p, ready: true },
        None => ApiPort { port: 0, ready: false },
    }
}

fn resolve_db_path(app: &tauri::AppHandle) -> PathBuf {
    // Tauri exposes per-platform app-data dirs. We use AppData (which
    // resolves to ~/Library/Application Support/<bundleId> on macOS,
    // %APPDATA%/<bundleId> on Windows, ~/.local/share/<bundleId> on
    // Linux) and put the SQLite file inside.
    let dir = app
        .path()
        .resolve("", BaseDirectory::AppData)
        .unwrap_or_else(|_| PathBuf::from("."));
    if !dir.exists() {
        let _ = std::fs::create_dir_all(&dir);
    }
    dir.join("delfi.db")
}

fn spawn_sidecar(
    app: &tauri::AppHandle,
    port: u16,
    db_path: &PathBuf,
    ready_tx: oneshot::Sender<u16>,
) -> Result<CommandChild, String> {
    let shell = app.shell();

    // In release builds we spawn the bundled `delfi-sidecar` binary
    // (declared as an externalBin in tauri.conf.json). In dev there is
    // no bundled binary yet, so we spawn the venv Python directly. The
    // user is expected to set `DELFI_DEV_PYTHON` to an interpreter that
    // has the project deps installed (e.g. .venv/bin/python).
    let cmd = if cfg!(debug_assertions) {
        let python = std::env::var("DELFI_DEV_PYTHON")
            .unwrap_or_else(|_| "python3".to_string());
        let bot_dir = std::env::var("DELFI_BOT_DIR").unwrap_or_else(|_| {
            // Default: walk up from `src-tauri` to `Delfibot/bot`.
            let here = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
            here.parent()
                .map(|p| p.to_path_buf())
                .unwrap_or(here)
                .to_string_lossy()
                .into_owned()
        });
        shell
            .command(python)
            .args(["main.py"])
            .current_dir(bot_dir)
    } else {
        match shell.sidecar("delfi-sidecar") {
            Ok(c) => c,
            Err(e) => return Err(format!("failed to locate sidecar: {e}")),
        }
    };

    let cmd = cmd
        .env("DELFI_PORT", port.to_string())
        .env("DELFI_DB_PATH", db_path.to_string_lossy().into_owned())
        .env("PYTHONUNBUFFERED", "1");
    // DELFI_PARENT_PID was used to drive a parent-death watchdog
    // that killed the sidecar when the GUI quit. Removed 2026-04-30:
    // the sidecar is now a 24/7 launchd-managed daemon that must
    // survive the GUI closing.

    let (mut rx, child) = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {e}"))?;

    // Drain the sidecar's stdout/stderr in a background task. We watch
    // for the ready line and forward everything else to the host
    // process's stdout/stderr so it shows up in `cargo tauri dev`
    // console output.
    async_runtime::spawn(async move {
        let mut ready_tx = Some(ready_tx);
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).into_owned();
                    let trimmed = line.trim_end();
                    println!("[sidecar] {trimmed}");
                    if let Some(rest) = trimmed.strip_prefix("DELFI_LOCAL_API_READY ") {
                        if let Ok(p) = rest.trim().parse::<u16>() {
                            if let Some(tx) = ready_tx.take() {
                                let _ = tx.send(p);
                            }
                        }
                    }
                }
                CommandEvent::Stderr(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).into_owned();
                    eprintln!("[sidecar] {}", line.trim_end());
                }
                CommandEvent::Error(err) => {
                    eprintln!("[sidecar] error: {err}");
                }
                CommandEvent::Terminated(payload) => {
                    eprintln!(
                        "[sidecar] terminated (code={:?}, signal={:?})",
                        payload.code, payload.signal
                    );
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(child)
}

/// Try to find a sidecar that's already running (managed by launchd
/// via the LaunchAgent installed at first install). Reads the port
/// file the daemon writes after binding, then verifies via a TCP
/// probe that the port is actually listening. Returns the port on
/// success, None if no daemon is running.
///
/// Why this exists: with the launchd LaunchAgent owning the sidecar
/// lifecycle, the GUI must NOT spawn its own sidecar - that would
/// create a duplicate that exits at the singleton lock check. The
/// GUI is now a viewer that connects to whatever daemon is already
/// running.
async fn read_existing_sidecar_port(app: &tauri::AppHandle) -> Option<u16> {
    let dir = app.path().resolve("", BaseDirectory::AppData).ok()?;
    let port_file = dir.join("sidecar.port");
    let contents = std::fs::read_to_string(&port_file).ok()?;
    let port: u16 = contents.trim().parse().ok()?;
    if port == 0 {
        return None;
    }
    // TCP probe with a short timeout. A successful connect means
    // the daemon is listening; we don't speak the API protocol here.
    let addr = format!("127.0.0.1:{port}");
    match tokio::time::timeout(
        std::time::Duration::from_millis(1500),
        tokio::net::TcpStream::connect(&addr),
    )
    .await
    {
        Ok(Ok(_stream)) => Some(port),
        _ => None,
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        // Updater. The front-end calls `check()` from JS on app
        // start; this plugin handles the manifest fetch, Ed25519
        // signature verification, download, and in-place install.
        // Config (GitHub Releases manifest URL + public verifying
        // key) lives in tauri.conf.json under plugins.updater.
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(ApiState {
            port: Mutex::new(None),
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![get_api_port])
        .setup(|app| {
            // Two paths to a running sidecar:
            //
            //   1. Daemon already running (production: launchd
            //      LaunchAgent installed by Delfibot/install.sh has
            //      RunAtLoad=true + KeepAlive=true). The daemon
            //      wrote its port to <app-data>/sidecar.port. The
            //      GUI just connects.
            //
            //   2. No daemon yet (dev mode, fresh install before the
            //      LaunchAgent is registered, or someone manually
            //      stopped it). Spawn a sidecar from here as a
            //      fallback. It still trades, just without auto-
            //      restart-on-crash. Next launch with the LaunchAgent
            //      registered uses path 1.
            let app_handle = app.handle().clone();
            async_runtime::spawn(async move {
                // Path 1: probe for an already-running daemon for up
                // to 30s. PyInstaller cold-start of the bundled
                // 160MB sidecar takes ~8-15s on first run (tempdir
                // decompress + Python interpreter init). 6s wasn't
                // enough and we'd fall through to the spawn fallback
                // even when the launchd daemon was 2 seconds away
                // from binding.
                for _ in 0..60 {
                    if let Some(p) = read_existing_sidecar_port(&app_handle).await {
                        let state = app_handle.state::<ApiState>();
                        *state.port.lock().unwrap() = Some(p);
                        println!(
                            "[delfi] connected to running daemon on \
                             127.0.0.1:{p}"
                        );
                        return;
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                }

                // Path 2: spawn a sidecar ourselves.
                //
                // Note: in production this fallback usually exits
                // immediately at the singleton lock check inside
                // main.py, because the launchd daemon got there
                // first. The spawned process gets to "lock held by
                // pid=X, exiting cleanly" and never emits a READY
                // line. We handle that by ALSO polling the port
                // file in parallel - the spawned sidecar's exit is
                // not a failure if the daemon is already up.
                println!(
                    "[delfi] no daemon found via port file - falling \
                     back to spawning a sidecar from the GUI"
                );
                let port = portpicker::pick_unused_port().unwrap_or(0);
                let db_path = resolve_db_path(&app_handle);
                let (ready_tx, ready_rx) = oneshot::channel::<u16>();

                let child = match spawn_sidecar(
                    &app_handle, port, &db_path, ready_tx,
                ) {
                    Ok(c) => c,
                    Err(e) => {
                        eprintln!("[delfi] sidecar spawn failed: {e}");
                        return;
                    }
                };
                {
                    let state = app_handle.state::<ApiState>();
                    *state.child.lock().unwrap() = Some(child);
                }

                // Wait for either:
                //   (a) READY line on stdout from our spawned child
                //       - means we are the bot (no daemon was up)
                //   (b) port file appearing - means the launchd
                //       daemon finished booting after our Path 1
                //       probe gave up; we connect to it instead and
                //       our spawned child has already (or will
                //       shortly) exit at the singleton lock
                // 120s overall timeout.
                let app_handle_for_poll = app_handle.clone();
                let port_file_watcher = async move {
                    loop {
                        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                        if let Some(p) = read_existing_sidecar_port(&app_handle_for_poll).await {
                            return p;
                        }
                    }
                };
                let timeout = tokio::time::sleep(std::time::Duration::from_secs(120));
                tokio::select! {
                    res = ready_rx => {
                        match res {
                            Ok(bound_port) => {
                                let state = app_handle.state::<ApiState>();
                                *state.port.lock().unwrap() = Some(bound_port);
                                println!("[delfi] spawned sidecar ready on 127.0.0.1:{bound_port}");
                            }
                            Err(_) => {
                                eprintln!("[delfi] sidecar ready channel dropped before READY line");
                            }
                        }
                    }
                    bound_port = port_file_watcher => {
                        let state = app_handle.state::<ApiState>();
                        *state.port.lock().unwrap() = Some(bound_port);
                        println!(
                            "[delfi] daemon came online after probe window - \
                             connected on 127.0.0.1:{bound_port} (our spawn \
                             will exit at the singleton lock)"
                        );
                    }
                    _ = timeout => {
                        eprintln!("[delfi] sidecar did not become ready within 120s");
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // GUI quit handler.
            //
            // In release builds we DO NOT kill the sidecar. The
            // sidecar is the bot - it runs 24/7, owned by launchd via
            // the LaunchAgent at ~/Library/LaunchAgents/
            // com.delfi.bot.plist. Closing the GUI window must not
            // stop trading. The user has been explicit about this:
            // "It must be unkillable. If it's killed, it should
            // reopen". launchd's KeepAlive=true handles the reopen;
            // our job is just to not kill it in the first place.
            //
            // In dev (`cargo tauri dev`) we DO kill it, otherwise
            // the spawned `python3 main.py` orphans into the user's
            // session and the next dev run trips on the singleton
            // lock.
            //
            // Note: in release the child handle here is only Some(_)
            // when we took Path 2 in setup() - i.e. spawned a
            // fallback sidecar because no daemon was running. Even
            // then, leaving it alive is correct: it's writing the
            // port file, the next GUI launch will find it, and (once
            // the LaunchAgent has been bootstrapped by install.sh)
            // it will be supervised on subsequent reboots.
            if let RunEvent::Exit = event {
                if cfg!(debug_assertions) {
                    let state = app_handle.state::<ApiState>();
                    // Bind the lock guard to a local so it drops before
                    // `state` does (otherwise the borrow checker sees
                    // the guard's destructor running after `state` is
                    // gone).
                    let mut child_slot = state.child.lock().unwrap();
                    if let Some(child) = child_slot.take() {
                        let _ = child.kill();
                    }
                } else {
                    // Release: drop the handle without killing. This
                    // detaches the child from our process; launchd
                    // takes over as the supervising parent.
                    let state = app_handle.state::<ApiState>();
                    let mut child_slot = state.child.lock().unwrap();
                    let _ = child_slot.take();
                }

                // macOS only: dedupe Delfi entries in the user's Dock
                // recent-apps list. macOS's Dock occasionally inserts
                // a second tile pointing at the same /Applications
                // path during long sessions; this fires once on quit
                // so the duplicate auto-resolves without the user
                // having to remember `bash dock-clean.sh`.
                #[cfg(target_os = "macos")]
                cleanup_dock_on_exit_macos();
            }
        });
}

/// Best-effort Dock dedupe on macOS, runs once at app exit.
///
/// Spawns `python3 -c "<script>"` fire-and-forget; the spawned
/// process inherits no parent-blocking and runs to completion after
/// the Tauri shell has already exited. The script reads the Dock
/// plist via `defaults export`, removes duplicate Delfi entries
/// from recent-apps and persistent-apps, writes the patched plist
/// back, and `pkill`s Dock so it re-reads. No-op (no plist write,
/// no Dock kill) when no duplicates exist, which is the common
/// case.
///
/// Conservative scope: this only collapses duplicate entries whose
/// bundle-identifier is `com.delfi.desktop`. Other apps' tiles pass
/// through untouched, so a user who has intentionally pinned the
/// same app twice (rare but valid) is not surprised.
#[cfg(target_os = "macos")]
fn cleanup_dock_on_exit_macos() {
    const SCRIPT: &str = r#"
import plistlib, subprocess, tempfile, os
try:
    xml = subprocess.check_output(["defaults", "export", "com.apple.dock", "-"])
except Exception:
    raise SystemExit(0)
data = plistlib.loads(xml)
changed = False
for key in ("recent-apps", "persistent-apps"):
    arr = data.get(key)
    if not isinstance(arr, list):
        continue
    seen_delfi = set()
    out = []
    for entry in arr:
        td = entry.get("tile-data", {}) or {}
        bid = td.get("bundle-identifier")
        url = (td.get("file-data", {}) or {}).get("_CFURLString") or ""
        if bid == "com.delfi.desktop":
            k = (bid, url)
            if k in seen_delfi:
                changed = True
                continue
            seen_delfi.add(k)
        out.append(entry)
    data[key] = out
if changed:
    fd, tmp = tempfile.mkstemp(suffix=".plist")
    os.close(fd)
    with open(tmp, "wb") as f:
        plistlib.dump(data, f)
    subprocess.check_call(["defaults", "import", "com.apple.dock", tmp])
    subprocess.run(["pkill", "-KILL", "Dock"], check=False)
"#;
    let _ = std::process::Command::new("python3")
        .arg("-c")
        .arg(SCRIPT)
        .spawn();
}
