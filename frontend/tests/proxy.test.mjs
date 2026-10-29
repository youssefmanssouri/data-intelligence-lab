import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import {
  backendOrigin,
  isApiPath,
  PROXY_TIMEOUT_MS,
} from "../config/proxy.mjs";

// A controlled loopback upstream verifies transport only, not FastAPI behaviour.
let server, frontend;
const observations = new Map();
before(
  async () => {
    server = http.createServer((req, res) => {
      const scenario = req.headers["x-test-scenario"];
      const observation = { bytes: 0, firstAt: 0, ended: false, url: req.url };
      observations.set(scenario, observation);
      req.on("data", (chunk) => {
        observation.bytes += chunk.length;
        observation.firstAt ||= Date.now();
      });
      req.on("close", () => {
        observation.requestClosed = true;
      });
      res.on("close", () => {
        observation.responseClosed = true;
      });
      if (scenario === "disconnect-response") {
        res.writeHead(200, { "content-type": "application/json" });
        const chunk = Buffer.alloc(64 * 1024, "x");
        let sent = 0;
        function write() {
          while (!res.destroyed && sent < 1024) {
            sent++;
            if (!res.write(chunk)) {
              res.once("drain", write);
              return;
            }
          }
          if (!res.destroyed) {
            observation.responseEnded = true;
            res.end();
          }
        }
        write();
        return;
      }
      req.on("end", () => {
        observation.ended = true;
        if (scenario === "timeout") return;
        res.writeHead(req.method === "POST" ? 201 : 200, {
          "content-type": "application/json",
        });
        res.end('{"status":"ok"}');
      });
    });
    server.listen(8000, "127.0.0.1");
    await once(server, "listening");
    frontend = spawn(
      process.execPath,
      ["scripts/server.mjs", "--port", "3102"],
      {
        env: {
          ...process.env,
          DIL_BACKEND_ORIGIN: "http://127.0.0.1:8000",
          NEXT_TELEMETRY_DISABLED: "1",
        },
        stdio: ["ignore", "pipe", "inherit"],
      },
    );
    // Consume but never record requests, upstream errors or dataset metadata.
    frontend.stdout.resume();
    for (let i = 0; i < 100; i++) {
      if (frontend.exitCode !== null)
        throw new Error("Frontend test server exited.");
      try {
        const r = await fetch("http://127.0.0.1:3102");
        if (r.ok) return;
      } catch {}
      await delay(200);
    }
    throw new Error("Frontend test server did not start.");
  },
  { timeout: 30_000 },
);
after(async () => {
  if (frontend && frontend.exitCode === null && frontend.signalCode === null) {
    frontend.kill();
    await once(frontend, "exit");
  }
  server?.closeAllConnections();
  await new Promise((resolve) => server?.close(resolve));
});
function send(scenario, path = "/api/v1/datasets/upload", headers = {}) {
  const req = http.request(`http://127.0.0.1:3102${path}`, {
    method: "POST",
    headers: { "x-test-scenario": scenario, ...headers },
  });
  const response = new Promise((resolve, reject) => {
    req.on("response", (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () =>
        resolve({ status: res.statusCode, headers: res.headers, body }),
      );
    });
    req.on("error", reject);
  });
  return { req, response };
}
test("configuration rejects non-loopback, credentials, alternate schemes and ambiguous origins", () => {
  for (const value of [
    "http://example.com:8000",
    "http://localhost:8000",
    "http://0.0.0.0:8000",
    "https://127.0.0.1:8000",
    "http://user:password@127.0.0.1:8000",
    "http://127.0.0.1:8000/path",
    "http://127.0.0.1:8000?target=evil",
    "http://127.0.0.256:8000",
    "http://127.0.0.1:65536",
    "http://127.1:8000",
  ])
    assert.throws(() => backendOrigin(value));
  assert.equal(backendOrigin("http://127.0.0.1:8000"), "http://127.0.0.1:8000");
  assert.equal(backendOrigin("http://[::1]:8000"), "http://[::1]:8000");
  assert.equal(isApiPath("/api/v1/datasets/../x"), false);
});
test("actual proxy forwards first chunk before upload completes and preserves >10 MiB bodies", async () => {
  const boundary = "dil-stream-boundary";
  const prefix = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="transport.csv"\r\nContent-Type: text/csv\r\n\r\n`,
  );
  const { req, response } = send("stream", "/api/v1/datasets/upload", {
    "content-type": `multipart/form-data; boundary=${boundary}`,
  });
  req.write(prefix);
  req.write(Buffer.from("a,b\n"));
  for (let i = 0; i < 50 && !observations.get("stream")?.firstAt; i++)
    await delay(20);
  assert.ok(
    observations.get("stream")?.firstAt,
    "upstream must receive bytes while producer remains open",
  );
  assert.equal(observations.get("stream").ended, false);
  const chunk = Buffer.alloc(64 * 1024, "x");
  let bytes = prefix.length + 4;
  for (let i = 0; i < 192; i++) {
    if (!req.write(chunk)) await once(req, "drain");
    bytes += chunk.length;
  }
  const suffix = Buffer.from(`\r\n--${boundary}--\r\n`);
  req.end(suffix);
  bytes += suffix.length;
  const result = await response;
  assert.equal(result.status, 201);
  assert.equal(observations.get("stream").bytes, bytes);
  assert.ok(bytes > 10 * 1024 * 1024);
  assert.match(result.headers["cache-control"], /no-store/);
});
test("browser-supplied destination query stays at the fixed configured origin", async () => {
  const { req, response } = send(
    "fixed",
    "/api/v1/datasets/upload?url=http://192.0.2.1:9&destination=http://example.com",
  );
  req.end("transport-only");
  assert.equal((await response).status, 201);
  assert.match(observations.get("fixed").url, /^\/api\/v1\/datasets\/upload\?/);
});
test("unknown API paths and deletion do not proxy", async () => {
  const unknown = await fetch("http://127.0.0.1:3102/api/unknown");
  assert.equal(unknown.status, 404);
  const deletion = await fetch(
    "http://127.0.0.1:3102/api/v1/datasets/ds_test",
    { method: "DELETE" },
  );
  assert.equal(deletion.status, 405);
  assert.equal(deletion.headers.get("allow"), "GET, POST");
});
test("A: actual proxy rejects hostile/ambiguous authorities and browser upload headers", async () => {
  for (const headers of [
    { host: "evil.example:3102" },
    { host: "127.0.0.1:3103" },
    { host: "127.0.0.1:3102,evil.example" },
    { origin: "https://evil.example" },
    { origin: "null" },
    { origin: "http://localhost:3102" },
    { origin: "http://127.0.0.1:3102", "sec-fetch-site": "cross-site" },
    { "sec-fetch-site": "same-origin" },
  ]) {
    const { req, response } = send(
      "rejected-authority",
      "/api/v1/datasets/upload",
      headers,
    );
    req.end();
    const result = await response;
    assert.equal(result.status, 403);
    assert.equal(result.body, "Forbidden");
    assert.equal(result.headers["referrer-policy"], "no-referrer");
  }
  assert.equal(observations.has("rejected-authority"), false);
  // Node's HTTP client rejects Host arrays before sending; use the wire format.
  const socket = net.connect(3102, "127.0.0.1");
  let reply = "";
  socket.setEncoding("utf8");
  socket.on("data", (chunk) => {
    reply += chunk;
  });
  const ended = once(socket, "end");
  socket.end(
    "POST /api/v1/datasets/upload HTTP/1.1\r\nHost: 127.0.0.1:3102\r\nHost: evil.example:3102\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
  );
  await ended;
  assert.match(reply, /^HTTP\/1\.1 403 /);
});
test("A: actual local/headerless clients and explicit same-origin uploads work", async () => {
  for (const headers of [
    {},
    { origin: "http://127.0.0.1:3102" },
    { origin: "http://127.0.0.1:3102", "sec-fetch-site": "same-origin" },
    { host: "localhost:3102", origin: "http://localhost:3102" },
  ]) {
    const { req, response } = send(
      "valid-authority",
      "/api/v1/datasets/upload",
      headers,
    );
    req.end();
    assert.equal((await response).status, 201);
  }
  assert.equal((await fetch("http://127.0.0.1:3102/health")).status, 200);
});
test("C: actual socket disconnect during request upload reaches upstream", async () => {
  const { req, response } = send("disconnect-upload");
  response.catch(() => {});
  req.write("partial upload");
  for (
    let i = 0;
    i < 100 && !observations.get("disconnect-upload")?.firstAt;
    i++
  )
    await delay(10);
  assert.ok(observations.get("disconnect-upload")?.firstAt);
  req.destroy();
  for (
    let i = 0;
    i < 100 && !observations.get("disconnect-upload")?.requestClosed;
    i++
  )
    await delay(10);
  assert.equal(observations.get("disconnect-upload").requestClosed, true);
  assert.equal(observations.get("disconnect-upload").ended, false);
});
test("C: paused real response reader backpressures upstream and disconnect closes it", async () => {
  const req = http.request("http://127.0.0.1:3102/api/v1/datasets/ds_stream", {
    headers: { "x-test-scenario": "disconnect-response" },
  });
  req.on("error", () => {});
  const ready = once(req, "response");
  req.end();
  const [res] = await ready;
  res.on("error", () => {});
  res.pause();
  await delay(150);
  assert.equal(
    observations.get("disconnect-response").responseEnded,
    undefined,
  );
  res.destroy();
  for (
    let i = 0;
    i < 100 && !observations.get("disconnect-response")?.responseClosed;
    i++
  )
    await delay(10);
  assert.equal(observations.get("disconnect-response").responseClosed, true);
});
test(
  "actual proxy enforces its 120-second upstream deadline with safe 504",
  { timeout: PROXY_TIMEOUT_MS + 20_000 },
  async () => {
    const start = Date.now();
    const { req, response } = send("timeout");
    req.end("transport-only");
    const result = await response;
    assert.equal(result.status, 504);
    assert.equal(result.body, "Gateway Timeout");
    assert.ok(Date.now() - start >= PROXY_TIMEOUT_MS - 1000);
    assert.ok(Date.now() - start < PROXY_TIMEOUT_MS + 10_000);
    assert.match(result.headers["cache-control"], /no-store/);
  },
);
