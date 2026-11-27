// Fixed same-origin proxy in front of Next. No body cloning or multipart parsing.
import http from "node:http";
import {
  backendOrigin,
  isApiPath,
  inboundPolicy,
  PROXY_TIMEOUT_MS,
  writePortFile,
  cleanupPortFile,
} from "../config/proxy.mjs";
import { apiError, createApiProxy } from "./api-proxy.mjs";

process.env.NEXT_TELEMETRY_DISABLED = "1";
const dev = process.argv.includes("--dev");
const portIndex = process.argv.indexOf("--port");
const requestedPort = portIndex >= 0 ? Number(process.argv[portIndex + 1]) : 3000;
if (!Number.isInteger(requestedPort) || requestedPort < 0 || requestedPort > 65535)
  throw new Error("Invalid frontend port.");

const portFileIndex = process.argv.indexOf("--port-file");
const portFilePath = portFileIndex >= 0 ? process.argv[portFileIndex + 1] : null;
if (portFileIndex >= 0 && (!portFilePath || portFilePath.startsWith("-"))) {
  throw new Error("Missing or invalid path for --port-file.");
}

const { default: next } = await import("next");
const app = next({ dev, hostname: "127.0.0.1", port: requestedPort });
await app.prepare();
// app.prepare loads Next .env.local; this value is never taken from a request.
const origin = new URL(backendOrigin(process.env.DIL_BACKEND_ORIGIN));
const handler = app.getRequestHandler();
const proxy = createApiProxy({ origin });
let acceptsInbound = requestedPort !== 0 ? inboundPolicy(requestedPort) : null;
const server = http.createServer((req, res) => {
  if (!acceptsInbound) {
    const addr = server.address();
    if (addr && typeof addr === "object" && addr.port) {
      acceptsInbound = inboundPolicy(addr.port);
    }
  }
  if (!acceptsInbound || !acceptsInbound(req)) {
    apiError(res, 403, "Forbidden");
    return;
  }
  const path = (req.url ?? "").split("?")[0];
  if (isApiPath(path)) {
    if (!["GET", "POST"].includes(req.method ?? "")) {
      res.setHeader("allow", "GET, POST");
      apiError(res, 405, "Method Not Allowed");
      return;
    }
    proxy(req, res);
    return;
  }
  // Unknown API paths never reach Next's request body cloning code.
  if (path.startsWith("/api/")) {
    apiError(res, 404, "Not Found");
    return;
  }
  handler(req, res).catch(() => {
    apiError(res, 500, "Internal Server Error");
  });
});
server.requestTimeout = PROXY_TIMEOUT_MS + 5_000;
server.headersTimeout = 15_000;
server.listen(requestedPort, "127.0.0.1", () => {
  const addr = server.address();
  const actualPort = addr && typeof addr === "object" ? addr.port : requestedPort;
  acceptsInbound = inboundPolicy(actualPort);
  if (app?.options) {
    app.options.port = actualPort;
  }
  if (portFilePath) {
    try {
      writePortFile(portFilePath, actualPort);
    } catch (err) {
      cleanupPortFile(portFilePath);
      server.closeAllConnections();
      server.close();
      console.error(err.message);
      process.exit(1);
    }
  }
  console.info(`Data Intelligence Lab: http://127.0.0.1:${actualPort}`);
});
let isShuttingDown = false;
let shutdownPromise = null;

async function executeShutdown(exitCode = 0) {
  let primaryError = null;

  // 1. Stop accepting new inbound connections
  let serverClosed = false;
  const closePromise = new Promise((resolve) => {
    try {
      server.close((err) => {
        serverClosed = true;
        if (err && !primaryError) primaryError = err;
        resolve();
      });
    } catch (err) {
      serverClosed = true;
      if (!primaryError) primaryError = err;
      resolve();
    }
  });

  // 2. Allow up to 2.0s grace period for existing requests to complete
  let forceCloseTimer = null;
  const timeoutPromise = new Promise((resolve) => {
    forceCloseTimer = setTimeout(() => {
      resolve();
    }, 2000);
  });

  // Await either normal server close or timeout
  await Promise.race([closePromise, timeoutPromise]);
  if (forceCloseTimer) {
    clearTimeout(forceCloseTimer);
  }

  // 3. If server has not closed yet (connections remaining after grace period), force close
  if (!serverClosed) {
    try {
      if (typeof server.closeAllConnections === "function") {
        server.closeAllConnections();
      }
    } catch (err) {
      if (!primaryError) primaryError = err;
    }
    // Conclusively observe server closure after destroying connections
    await Promise.race([
      closePromise,
      new Promise((resolve) => setTimeout(resolve, 500)),
    ]);
  }

  // 4. Close Next.js application server
  try {
    if (app && typeof app.close === "function") {
      await app.close();
    }
  } catch (err) {
    if (!primaryError) primaryError = err;
  }

  // 5. Remove this process's port file after listener closure and application cleanup
  try {
    cleanupPortFile(portFilePath);
  } catch (err) {
    if (!primaryError) primaryError = err;
  }

  if (primaryError) {
    console.error("Error during graceful shutdown:", primaryError);
  }

  // 6. Exit normally
  process.exit(exitCode);
}

function gracefulShutdown() {
  if (isShuttingDown) return shutdownPromise;
  isShuttingDown = true;
  shutdownPromise = executeShutdown(0);
  return shutdownPromise;
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, gracefulShutdown);
}

// Windows child process lifecycle: detect parent pipe closure (EOF)
if (process.stdin && !process.stdin.isTTY && process.stdin.constructor?.name === "Socket") {
  process.stdin.resume();
  process.stdin.on("end", gracefulShutdown);
  process.stdin.on("close", gracefulShutdown);
}
