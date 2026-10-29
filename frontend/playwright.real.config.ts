import { defineConfig, devices } from "@playwright/test";
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
      command: "..\\.venv\\Scripts\\python.exe tests/backend_server.py",
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm.cmd run start -- --port 3101",
      url: "http://127.0.0.1:3101",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
