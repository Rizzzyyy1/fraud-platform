/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Development only: the Vite dev server forwards /api to the local console service, which holds
// the scoring API key server-side. The production build is served by the console itself.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8200" },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: false,
  },
});
