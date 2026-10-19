import { resolve } from "node:path";
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests/browser",
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  use: {
    baseURL: "http://127.0.0.1:8411",
    viewport: { width: 1440, height: 1100 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../.venv/bin/parquetscope serve --demo --port 8411",
    url: "http://127.0.0.1:8411/api/session",
    reuseExistingServer: false,
    env: { PARQUETSCOPE_HOME: process.env.PARQUETSCOPE_HOME ?? resolve("../.runtime") },
  },
});
