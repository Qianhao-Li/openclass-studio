import { defineConfig, devices } from "@playwright/test";
import os from "node:os";
import path from "node:path";

const root = path.resolve(__dirname, "../..");
const origin = process.env.OPENCLASS_STUDIO_E2E_URL || "http://127.0.0.1:3510";
export default defineConfig({
  testDir: "./e2e", testMatch: "local-studio.spec.ts", fullyParallel: false, timeout: 90_000,
  expect: { timeout: 20_000 },
  use: { baseURL: origin, viewport: { width: 1440, height: 1000 }, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "node scripts/studio.mjs --dev --no-open", cwd: root,
    url: origin + "/studio", reuseExistingServer: !process.env.CI, timeout: 120_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
    env: { ...process.env, OPENCLASS_STUDIO_DATA_DIR: process.env.OPENCLASS_STUDIO_DATA_DIR || path.join(os.tmpdir(), "openclass-studio-browser-tests") },
  },
});
