import { defineConfig, devices } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const storageDir = path.join(os.tmpdir(), "gpt-image-studio-playwright-storage");
const port = Number(process.env.PLAYWRIGHT_PORT || 8123);

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "on-first-retry",
  },
  webServer: {
    command: "pwsh -NoLogo -NoProfile -Command \"& 'backend/.venv/Scripts/python.exe' backend/run.py\"",
    cwd: rootDir,
    url: `http://127.0.0.1:${port}/auth/login`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      HOST: "127.0.0.1",
      PORT: String(port),
      IMAGE_API_BASE_URL: "https://api.asxs.top/v1",
      IMAGE_API_KEY: "sk-playwright",
      DATABASE_PATH: path.join(storageDir, "app.db"),
      STORAGE_DIR: storageDir,
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
});
