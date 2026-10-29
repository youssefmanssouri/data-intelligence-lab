import { test } from "node:test";
import assert from "node:assert/strict";
import { PassThrough, Writable } from "node:stream";
import { setImmediate as tick } from "node:timers/promises";
import { inboundPolicy, API_HEADERS } from "../config/proxy.mjs";
import { createApiProxy } from "../scripts/api-proxy.mjs";

function inbound(headers = {}, method = "POST", rawHeaders) {
  return {
    method,
    url: "/api/v1/datasets/upload",
    headers: { host: "127.0.0.1:3000", ...headers },
    rawHeaders,
  };
}
test("A: accept only explicit configured local authorities", () => {
  const accepts = inboundPolicy(3000);
  for (const host of [
    "attacker.example:3000",
    "127.0.0.1",
    "127.0.0.1:3001",
    "127.1:3000",
    "127.0.0.1:03000",
    "127.0.0.1:3000 ",
    "user@127.0.0.1:3000",
    "127.0.0.1:3000,evil",
    "http://127.0.0.1:3000",
    "[::1]:3000",
    "",
    undefined,
  ]) {
    assert.equal(accepts(inbound({ host })), false, String(host));
  }
  assert.equal(accepts(inbound()), true);
  assert.equal(accepts(inbound({ host: "localhost:3000" })), true);
  assert.equal(
    accepts(
      inbound({}, "POST", ["Host", "127.0.0.1:3000", "host", "127.0.0.1:3000"]),
    ),
    false,
  );
});
test("A: pin browser origin independently and reject foreign/null/ambiguous inputs", () => {
  const accepts = inboundPolicy(3000);
  for (const origin of [
    "https://evil.example",
    "null",
    "http://localhost:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3000/",
    "http://127.0.0.1:3000, http://evil.example",
    "",
    null,
  ]) {
    assert.equal(accepts(inbound({ origin })), false, String(origin));
  }
  assert.equal(
    accepts(
      inbound({
        origin: "http://127.0.0.1:3000",
        "sec-fetch-site": "same-origin",
      }),
    ),
    true,
  );
  assert.equal(
    accepts(
      inbound({ host: "localhost:3000", origin: "http://localhost:3000" }),
    ),
    true,
  );
  assert.equal(accepts(inbound({ origin: "http://127.0.0.1:3000" })), true);
  assert.equal(
    accepts(
      inbound({ origin: "http://127.0.0.1:3000" }, "POST", [
        "Origin",
        "http://127.0.0.1:3000",
        "Origin",
        "http://127.0.0.1:3000",
      ]),
    ),
    false,
  );
});
test("A: define missing optional headers without accepting cross-site browser hints", () => {
  const accepts = inboundPolicy(3000);
  for (const site of ["cross-site", "same-site", "unknown", "", null]) {
    assert.equal(
      accepts(
        inbound({ origin: "http://127.0.0.1:3000", "sec-fetch-site": site }),
      ),
      false,
    );
  }
  assert.equal(accepts(inbound({ "sec-fetch-site": "same-origin" })), false);
  assert.equal(
    accepts(
      inbound({ origin: "http://127.0.0.1:3000", "sec-fetch-site": "none" }),
    ),
    true,
  );
  assert.equal(accepts(inbound()), true); // Headerless CLI upload.
  assert.equal(
    accepts({
      method: "GET",
      url: "/health",
      headers: { host: "127.0.0.1:3000" },
    }),
    true,
  );
});

class ClientRequest extends Writable {
  constructor() {
    super({ autoDestroy: false });
  }
  _write(chunk, encoding, callback) {
    callback();
  }
  flushHeaders() {}
}
class ClientResponse extends Writable {
  constructor(slow = false) {
    super({ highWaterMark: 4 });
    this.slow = slow;
    this.pending = [];
    this.body = [];
    this.headersSent = false;
  }
  writeHead(status, headers) {
    this.statusCode = status;
    this.headers = headers;
    this.headersSent = true;
    return this;
  }
  _write(chunk, encoding, callback) {
    this.body.push(chunk);
    if (this.slow) this.pending.push(callback);
    else callback();
  }
  release() {
    this.slow = false;
    for (const callback of this.pending.splice(0)) callback();
  }
}
function fixture({ slow = false, complete = true } = {}) {
  const req = new PassThrough();
  Object.assign(req, {
    complete,
    method: "GET",
    url: "/api/v1/datasets/ds_test",
    headers: {},
  });
  const res = new ClientResponse(slow);
  const upstream = new ClientRequest();
  let expire;
  let cancelled = 0;
  let destination;
  const proxy = createApiProxy({
    origin: new URL("http://127.0.0.1:8000"),
    request: (options) => {
      destination = options;
      return upstream;
    },
    schedule: (callback) => {
      expire = callback;
      return 7;
    },
    cancelSchedule: (token) => {
      assert.equal(token, 7);
      cancelled++;
    },
  });
  proxy(req, res);
  if (complete) req.end();
  function respond(
    status = 200,
    headers = { "content-type": "application/json" },
  ) {
    const response = new PassThrough();
    Object.assign(response, { statusCode: status, headers });
    upstream.emit("response", response);
    return response;
  }
  return {
    req,
    res,
    upstream,
    respond,
    expire: () => expire(),
    cancelled: () => cancelled,
    destination: () => destination,
  };
}

test("C: upstream end leaves deadline active until downstream finish", async () => {
  const f = fixture({ slow: true });
  const response = f.respond();
  response.end('{"ok":true}');
  await tick();
  assert.equal(response.readableEnded, true);
  assert.equal(f.res.writableFinished, false);
  assert.equal(f.cancelled(), 0);
  f.res.release();
  await tick();
  assert.equal(f.res.writableFinished, true);
  assert.equal(f.res.destroyed, true); // Writable's normal autoDestroy after finish.
  assert.equal(f.cancelled(), 1);
  f.expire();
  assert.equal(f.cancelled(), 1);
  for (const stream of [f.req, f.res, f.upstream, response])
    assert.equal(stream.listenerCount("error"), 0);
  assert.equal(f.res.listenerCount("finish"), 0);
});
test("C: deadline during slow downstream delivery closes response exactly once", async () => {
  const f = fixture({ slow: true });
  const response = f.respond();
  response.end('{"ok":true}');
  await tick();
  f.expire();
  f.expire();
  await tick();
  assert.equal(f.res.destroyed, true);
  assert.equal(f.cancelled(), 1);
  assert.equal(f.upstream.destroyed, true);
  assert.equal(response.destroyed, true);
});
test("C: request upload abort cancels upstream and downstream", async () => {
  const f = fixture({ complete: false });
  f.req.write("partial");
  f.req.emit("aborted");
  await tick();
  assert.equal(f.upstream.destroyed, true);
  assert.equal(f.res.destroyed, true);
  assert.equal(f.req.destroyed, true);
  assert.equal(f.cancelled(), 1);
  assert.equal(f.req.listenerCount("aborted"), 0);
});
test("C: client response disconnect cancels a streaming upstream", async () => {
  const f = fixture({ slow: true });
  const response = f.respond();
  response.write('{"pending":');
  await tick();
  f.res.destroy();
  await tick();
  assert.equal(response.destroyed, true);
  assert.equal(f.upstream.destroyed, true);
  assert.equal(f.cancelled(), 1);
});
test("C: pre-header deadline produces controlled 504 and releases timer", async () => {
  const f = fixture();
  f.expire();
  await tick();
  assert.equal(f.res.statusCode, 504);
  assert.equal(Buffer.concat(f.res.body).toString(), "Gateway Timeout");
  assert.equal(f.cancelled(), 1);
  assert.deepEqual(
    Object.fromEntries(
      Object.keys(API_HEADERS).map((key) => [key, f.res.headers[key]]),
    ),
    API_HEADERS,
  );
});
test("C: premature upstream close fails safely", async () => {
  const f = fixture();
  const response = f.respond();
  response.destroy();
  await tick();
  assert.equal(f.res.destroyed, true);
  assert.equal(f.cancelled(), 1);
});

for (const [label, status, headers] of [
  ["HTML", 200, { "content-type": "text/html" }],
  [
    "redirect",
    302,
    { "content-type": "application/json", location: "https://evil.example" },
  ],
  ["missing type", 200, {}],
  ["unexpected type", 200, { "content-type": "application/jsonp" }],
  ["unexpected success status", 204, { "content-type": "application/json" }],
  ["non-problem error", 500, { "content-type": "text/html" }],
  [
    "unsupported encoding",
    200,
    { "content-type": "application/json", "content-encoding": "gzip" },
  ],
]) {
  test(`D: reject upstream ${label} without reflecting its body or headers`, async () => {
    const f = fixture();
    const response = f.respond(status, headers);
    await tick();
    assert.equal(f.res.statusCode, 502);
    assert.equal(Buffer.concat(f.res.body).toString(), "Bad Gateway");
    assert.equal(f.res.headers.location, undefined);
    assert.equal(response.destroyed, true);
    for (const [key, value] of Object.entries(API_HEADERS))
      assert.equal(f.res.headers[key], value);
  });
}
test("D: allow only canonical JSON type and frontend-controlled response headers", async () => {
  const f = fixture();
  const response = f.respond(200, {
    "content-type": "application/json; charset=utf-8",
    "set-cookie": ["evil=1"],
    location: "https://evil.example",
    "referrer-policy": "unsafe-url",
    "x-frame-options": "ALLOWALL",
    "content-security-policy": "default-src *",
    "cache-control": "public",
    "content-length": "1",
  });
  response.end('{"ok":true}');
  await tick();
  assert.equal(f.res.statusCode, 200);
  assert.equal(f.res.headers["set-cookie"], undefined);
  assert.equal(f.res.headers.location, undefined);
  assert.equal(f.res.headers["content-length"], undefined);
  for (const [key, value] of Object.entries(API_HEADERS))
    assert.equal(f.res.headers[key], value);
  assert.equal(f.destination().hostname, "127.0.0.1");
  assert.equal(f.destination().headers["accept-encoding"], "identity");
});
test("D: preserve backend problem JSON and status without interpreting or logging details", async () => {
  const f = fixture();
  const response = f.respond(413, {
    "content-type": "application/problem+json",
  });
  const body = '{"status":413,"code":"ERR_FILE_SIZE_LIMIT_EXCEEDED"}';
  response.end(body);
  await tick();
  assert.equal(f.res.statusCode, 413);
  assert.equal(Buffer.concat(f.res.body).toString(), body);
});
test("D: malformed claimed JSON is inert JSON; client remains responsible for parsing", async () => {
  const f = fixture();
  const response = f.respond();
  response.end("<script>bad()</script>");
  await tick();
  assert.equal(
    f.res.headers["content-type"],
    "application/json; charset=utf-8",
  );
  assert.match(f.res.headers["content-security-policy"], /sandbox/);
  assert.throws(() => JSON.parse(Buffer.concat(f.res.body).toString()));
});
