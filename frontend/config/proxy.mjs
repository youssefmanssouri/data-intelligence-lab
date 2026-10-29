/** Configuration only: no request, query, header or browser value selects the origin. */
export function backendOrigin(value = "http://127.0.0.1:8000") {
  if (
    !/^http:\/\/(?:127(?:\.(?:0|[1-9]\d{0,2})){3}|\[::1\]):[1-9]\d{0,4}$/.test(
      value,
    )
  ) {
    throw new Error(
      "DIL_BACKEND_ORIGIN must be an HTTP loopback IP origin with an explicit port.",
    );
  }
  const url = new URL(value);
  if (
    Number(url.port || 80) > 65535 ||
    url.hostname
      .split(".")
      .some((part) => /^\d+$/.test(part) && Number(part) > 255)
  ) {
    throw new Error("DIL_BACKEND_ORIGIN has an invalid IP address or port.");
  }
  return url.origin;
}

export const PROXY_TIMEOUT_MS = 120_000;
export const SECURITY_HEADERS = Object.freeze({
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
});
export const API_HEADERS = Object.freeze({
  ...SECURITY_HEADERS,
  "cache-control": "no-store, max-age=0",
  "content-security-policy":
    "default-src 'none'; frame-ancestors 'none'; sandbox",
});

function singleHeader(req, name) {
  const occurrences = (req.rawHeaders ?? []).filter(
    (value, index) => index % 2 === 0 && value.toLowerCase() === name,
  ).length;
  if (occurrences > 1 || Array.isArray(req.headers[name])) return null;
  return req.headers[name];
}

// Build trust from startup configuration, never from a request's Host/forwarded headers.
export function inboundPolicy(port) {
  const origins = new Map([
    [`127.0.0.1:${port}`, `http://127.0.0.1:${port}`],
    [`localhost:${port}`, `http://localhost:${port}`],
  ]);
  return (req) => {
    const host = singleHeader(req, "host");
    if (typeof host !== "string" || !origins.has(host.toLowerCase()))
      return false;
    const path = (req.url ?? "").split("?")[0];
    if (req.method !== "POST" || path !== "/api/v1/datasets/upload")
      return true;
    const origin = singleHeader(req, "origin");
    const site = singleHeader(req, "sec-fetch-site");
    if (site !== undefined && site !== "same-origin" && site !== "none")
      return false;
    // Headerless CLI clients are allowed. Browser-like requests must supply Origin.
    if (origin === undefined) return site === undefined;
    return (
      typeof origin === "string" && origin === origins.get(host.toLowerCase())
    );
  };
}

export function isApiPath(path) {
  return (
    path === "/health" ||
    path === "/ready" ||
    path === "/api/v1/datasets/upload" ||
    /^\/api\/v1\/datasets\/[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(path)
  );
}
