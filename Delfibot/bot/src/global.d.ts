// Pulls in Vite's ambient types (ImportMeta.env, asset imports, etc.).
/// <reference types="vite/client" />

// Tauri injects a global into the webview that exposes `invoke` and
// other internal helpers. Declared so we can feature-detect "running
// inside Tauri" without a TS error.

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export {};
