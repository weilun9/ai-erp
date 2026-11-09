import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is proxied so the browser sees one origin: the HttpOnly login cookies
// (path /api/v1/auth, SameSite=Strict) work without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
        // Backend unreachable -> 503 (as nginx would send 502/504), so the till can tell
        // "offline" apart from a real server error and queue the sale.
        configure: (proxy) => {
          proxy.on("error", (_err, _req, res) => {
            const out = res as unknown as {
              headersSent: boolean;
              writeHead: (code: number, headers: Record<string, string>) => void;
              end: (body: string) => void;
            };
            if (!out.headersSent) {
              out.writeHead(503, { "Content-Type": "application/json" });
            }
            out.end(JSON.stringify({ error: { code: "UNREACHABLE", message: "API unreachable" } }));
          });
        },
      },
    },
  },
});
