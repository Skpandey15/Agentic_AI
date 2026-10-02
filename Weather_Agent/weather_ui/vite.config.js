import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The service API key stays on the dev-server side (no VITE_ prefix => never bundled
// into browser code). The browser only talks to /api/*, and the proxy adds the header.
const target = process.env.API_TARGET || "http://127.0.0.1:8000";
const apiKey = process.env.WX_API_KEY || "";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        headers: { "x-api-key": apiKey },
      },
    },
  },
});
