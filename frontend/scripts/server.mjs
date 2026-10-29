// Fixed same-origin proxy in front of Next. No body cloning or multipart parsing.
import http from "node:http";
import {
  backendOrigin,
  isApiPath,
  inboundPolicy,
  PROXY_TIMEOUT_MS,
} from "../config/proxy.mjs";
import { apiError, createApiProxy } from "./api-proxy.mjs";

process.env.NEXT_TELEMETRY_DISABLED = "1";
const dev = process.argv.includes("--dev");
const portIndex = process.argv.indexOf("--port");
const port = portIndex >= 0 ? Number(process.argv[portIndex + 1]) : 3000;
if (!Number.isInteger(port) || port < 1 || port > 65535)
  throw new Error("Invalid frontend port.");
const { default: next } = await import("next");
const app = next({ dev, hostname: "127.0.0.1", port });
await app.prepare();
// app.prepare loads Next .env.local; this value is never taken from a request.
const origin = new URL(backendOrigin(process.env.DIL_BACKEND_ORIGIN));
const handler = app.getRequestHandler();
const proxy = createApiProxy({ origin });
const acceptsInbound = inboundPolicy(port);
const server = http.createServer((req, res) => {
  if (!acceptsInbound(req)) {
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
server.listen(port, "127.0.0.1", () => {
  console.info(`Data Intelligence Lab: http://127.0.0.1:${port}`);
});
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () => {
    server.closeAllConnections();
    server.close();
    app.close().finally(() => process.exit(0));
  });
