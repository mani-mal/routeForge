import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API so the UI can call same-origin paths (/api, /v1)
// and the SSE stream works without CORS friction. In production the built
// assets are served by / behind the same ingress as the API.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
      "/v1": { target: "http://127.0.0.1:8080", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
});
