import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The client talks to the Layer 2 Serving API. In dev we proxy /api and /ops to it so the
// browser makes same-origin requests; in production set VITE_API_BASE_URL to the L2 host.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ops": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
