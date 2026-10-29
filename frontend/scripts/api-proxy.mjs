import http from "node:http";
import { API_HEADERS, PROXY_TIMEOUT_MS } from "../config/proxy.mjs";

const HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
function requestHeaders(headers) {
  const excluded = new Set([
    ...HOP_HEADERS,
    ...String(headers.connection ?? "")
      .toLowerCase()
      .split(",")
      .map((x) => x.trim()),
  ]);
  return Object.fromEntries(
    Object.entries(headers).filter(([key]) => !excluded.has(key)),
  );
}

export function apiError(res, status, message) {
  if (res.destroyed || res.writableFinished) return;
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, {
    ...API_HEADERS,
    "content-type": "text/plain; charset=utf-8",
    connection: "close",
  });
  res.end(message);
}

function responseType(req, response) {
  const status = response.statusCode;
  const type =
    typeof response.headers["content-type"] === "string"
      ? response.headers["content-type"].split(";")[0].trim().toLowerCase()
      : "";
  const encoding = response.headers["content-encoding"];
  if (encoding !== undefined && encoding !== "identity") return null;
  const success = req.method === "POST" ? 201 : 200;
  if (status === success && type === "application/json") return type;
  if (
    Number.isInteger(status) &&
    status >= 400 &&
    status <= 599 &&
    type === "application/problem+json"
  )
    return type;
  return null;
}

// Dependency injection is for controlled stream tests only; startup owns the destination.
export function createApiProxy({
  origin,
  timeoutMs = PROXY_TIMEOUT_MS,
  request = http.request,
  schedule = setTimeout,
  cancelSchedule = clearTimeout,
}) {
  return (req, res) => {
    let upstream;
    try {
      upstream = request({
        protocol: origin.protocol,
        hostname: origin.hostname.replace(/^\[|\]$/g, ""),
        port: origin.port,
        path: req.url,
        method: req.method,
        headers: {
          ...requestHeaders(req.headers),
          host: origin.host,
          "accept-encoding": "identity",
        },
      });
    } catch {
      apiError(res, 502, "Bad Gateway");
      return;
    }

    let settled = false;
    let response;
    const listeners = [];
    const timer = schedule(() => fail(504, "Gateway Timeout"), timeoutMs);
    function watch(stream, event, listener) {
      stream.on(event, listener);
      listeners.push(() => stream.off(event, listener));
    }
    // Retain error guards until native close: destroy() can emit an asynchronous error.
    function guard(stream) {
      const onError = () => fail(502, "Bad Gateway");
      stream.on("error", onError);
      stream.once("close", () => stream.off("error", onError));
    }
    function cleanup() {
      cancelSchedule(timer);
      for (const remove of listeners) remove();
      req.unpipe(upstream);
      response?.unpipe(res);
      response?.destroy();
      upstream.destroy();
    }
    function closeInput() {
      res.off("finish", closeInput);
      res.off("close", closeInput);
      if (!req.complete && !req.destroyed) req.destroy();
    }
    function finish() {
      if (settled) return;
      settled = true;
      cleanup();
      closeInput();
    }
    function fail(status, message) {
      if (settled) return;
      settled = true;
      cleanup();
      if (!req.complete && !req.destroyed) {
        res.once("finish", closeInput);
        res.once("close", closeInput);
      }
      apiError(res, status, message);
      if (res.destroyed || res.writableFinished) closeInput();
    }
    guard(req);
    guard(res);
    guard(upstream);
    watch(req, "aborted", () => {
      finish();
      if (!res.writableFinished) res.destroy();
    });
    watch(res, "finish", finish);
    watch(res, "close", finish);
    watch(upstream, "response", (incoming) => {
      response = incoming;
      guard(response);
      const type = responseType(req, response);
      if (!type) {
        fail(502, "Bad Gateway");
        return;
      }
      // Do not forward cookies, redirects, content length, or upstream security policies.
      res.writeHead(response.statusCode, {
        ...API_HEADERS,
        "content-type": `${type}; charset=utf-8`,
        ...(!req.complete ? { connection: "close" } : {}),
      });
      watch(response, "close", () => {
        if (!response.readableEnded) fail(502, "Bad Gateway");
      });
      response.pipe(res);
      // Upstream end intentionally does not settle the downstream deadline.
    });
    upstream.flushHeaders();
    req.pipe(upstream);
  };
}
