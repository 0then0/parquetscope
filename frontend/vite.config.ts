import { readFileSync } from "node:fs";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const projectIcon = `data:image/svg+xml,${encodeURIComponent(readFileSync(new URL("../docs/icon.svg", import.meta.url), "utf8"))}`;

export default defineConfig({
  plugins: [
    react(),
    {
      name: "parquetscope-project-icon",
      transformIndexHtml() {
        return [{ tag: "link", attrs: { rel: "icon", type: "image/svg+xml", href: projectIcon } }];
      },
    },
  ],
  build: { outDir: "../src/parquetscope/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8400", "/offline.html": "http://127.0.0.1:8400" } },
});
