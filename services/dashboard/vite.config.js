import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the gateway runs on :8000; in the cluster the Ingress routes /api and /mcp to it.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000", "/mcp": "http://localhost:8000" } },
});
