import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/lab",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: "http://127.0.0.1:8418/parquetscope/",
    viewport: { width: 1440, height: 1100 },
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "../.venv/bin/python -m http.server 8418 --bind 127.0.0.1 --directory ../.runtime/pages",
    url: "http://127.0.0.1:8418/parquetscope/",
    reuseExistingServer: false,
  },
});
