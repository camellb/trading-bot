// Tauri's build helper. Generates the Cargo metadata Tauri needs to
// resolve the bundled web assets and embed the icon set declared in
// tauri.conf.json. Without this file the `tauri::generate_context!()`
// macro in src/main.rs has nothing to point at.

fn main() {
    tauri_build::build()
}
