import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is proxied so the browser sees one origin: the HttpOnly login cookies
// (path /api/v1/auth, SameSite=Strict) work without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
});
