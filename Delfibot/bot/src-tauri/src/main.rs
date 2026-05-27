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
use tauri::menu::{Menu, MenuItem};
use tauri::path::BaseDirectory;
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// Set up the macOS LaunchAgent + UI-less sidecar sub-bundle if either
/// is missing. Runs at GUI startup before we wait for the sidecar's
/// port file.
///
/// Why this exists: on macOS release the GUI doesn't spawn the
/// sidecar - it expects `launchd` to be running it via a user
/// LaunchAgent at ~/Library/LaunchAgents/com.delfi.bot.plist that
/// points at /Applications/Delfi.app/Contents/Library/Daemon/
/// DelfiSidecar.app. Neither artifact is in the Tauri build output;
/// historically they were created by `Delfibot/install.sh`. That
/// meant every DMG drag-install AND every auto-updater bundle
/// replacement left the user with no running daemon and the GUI
/// timing out with "Delfi could not start".
///
/// This function is the in-process equivalent of the install.sh
/// LaunchAgent block, so the .app is self-sufficient on first launch
/// regardless of how the bundle got into /Applications.
///
/// Idempotent: returns early when both artifacts already exist and the
/// LaunchAgent path matches the current bundle's sub-bundle path. Best
/// effort - logs and returns on any failure rather than crashing the
/// GUI (the user can re-run the install script manually if this
/// silently can't write to the bundle, e.g. quarantined permissions).
#[cfg(target_os = "macos")]
fn ensure_macos_launchagent() {
    let home = match std::env::var("HOME") {
        Ok(h) if !h.is_empty() => h,
        _ => {
            eprintln!("[bootstrap] HOME unset; can't install LaunchAgent");
            return;
        }
    };

    // Resolve the .app path from the running binary at
    // <app>/Contents/MacOS/delfi.
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("[bootstrap] current_exe failed: {e}");
            return;
        }
    };
    let app_dir = match exe.parent().and_then(|p| p.parent()).and_then(|p| p.parent()) {
        Some(p) => p.to_path_buf(),
        None => {
            eprintln!("[bootstrap] couldn't resolve .app dir from {exe:?}");
            return;
        }
    };

    let sub_bundle_root = app_dir.join("Contents/Library/Daemon/DelfiSidecar.app");
    let sub_bundle_contents = sub_bundle_root.join("Contents");
    let sub_bundle_macos = sub_bundle_contents.join("MacOS");
    let sub_bundle_info = sub_bundle_contents.join("Info.plist");
    let sub_bundle_sidecar = sub_bundle_macos.join("delfi-sidecar");
    let real_sidecar = app_dir.join("Contents/MacOS/delfi-sidecar");
    let agent_dir = format!("{home}/Library/LaunchAgents");
    let agent_path = format!("{agent_dir}/com.delfi.bot.plist");
    let log_dir = format!("{home}/Library/Logs/Delfi");
    let appdata_dir = format!("{home}/Library/Application Support/com.delfi.desktop");

    // Detect what's already in place. The LaunchAgent's `ProgramArguments`
    // must point at THIS bundle's sub-bundle path - if the user
    // reinstalled the .app at a different path, the stale plist
    // would launch the wrong binary. Compare path strings as a
    // cheap freshness check.
    let agent_exists = std::path::Path::new(&agent_path).exists();
    let sub_bundle_exists = sub_bundle_sidecar.exists();
    let agent_matches_current_bundle = if agent_exists {
        std::fs::read_to_string(&agent_path)
            .map(|s| s.contains(&*sub_bundle_sidecar.to_string_lossy()))
            .unwrap_or(false)
    } else {
        false
    };

    if agent_exists && sub_bundle_exists && agent_matches_current_bundle {
        // Already bootstrapped against this bundle path. Nothing to do.
        return;
    }

    eprintln!(
        "[bootstrap] bootstrapping launchd daemon \
         (agent_exists={agent_exists} sub_bundle_exists={sub_bundle_exists} \
          agent_matches={agent_matches_current_bundle})"
    );

    // 1. Create the UI-less sub-bundle so the daemon doesn't paint a
    //    duplicate Dock tile. The wrapper carries CFBundleIdentifier=
    //    com.delfi.sidecar + LSUIElement=true; the binary inside is a
    //    hard link to the real sidecar so we don't double 120MB.
    if let Err(e) = std::fs::create_dir_all(&sub_bundle_macos) {
        eprintln!("[bootstrap] create sub-bundle dir failed: {e}");
        return;
    }
    let sub_bundle_info_plist = r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.delfi.sidecar</string>
    <key>CFBundleName</key>
    <string>Delfi Sidecar</string>
    <key>CFBundleDisplayName</key>
    <string>Delfi Sidecar</string>
    <key>CFBundleExecutable</key>
    <string>delfi-sidecar</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
"#;
    if let Err(e) = std::fs::write(&sub_bundle_info, sub_bundle_info_plist) {
        eprintln!("[bootstrap] write sub-bundle Info.plist failed: {e}");
        return;
    }
    // Hard-link (preferred) or copy (fallback) the sidecar binary
    // into the wrapper. Hard link keeps the disk footprint flat;
    // copy is the fallback if hard linking is somehow rejected on
    // the user's filesystem.
    let _ = std::fs::remove_file(&sub_bundle_sidecar);
    if std::fs::hard_link(&real_sidecar, &sub_bundle_sidecar).is_err() {
        if let Err(e) = std::fs::copy(&real_sidecar, &sub_bundle_sidecar) {
            eprintln!("[bootstrap] copy sidecar into wrapper failed: {e}");
            return;
        }
    }

    // 2. Create LaunchAgent + Logs dirs (idempotent).
    let _ = std::fs::create_dir_all(&agent_dir);
    let _ = std::fs::create_dir_all(&log_dir);

    // 3. Write the LaunchAgent plist. ProgramArguments points at THIS
    //    bundle's sub-bundle path so an auto-update that moves the
    //    bundle gets a fresh plist that follows.
    let sub_bundle_sidecar_str = sub_bundle_sidecar.to_string_lossy();
    let agent_plist = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.delfi.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sub_bundle_sidecar_str}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DELFI_DB_PATH</key>
        <string>{appdata_dir}/delfi.db</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>DELFI_LIVE_KILLSWITCH_OFF</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_dir}/sidecar.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/sidecar.err</string>
    <key>WorkingDirectory</key>
    <string>{home}</string>
</dict>
</plist>
"#
    );
    if let Err(e) = std::fs::write(&agent_path, &agent_plist) {
        eprintln!("[bootstrap] write LaunchAgent plist failed: {e}");
        return;
    }

    // 4. Get UID via id -u (avoids a libc dep just for getuid()).
    let uid_out = std::process::Command::new("/usr/bin/id")
        .arg("-u")
        .output();
    let uid = match uid_out {
        Ok(o) if o.status.success() => {
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }
        _ => {
            eprintln!("[bootstrap] id -u failed");
            return;
        }
    };
    if uid.is_empty() || !uid.chars().all(|c| c.is_ascii_digit()) {
        eprintln!("[bootstrap] UID not numeric: {uid:?}");
        return;
    }
    let user_gui = format!("gui/{uid}");

    // 5. bootout + bootstrap. bootout is best-effort (no-op if the
    //    agent was never registered). bootstrap is the real
    //    registration. kickstart -k starts the daemon immediately
    //    without waiting for ThrottleInterval.
    let _ = std::process::Command::new("/bin/launchctl")
        .args(["bootout", &user_gui, &agent_path])
        .output();
    match std::process::Command::new("/bin/launchctl")
        .args(["bootstrap", &user_gui, &agent_path])
        .output()
    {
        Ok(o) if o.status.success() => {
            eprintln!("[bootstrap] launchctl bootstrap OK");
        }
        Ok(o) => {
            eprintln!(
                "[bootstrap] launchctl bootstrap rc={:?} stderr={}",
                o.status.code(),
                String::from_utf8_lossy(&o.stderr)
            );
        }
        Err(e) => {
            eprintln!("[bootstrap] launchctl bootstrap failed: {e}");
        }
    }
    let _ = std::process::Command::new("/bin/launchctl")
        .args(["kickstart", "-k", &format!("{user_gui}/com.delfi.bot")])
        .output();
}

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
    /// True once the user has asked the app to quit. The non-macOS
    /// respawn loop checks this before kicking off a new sidecar so
    /// we don't race a "respawn" against the imminent process exit
    /// and leave an orphan Python process behind.
    shutting_down: std::sync::atomic::AtomicBool,
}

#[derive(Debug, Serialize)]
struct ApiPort {
    port: u16,
    ready: bool,
}

#[tauri::command]
fn get_api_port(state: tauri::State<ApiState>) -> ApiPort {
    let guard = state.port.lock().unwrap();
    let result = match *guard {
        Some(p) => ApiPort { port: p, ready: true },
        None => ApiPort { port: 0, ready: false },
    };
    // Sparse log: a single line on every poll would spam, so only log
    // the transition to ready (the React side polls every ~250ms until
    // it succeeds).
    if result.ready {
        static LOGGED_READY: std::sync::Once = std::sync::Once::new();
        LOGGED_READY.call_once(|| {
            dlog(&format!("ipc: get_api_port returning ready port={}", result.port));
        });
    }
    result
}

/// User-initiated restart of the launchd-supervised daemon.
///
/// Why this exists separate from /api/system/restart: when the
/// daemon's HTTP loop is wedged, /api/system/restart is unreachable
/// - the React shell can't even open Settings to find the button.
/// The "Delfi could not start" splash and the inline "/api/X timed
/// out" banner both expose this command instead, so a paying user
/// with no terminal access can recover from a wedge in one click.
///
/// macOS-only. Runs `launchctl kickstart -k gui/<uid>/com.delfi.bot`,
/// which sends SIGTERM to the running daemon (if any) and starts a
/// fresh one. The LaunchAgent's KeepAlive + RunAtLoad take it from
/// there. UID is resolved via `id -u` through the shell plugin so
/// we don't need a libc dep. On non-macOS platforms returns an
/// error string the UI renders; we don't have a Windows / Linux
/// daemon supervisor wired up yet.
#[tauri::command]
async fn restart_sidecar(
    app: tauri::AppHandle,
    state: tauri::State<'_, ApiState>,
) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        // Windows: kill the sidecar; the respawn loop in setup() picks
        // up the Terminated event and spawns a fresh one. The user
        // sees the BootScreen briefly during the ~12s cold-start
        // window, then the dashboard reconnects automatically via
        // `refresh_api_port`.
        let _ = app;
        *state.port.lock().unwrap() = None;
        let result = tokio::task::spawn_blocking(|| {
            std::process::Command::new("taskkill")
                .args(["/F", "/T", "/IM", "delfi-sidecar.exe"])
                .output()
                .map_err(|e| format!("taskkill failed: {e}"))
        })
        .await
        .map_err(|e| format!("restart task panicked: {e}"))?;
        result?;
        return Ok(());
    }
    #[cfg(target_os = "linux")]
    {
        let _ = app;
        let _ = state;
        return Err(
            "Restart from the GUI is not implemented on Linux yet."
                .into(),
        );
    }
    #[allow(unreachable_code)]
    {
    // Read the port file BEFORE we kill anything, so we can target
    // the actual port-holder by lsof. This handles the case where
    // a Tauri-spawned orphan from a prior session is the one
    // wedging the API: a plain `launchctl kickstart -k` only
    // touches the launchd-managed slot and leaves the orphan up,
    // so the GUI keeps timing out forever (incident 2026-05-06).
    let port = read_existing_sidecar_port(&app).await;

    // CRITICAL: clear the cached port. The user-visible bug from
    // 2026-05-20: every Restart succeeded at the OS level (daemon
    // killed + respawned on a fresh random port) but the JS
    // `waitForSidecar` then called `get_api_port` which returned
    // the OLD cached port with ready=true. waitForSidecar returned
    // immediately, the page reloaded, and the reloaded page hit
    // the dead old port and timed out again. The user saw the same
    // "/api/state: timed out" banner that they clicked Restart to
    // get rid of - the button was provably useless.
    //
    // By clearing the cache here, get_api_port returns ready=false
    // until the new daemon's port-file write triggers a
    // refresh_api_port call that re-resolves to the new port. The
    // JS layer falls back to refresh_api_port when ready=false.
    *state.port.lock().unwrap() = None;

    // Every shelled command gets a HARD wall-clock budget. Without
    // it, `launchctl kickstart -k` can wedge for >60s when launchd
    // is in a half-stuck state — the GUI showed "Restarting..." for
    // over a minute with no way to recover (incident 2026-05-18).
    // The helper spawns the child, polls for exit, and returns Err
    // on timeout. The daemon will still come back via launchd's
    // KeepAlive=true — we just don't make the user wait for it.
    fn run_with_timeout(
        program: &str,
        args: &[&str],
        timeout: std::time::Duration,
    ) -> Result<std::process::Output, String> {
        use std::process::{Command, Stdio};
        let mut child = Command::new(program)
            .args(args)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("failed to spawn {program}: {e}"))?;

        let deadline = std::time::Instant::now() + timeout;
        loop {
            match child.try_wait() {
                Ok(Some(_status)) => {
                    return child.wait_with_output()
                        .map_err(|e| format!("wait_with_output: {e}"));
                }
                Ok(None) => {
                    if std::time::Instant::now() >= deadline {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(format!(
                            "{program} exceeded {}s budget",
                            timeout.as_secs(),
                        ));
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                Err(e) => return Err(format!("try_wait: {e}")),
            }
        }
    }

    async_runtime::spawn_blocking(move || -> Result<(), String> {
        // Step 1 (best-effort): SIGKILL whatever owns the sidecar
        // port. Targets the right pid even if launchd is desynced.
        // Skipped if no port file - covered by Step 2's pkill.
        if let Some(p) = port {
            if let Ok(out) = run_with_timeout(
                "/usr/sbin/lsof",
                &["-nP", "-iTCP", "-sTCP:LISTEN", "-t",
                  &format!("-iTCP:{p}")],
                std::time::Duration::from_secs(5),
            ) {
                let pids = String::from_utf8_lossy(&out.stdout);
                for line in pids.lines() {
                    let pid = line.trim();
                    if pid.is_empty() { continue; }
                    let _ = run_with_timeout(
                        "/bin/kill", &["-9", pid],
                        std::time::Duration::from_secs(3),
                    );
                }
            }
        }

        // Step 2: BULLETPROOF KILL. SIGKILL every delfi-sidecar
        // process by name. This is the primitive that makes Restart
        // a 100%-effective recovery: regardless of whether the port
        // file is missing/stale, whether launchd's tracked pid is
        // right, or whether multiple orphans are alive (singleton
        // race after install.sh's bootout/rsync/bootstrap dance),
        // this clears the field. SIGKILL (not SIGTERM) so a hung
        // signal handler can't keep a wedged daemon alive.
        // KeepAlive=true respawns afterward.
        let _ = run_with_timeout(
            "/usr/bin/pkill",
            &["-KILL", "-x", "delfi-sidecar"],
            std::time::Duration::from_secs(5),
        );

        // Step 3: kickstart -k the LaunchAgent as the respawn
        // trigger (belt-and-braces; KeepAlive=true would respawn
        // anyway, but kickstart skips the ThrottleInterval for a
        // snappier recovery). With Step 2 having killed everything,
        // any non-zero exit here is non-fatal: the polling loop on
        // the React side will surface "quit + relaunch manually" if
        // no daemon comes back.
        let uid_out = run_with_timeout(
            "/usr/bin/id", &["-u"],
            std::time::Duration::from_secs(3),
        )?;
        if !uid_out.status.success() {
            return Err("id -u returned non-zero".into());
        }
        let uid = String::from_utf8_lossy(&uid_out.stdout).trim().to_string();
        if uid.is_empty() || !uid.chars().all(|c| c.is_ascii_digit()) {
            return Err(format!("unexpected uid from id -u: {uid:?}"));
        }
        let service = format!("gui/{uid}/com.delfi.bot");

        match run_with_timeout(
            "/bin/launchctl",
            &["kickstart", "-k", &service],
            std::time::Duration::from_secs(15),
        ) {
            Ok(out) if !out.status.success() => {
                let stderr = String::from_utf8_lossy(&out.stderr);
                eprintln!(
                    "[restart_sidecar] kickstart non-zero: {}; \
                    relying on KeepAlive respawn",
                    stderr.trim(),
                );
                Ok(())
            }
            Ok(_) => Ok(()),
            Err(e) => {
                eprintln!("[restart_sidecar] {e}; \
                    KeepAlive will respawn the daemon");
                Ok(())
            }
        }
    })
    .await
    .map_err(|e| format!("restart task panicked: {e}"))??;

    // Block here until the respawned daemon has bound its new port
    // AND we can TCP-connect to it. read_existing_sidecar_port has
    // a 15s internal budget that polls + TCP-probes; on success we
    // update the cached port so the JS layer's waitForSidecar
    // immediately sees the new live port. On timeout we still
    // return Ok - the caller's polling loop will keep retrying
    // and surface a concrete "quit and reopen" message if it
    // ultimately fails.
    //
    // This is the second half of the 2026-05-20 fix: clearing the
    // cache at the top guarantees no stale reads during the
    // kill+respawn window; populating it here guarantees the very
    // first read after this function returns sees the new port.
    if let Some(new_port) = read_existing_sidecar_port(&app).await {
        *state.port.lock().unwrap() = Some(new_port);
        println!("[restart_sidecar] new daemon up on port {new_port}");
    } else {
        eprintln!("[restart_sidecar] daemon did not bind a port within 15s; JS will keep polling");
    }
    Ok(())
    } // end of unreachable_code wrapper
}

/// Re-resolve the daemon's listening port and update ApiState.
///
/// Why this exists: each daemon respawn picks a fresh random port
/// (aiohttp port=0). The cached port in ApiState becomes stale after
/// any respawn (launchd KeepAlive on a crash, the autostart toggle,
/// or a fresh `bash install.sh`). The frontend calls this when a
/// fetch fails with a connection error so it can recover without
/// the user manually restarting the GUI.
///
/// Returns the new port + ready=true on success. On failure (no
/// port file, port not listening) returns ready=false; the caller
/// can keep polling or surface a "daemon is down" message.
#[tauri::command]
async fn refresh_api_port(
    app: tauri::AppHandle,
    state: tauri::State<'_, ApiState>,
) -> Result<ApiPort, String> {
    match read_existing_sidecar_port(&app).await {
        Some(p) => {
            *state.port.lock().unwrap() = Some(p);
            println!("[delfi] refreshed daemon port -> 127.0.0.1:{p}");
            Ok(ApiPort { port: p, ready: true })
        }
        None => Ok(ApiPort { port: 0, ready: false }),
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
    terminated_tx: oneshot::Sender<()>,
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
        let mut terminated_tx = Some(terminated_tx);
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
                    // Signal the lifecycle loop so it can respawn
                    // (on non-macOS-release) or just clean up.
                    if let Some(tx) = terminated_tx.take() {
                        let _ = tx.send(());
                    }
                    break;
                }
                _ => {}
            }
        }
        // Defensive: if the event stream ended without a Terminated
        // event (rare; usually means the IPC channel was dropped),
        // still wake the lifecycle loop so it doesn't hang forever.
        if let Some(tx) = terminated_tx.take() {
            let _ = tx.send(());
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
    // Up to 30 attempts x 500ms = 15s budget. On a fresh launchd
    // respawn (Restart Delfi button, kickstart -k, KeepAlive after
    // crash), the PyInstaller bootloader takes 8-12s to finish
    // unpacking + importing + binding the aiohttp socket. The prior
    // 5s budget gave up before that, so the user saw "Delfi could not
    // start" every time they hit Restart even though the daemon was
    // booting normally.
    //
    // Each attempt does the FULL pipeline (read file -> parse -> TCP
    // probe). The earlier version bailed on the first read/parse
    // failure - when the daemon hadn't yet written the port file,
    // the function returned None immediately and the GUI gave up.
    // Now we retry the whole thing so transient "file missing" or
    // "file empty" states are tolerated.
    for attempt in 0..30u8 {
        let port_opt = std::fs::read_to_string(&port_file)
            .ok()
            .and_then(|s| s.trim().parse::<u16>().ok())
            .filter(|&p| p != 0);
        if let Some(port) = port_opt {
            let addr = format!("127.0.0.1:{port}");
            if let Ok(Ok(_)) = tokio::time::timeout(
                std::time::Duration::from_millis(1500),
                tokio::net::TcpStream::connect(&addr),
            )
            .await
            {
                return Some(port);
            }
        }
        if attempt == 29 {
            return None;
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    None
}

/// Diagnostic logger. Release builds run with windows_subsystem="windows"
/// so println! goes nowhere; we still need to be able to see how far
/// startup got when the GUI hangs on the boot splash. Writes to
/// %TEMP%\delfi-shell.log (one line per call), best-effort.
fn dlog(msg: &str) {
    if let Ok(tmp) = std::env::var("TEMP") {
        let path = std::path::Path::new(&tmp).join("delfi-shell.log");
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true).append(true).open(&path)
        {
            use std::io::Write;
            let _ = writeln!(f, "[{}] pid={} {}",
                chrono_or_secs(), std::process::id(), msg);
        }
    }
}

/// Trivial timestamp without dragging in chrono. Seconds since UNIX epoch.
fn chrono_or_secs() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn main() {
    dlog("==== main() entry ====");
    tauri::Builder::default()
        // Single-instance MUST be the first plugin registered. The
        // plugin's init runs synchronously inside Builder::build and
        // is what detects "another delfi is already running" — if it
        // fires after other plugins have set up state, we waste work
        // initialising things in a process that's about to exit.
        // The callback runs in the FIRST (surviving) instance when
        // someone double-clicks the shortcut: we surface its window
        // instead of letting a duplicate spawn.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            dlog("single-instance callback fired (another delfi tried to start)");
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        // Updater. The front-end calls `check()` from JS on app
        // start; this plugin handles the manifest fetch, Ed25519
        // signature verification, download, and in-place install.
        // Config (GitHub Releases manifest URL + public verifying
        // key) lives in tauri.conf.json under plugins.updater.
        .plugin(tauri_plugin_updater::Builder::new().build())
        // process.relaunch() so the updater path and the restart
        // button can restart the GUI themselves instead of asking
        // the user to quit + reopen.
        .plugin(tauri_plugin_process::init())
        .manage(ApiState {
            port: Mutex::new(None),
            child: Mutex::new(None),
            shutting_down: std::sync::atomic::AtomicBool::new(false),
        })
        .invoke_handler(tauri::generate_handler![
            get_api_port,
            refresh_api_port,
            restart_sidecar,
        ])
        // Window close button = hide, NOT quit.
        //
        // The bot runs 24/7 in a launchd-supervised daemon. The Tauri
        // shell is a viewer for that daemon. If clicking the red close
        // (X) button actually QUIT the app, two bad things happen:
        //   1. The user thinks they paused trading. They didn't; the
        //      daemon kept going. Confusion + a real-money risk surface.
        //   2. Even if they meant to "close the window for now", they
        //      now have to re-launch Delfi from /Applications to see
        //      positions or pause the bot. That's friction we can avoid.
        //
        // Instead, we intercept the close request, prevent the default
        // (which on macOS exits the process), and hide the window. The
        // dock icon stays so a single click brings it back; the
        // RunEvent::Reopen handler below also covers the case where
        // the user clicks the dock icon while no windows are visible.
        //
        // On Windows / Linux we still hide; the user uses Alt-Tab or
        // the taskbar to bring it back. No system-tray icon yet —
        // intentionally out of scope, the daemon is the source of
        // truth and you don't need a tray indicator to know it's
        // running.
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Don't let the OS close-handler tear down the
                // window. Hide it instead. Errors here are non-
                // fatal: if hide() fails the window stays open,
                // which is the safer default.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            dlog("setup() entry");

            // First job on macOS release builds: make sure the
            // LaunchAgent + UI-less sidecar sub-bundle exist and point
            // at this bundle. Idempotent (no-op after the first
            // launch) - see the doc comment on
            // ensure_macos_launchagent for the why.
            #[cfg(target_os = "macos")]
            if !cfg!(debug_assertions) {
                ensure_macos_launchagent();
            }

            // System tray icon. Sits in the Windows notification area
            // (the up-arrow "hidden icons" tray) and the macOS menu bar.
            // Left-click brings the main window forward; right-click
            // opens a menu with Show / Quit. The X button on the window
            // already hides the window (see .on_window_event below) so
            // the tray is the user's path back to it. Without this, an
            // accidental X click on Windows means re-launching Delfi
            // from the Start menu — which is fine for the bot (sidecar
            // keeps running) but kills the GUI's "always-available"
            // promise.
            //
            // "Quit" through the tray menu is intentional full shutdown:
            // app.exit(0) triggers our RunEvent::Exit handler, which
            // kills the sidecar on Windows/Linux (no launchd to
            // supervise it). On macOS the sidecar is launchd-managed so
            // Quit only takes down the GUI.
            let show_item = MenuItem::with_id(
                app, "tray_show", "Show Delfi", true, None::<&str>,
            )?;
            let quit_item = MenuItem::with_id(
                app, "tray_quit", "Quit Delfi", true, None::<&str>,
            )?;
            let tray_menu = Menu::with_items(app, &[&show_item, &quit_item])?;
            dlog("tray: building");
            let tray_icon = app.default_window_icon().cloned();
            if tray_icon.is_none() {
                dlog("tray: WARN no default_window_icon; skipping tray setup");
            }
            if let Some(icon) = tray_icon {
            let _tray = TrayIconBuilder::with_id("delfi-tray")
                .icon(icon)
                .tooltip("Delfi")
                .menu(&tray_menu)
                // Left-click on the tray icon shows the window. The
                // default Tauri behaviour does nothing, which feels
                // broken next to apps like Discord / Spotify that all
                // pop the window on left-click.
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.unminimize();
                            let _ = w.set_focus();
                        }
                    }
                })
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "tray_show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.unminimize();
                            let _ = w.set_focus();
                        }
                    }
                    "tray_quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;
            dlog("tray: built");
            } // end if let Some(icon)

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
                dlog("async: sidecar lookup started");
                // Path 1: probe for an already-running daemon.
                //
                // The probe budget depends on the platform:
                //
                //   macOS:  launchd manages the sidecar as a 24/7
                //           daemon. PyInstaller cold-start can take
                //           ~8-15s, and after a `bash install.sh`
                //           rebuild the daemon goes through a brief
                //           bootout/bootstrap cycle. 30s is the safe
                //           floor here.
                //
                //   Windows/Linux:  no separate daemon supervisor.
                //           Either there's a sidecar from a previous
                //           same-session launch (port file present
                //           AND the port is live), or there isn't and
                //           the GUI must spawn one. A single 1.5s TCP
                //           probe is enough; longer is just dead time
                //           on the boot screen. A stale port file
                //           from a previous run that we used to keep
                //           probing for 60 iterations × 1.5s = 90s
                //           was the headline "loads forever" symptom.
                let probe_iterations: u32 =
                    if cfg!(target_os = "macos") { 60 } else { 1 };
                let mut probed_dead = false;
                for _ in 0..probe_iterations {
                    if let Some(p) = read_existing_sidecar_port(&app_handle).await {
                        let state = app_handle.state::<ApiState>();
                        *state.port.lock().unwrap() = Some(p);
                        dlog(&format!("async: attached to existing daemon port={p}"));
                        println!(
                            "[delfi] connected to running daemon on \
                             127.0.0.1:{p}"
                        );
                        return;
                    }
                    probed_dead = true;
                    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                }
                if probed_dead && !cfg!(target_os = "macos") {
                    // Best-effort: remove the stale port file so a
                    // future launch doesn't pay the same probe cost
                    // for a port nobody's listening on.
                    if let Ok(dir) = app_handle.path()
                        .resolve("", BaseDirectory::AppData) {
                        let pf = dir.join("sidecar.port");
                        if pf.exists() {
                            dlog(&format!("async: removing stale port file {:?}", pf));
                            let _ = std::fs::remove_file(&pf);
                        }
                    }
                }

                // Path 2: depends on build mode AND target OS.
                //
                // RELEASE on macOS: never spawn. Doing so creates a
                // Tauri-owned sidecar that competes with the launchd-
                // managed one. The Tauri spawn binds the port first;
                // launchd's daemon hits the singleton lock and exits,
                // launchd respawns it, hits the lock again, exits, and
                // so on forever. The "Restart Delfi" button only kicks
                // the launchd slot, leaving the Tauri orphan up - so
                // to the user it looks like nothing happened. This was
                // the 2026-05-06 incident.
                //
                // DEV: keep the spawn fallback. Dev mode runs
                // `python main.py` with no LaunchAgent supervision,
                // so the GUI itself is the only path to a running
                // sidecar.
                //
                // NON-MACOS RELEASE: also spawn from the GUI. The
                // launchd LaunchAgent only exists on macOS; on Windows
                // and Linux there is no separate daemon supervisor, so
                // the GUI is the sidecar's parent. The "competing with
                // launchd" failure mode that the macOS release branch
                // avoids cannot happen here.
                if cfg!(debug_assertions) || !cfg!(target_os = "macos") {
                    // Lifecycle loop. Spawn the sidecar, wait for it
                    // to report ready, then watch for termination.
                    // On termination (sidecar crashed or was killed),
                    // respawn with a small backoff - unless the app
                    // is shutting down, in which case exit cleanly.
                    //
                    // macOS-release deliberately does NOT use this
                    // loop: launchd's KeepAlive=true is the canonical
                    // respawn supervisor there. Running both would
                    // race.
                    let mut backoff_secs: u64 = 0;
                    loop {
                        {
                            let state = app_handle.state::<ApiState>();
                            if state.shutting_down.load(std::sync::atomic::Ordering::Acquire) {
                                dlog("async: shutting_down set, exiting respawn loop");
                                break;
                            }
                        }
                        if backoff_secs > 0 {
                            dlog(&format!("async: backoff {backoff_secs}s before respawn"));
                            tokio::time::sleep(std::time::Duration::from_secs(backoff_secs)).await;
                        }

                        dlog("async: spawning sidecar");
                        let port = portpicker::pick_unused_port().unwrap_or(0);
                        let db_path = resolve_db_path(&app_handle);
                        let (ready_tx, ready_rx) = oneshot::channel::<u16>();
                        let (terminated_tx, terminated_rx) = oneshot::channel::<()>();
                        let child = match spawn_sidecar(
                            &app_handle, port, &db_path, ready_tx, terminated_tx,
                        ) {
                            Ok(c) => c,
                            Err(e) => {
                                dlog(&format!("async: spawn_sidecar FAILED: {e}"));
                                eprintln!("[delfi] sidecar spawn failed: {e}");
                                backoff_secs = (backoff_secs + 2).min(30);
                                continue;
                            }
                        };
                        dlog("async: spawn_sidecar OK, waiting for ready");
                        {
                            let state = app_handle.state::<ApiState>();
                            *state.child.lock().unwrap() = Some(child);
                        }

                        let app_handle_for_poll = app_handle.clone();
                        let port_file_watcher = async move {
                            loop {
                                tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                                if let Some(p) = read_existing_sidecar_port(&app_handle_for_poll).await {
                                    return p;
                                }
                            }
                        };
                        let ready_timeout = tokio::time::sleep(std::time::Duration::from_secs(120));
                        let mut ready_ok = false;
                        tokio::select! {
                            res = ready_rx => {
                                if let Ok(bound_port) = res {
                                    let state = app_handle.state::<ApiState>();
                                    *state.port.lock().unwrap() = Some(bound_port);
                                    dlog(&format!("async: READY via stdout, port={bound_port}"));
                                    println!("[delfi] spawned sidecar ready on 127.0.0.1:{bound_port}");
                                    ready_ok = true;
                                } else {
                                    dlog("async: ready_rx channel dropped before READY line");
                                    eprintln!("[delfi] sidecar ready channel dropped before READY line");
                                }
                            }
                            bound_port = port_file_watcher => {
                                let state = app_handle.state::<ApiState>();
                                *state.port.lock().unwrap() = Some(bound_port);
                                dlog(&format!("async: READY via port file, port={bound_port}"));
                                println!(
                                    "[delfi] daemon came online on 127.0.0.1:{bound_port}"
                                );
                                ready_ok = true;
                            }
                            _ = ready_timeout => {
                                dlog("async: TIMEOUT 120s no ready signal");
                                eprintln!("[delfi] sidecar did not become ready within 120s");
                            }
                        }

                        if !ready_ok {
                            // The sidecar process is alive but never
                            // reported ready. Kill it before looping
                            // so we don't pile up zombie processes
                            // (the singleton mutex would also block
                            // future spawns).
                            let state = app_handle.state::<ApiState>();
                            if let Some(c) = state.child.lock().unwrap().take() {
                                let _ = c.kill();
                            }
                            *state.port.lock().unwrap() = None;
                            backoff_secs = (backoff_secs + 2).min(30);
                            continue;
                        }

                        // Reset backoff once we've had a successful
                        // start. Crash-loops use exponential backoff;
                        // a stable-then-crash gets a quick respawn.
                        backoff_secs = 0;

                        // Block until the sidecar terminates. The
                        // CommandEvent handler in spawn_sidecar fires
                        // terminated_tx when it sees Terminated (or
                        // when the IPC channel closes).
                        let _ = terminated_rx.await;
                        dlog("async: sidecar terminated");

                        let state = app_handle.state::<ApiState>();
                        *state.port.lock().unwrap() = None;
                        // child slot will be replaced on next spawn.
                        // Drop the dead handle without calling kill
                        // (it's already dead).
                        let _ = state.child.lock().unwrap().take();

                        if state.shutting_down.load(std::sync::atomic::Ordering::Acquire) {
                            dlog("async: shutting_down set after terminate, exiting respawn loop");
                            break;
                        }

                        // Brief backoff so a tight crash loop doesn't
                        // spin a sidecar 100x/sec. 2s gives the user
                        // time to read whatever the BootScreen shows.
                        backoff_secs = 2;
                    }
                } else {
                    // macOS release path. Other OSes are handled by
                    // the spawn branch above (which is also dev-mode).
                    //
                    // Kickstart launchd's LaunchAgent in case it has
                    // crashed and is mid-throttle, then keep polling
                    // the port file for another 60s. If still nothing,
                    // surface the error to the splash screen and let
                    // the user click Restart.
                    eprintln!(
                        "[delfi] no daemon after 30s probe - kickstarting \
                         launchd LaunchAgent and continuing to wait"
                    );
                    let _ = std::process::Command::new("/usr/bin/id")
                        .arg("-u")
                        .output()
                        .ok()
                        .and_then(|o| {
                            if !o.status.success() { return None; }
                            let uid = String::from_utf8_lossy(&o.stdout).trim().to_string();
                            if uid.is_empty() || !uid.chars().all(|c| c.is_ascii_digit()) {
                                return None;
                            }
                            std::process::Command::new("/bin/launchctl")
                                .args(["kickstart", &format!("gui/{uid}/com.delfi.bot")])
                                .output()
                                .ok()
                        });
                    // Continue probing for another 60s.
                    for _ in 0..120 {
                        if let Some(p) = read_existing_sidecar_port(&app_handle).await {
                            let state = app_handle.state::<ApiState>();
                            *state.port.lock().unwrap() = Some(p);
                            println!(
                                "[delfi] connected to launchd daemon on 127.0.0.1:{p} \
                                 after kickstart"
                            );
                            return;
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
                    }
                    eprintln!(
                        "[delfi] launchd daemon failed to come up within 90s of \
                         total probing - the GUI will surface 'Delfi could not start'"
                    );
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
            // macOS dock-click while the window is hidden.
            //
            // On macOS, clicking the dock icon for an app that's
            // already running but has no visible windows fires
            // RunEvent::Reopen. By default Tauri does nothing here,
            // so once the user has hidden Delfi (via the X button)
            // there's no path back to the UI short of relaunching.
            // We re-show + focus the main window so the dock icon
            // behaves like every other macOS app.
            #[cfg(target_os = "macos")]
            if let RunEvent::Reopen { .. } = event {
                if let Some(win) = app_handle.get_webview_window("main") {
                    let _ = win.show();
                    let _ = win.set_focus();
                }
            }

            if let RunEvent::Exit = event {
                // Signal the respawn loop that we're shutting down so
                // it doesn't race a new spawn against this teardown.
                {
                    let state = app_handle.state::<ApiState>();
                    state.shutting_down.store(true, std::sync::atomic::Ordering::Release);
                }
                if cfg!(debug_assertions) || !cfg!(target_os = "macos") {
                    // Dev mode, or any non-macOS release: the GUI is
                    // the sidecar's parent so kill it on exit, else
                    // we leak a Python process every relaunch.
                    let state = app_handle.state::<ApiState>();
                    // Bind the lock guard to a local so it drops before
                    // `state` does (otherwise the borrow checker sees
                    // the guard's destructor running after `state` is
                    // gone).
                    let mut child_slot = state.child.lock().unwrap();
                    if let Some(child) = child_slot.take() {
                        let _ = child.kill();
                    }
                    // Windows safety net: even if state.child is None
                    // (the GUI attached to a sidecar from a previous
                    // session and never owned a CommandChild handle),
                    // taskkill any lingering delfi-sidecar.exe. The
                    // singleton mutex guarantees there's at most one,
                    // and PyInstaller's bootloader + child are both
                    // named delfi-sidecar.exe so /T sweeps the tree.
                    #[cfg(target_os = "windows")]
                    {
                        let _ = std::process::Command::new("taskkill")
                            .args(["/F", "/T", "/IM", "delfi-sidecar.exe"])
                            .output();
                    }
                } else {
                    // macOS release: drop the handle without killing.
                    // This detaches the child from our process; launchd
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
