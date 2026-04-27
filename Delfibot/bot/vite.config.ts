import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite + Tauri config.
//
// `tauri.conf.json` declares `devUrl: http://localhost:1420` and runs
// `npm run dev` as `beforeDevCommand`, so the dev server has to bind to
// 1420 and disable HMR fallbacks that would change the port. The
// `clearScreen: false` keeps Vite's startup logs in the same terminal
// as the Tauri shell logs (helpful while debugging the sidecar
// handshake).

const port = 1420;

export default defineConfig(async () => ({
  plugins: [react()],

  clearScreen: false,
  server: {
    port,
    strictPort: true,
    host: "127.0.0.1",
    hmr: {
      protocol: "ws",
      host: "127.0.0.1",
      port: port + 1,
    },
    fs: {
      strict: true,
    },
  },

  // Tauri uses Chromium 90+ on Windows / WebKit on macOS; both support
  // ES2022. Targeting `es2022` keeps the dev bundle small.
  build: {
    target: "es2022",
    chunkSizeWarningLimit: 1500,
  },

  envPrefix: ["VITE_", "TAURI_"],
}));
