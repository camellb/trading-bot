// Prevents the additional console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Delfi - Tauri shell.
//!
//! Responsibilities of this binary, in order:
//!
//! 1. Pick a free TCP port on 127.0.0.1.
//! 2. Resolve the platform-specific app-data dir for the SQLite DB.
//! 3. Spawn the Python sidecar (`delfi-sidecar` in production, `python
//!    main.py` in dev with the venv interpreter from `DELFI_DEV_PYTHON`)
//!    with env vars `DELFI_PORT` and `DELFI_DB_PATH` set, plus
//!    `PYTHONUNBUFFERED=1` so we can read stdout line-by-line.
//! 4. Read stdout looking for `DELFI_LOCAL_API_READY <port>`.
//! 5. Store the port in `tauri::State<ApiState>` so the `get_api_port`
//!    IPC command can hand it to the React UI.
//! 6. On app exit, kill the sidecar.
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
    /// The sidecar child handle. Held for the lifetime of the app so
    /// we can `kill()` it on quit. `Mutex<Option<...>>` because
    /// `CommandChild::kill` consumes self.
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

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(ApiState {
            port: Mutex::new(None),
            child: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![get_api_port])
        .setup(|app| {
            // Pick a port and resolve the DB path before spawning.
            let port = portpicker::pick_unused_port().unwrap_or(0);
            let db_path = resolve_db_path(&app.handle());

            let (ready_tx, ready_rx) = oneshot::channel::<u16>();

            let child = spawn_sidecar(&app.handle(), port, &db_path, ready_tx)
                .map_err(|e| -> Box<dyn std::error::Error> { e.into() })?;

            // Stash the child so the `RunEvent::Exit` handler can kill
            // it. (We can't call kill() on Drop because Tauri doesn't
            // run our state's Drop in the right order.)
            {
                let state = app.state::<ApiState>();
                *state.child.lock().unwrap() = Some(child);
            }

            // Wait for the ready handshake on a background task, with a
            // 30s timeout. Once we have the bound port, write it into
            // ApiState so JS can retrieve it.
            let app_handle = app.handle().clone();
            async_runtime::spawn(async move {
                let timeout = tokio::time::sleep(std::time::Duration::from_secs(30));
                tokio::select! {
                    res = ready_rx => {
                        match res {
                            Ok(bound_port) => {
                                let state = app_handle.state::<ApiState>();
                                *state.port.lock().unwrap() = Some(bound_port);
                                println!("[delfi] sidecar ready on 127.0.0.1:{bound_port}");
                            }
                            Err(_) => {
                                eprintln!("[delfi] sidecar ready channel dropped before READY line");
                            }
                        }
                    }
                    _ = timeout => {
                        eprintln!("[delfi] sidecar did not become ready within 30s");
                    }
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Kill the sidecar on app exit (Cmd-Q, window close on the
            // last window, etc.). Without this the Python process is
            // reparented to launchd / systemd.
            if let RunEvent::Exit = event {
                let state = app_handle.state::<ApiState>();
                // Bind the lock guard to a local so it drops before
                // `state` does (otherwise the borrow checker sees the
                // guard's destructor running after `state` is gone).
                let mut child_slot = state.child.lock().unwrap();
                if let Some(child) = child_slot.take() {
                    let _ = child.kill();
                }
            }
        });
}
