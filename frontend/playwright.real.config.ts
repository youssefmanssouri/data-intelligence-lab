import { defineConfig, devices } from "@playwright/test";
// Backend interpreter: DIL_PYTHON when set (CI), else the repository venv.
const python =
  process.env.DIL_PYTHON ??
  (process.platform === "win32" ? "..\\.venv\\Scripts\\python.exe" : "../.venv/bin/python");
export default defineConfig({
  testDir: "./tests",
  testMatch: "real.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:3101",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [{ name: "real-chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `"${python}" tests/backend_server.py`,
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "node scripts/server.mjs --port 3101",
      url: "http://127.0.0.1:3101",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
