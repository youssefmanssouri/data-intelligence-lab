import { existsSync } from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
// Backend interpreter: DIL_PYTHON when set (CI), else the repository venv if it
// exists, else the system Python on PATH.
const isWindows = process.platform === "win32";
const venvPython = path.join(__dirname, "..", ".venv",
  ...(isWindows ? ["Scripts", "python.exe"] : ["bin", "python"]));
const python =
  process.env.DIL_PYTHON ??
  (existsSync(venvPython) ? venvPython : isWindows ? "python" : "python3");
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
