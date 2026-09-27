import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The hub's own pages. `@site` points at the site UI sources so the toolkit (ui.tsx, styles.css, ThemeToggle)
// is shared, not copied. Assets go under /hub-assets so they never collide with a site's /assets.
export default defineConfig({
  plugins: [react()],
  // dedupe: files under ../../frontend/src must use THIS app's React, not frontend/node_modules' copy
  resolve: { alias: { "@site": fileURLToPath(new URL("../../frontend/src", import.meta.url)) }, dedupe: ["react", "react-dom"] },
  build: { outDir: "dist", assetsDir: "hub-assets", emptyOutDir: true },
  server: { port: 5174, proxy: { "/api": "http://localhost:8000", "/auth": "http://localhost:8000", "/s": "http://localhost:8000", "/agent": { target: "ws://localhost:8000", ws: true } } },
});
