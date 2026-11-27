import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { cleanupPortFile, writePortFile } from "../config/proxy.mjs";

function scratch() {
  return mkdtempSync(join(tmpdir(), "dil-port-"));
}

test("writePortFile announces ASCII digits only and leaves no temp file", () => {
  const dir = scratch();
  try {
    const file = join(dir, "frontend.port");
    writePortFile(file, 51234);
    assert.equal(readFileSync(file, "ascii"), "51234");
    assert.deepEqual(readdirSync(dir), ["frontend.port"]);
    cleanupPortFile(file);
    assert.equal(existsSync(file), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("writePortFile rejects invalid ports and paths", () => {
  const dir = scratch();
  try {
    for (const port of [0, -1, 65536, 1.5, "3000", NaN]) {
      assert.throws(() => writePortFile(join(dir, "p"), port));
    }
    assert.throws(() => writePortFile("", 3000));
    assert.deepEqual(readdirSync(dir), []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("cleanupPortFile never deletes a file it did not write", () => {
  const dir = scratch();
  try {
    const foreign = join(dir, "foreign.port");
    writeFileSync(foreign, "40000");
    cleanupPortFile(foreign);
    assert.equal(readFileSync(foreign, "ascii"), "40000");

    // A newer invocation replaced our announcement: keep theirs.
    const shared = join(dir, "shared.port");
    writePortFile(shared, 41000);
    writeFileSync(shared, "42000");
    cleanupPortFile(shared);
    assert.equal(readFileSync(shared, "ascii"), "42000");

    cleanupPortFile(null);
    cleanupPortFile(join(dir, "missing.port"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test(
  "server.mjs --port 0 announces its port, serves it, and exits 0 on stdin EOF",
  { skip: !existsSync(new URL("../.next/BUILD_ID", import.meta.url)) && "needs npm run build", timeout: 90_000 },
  async () => {
    const dir = scratch();
    const portFile = join(dir, "frontend.port");
    const child = spawn(
      process.execPath,
      ["scripts/server.mjs", "--port", "0", "--port-file", portFile],
      {
        cwd: new URL("..", import.meta.url),
        env: { ...process.env, DIL_BACKEND_ORIGIN: "http://127.0.0.1:9" },
        stdio: ["pipe", "ignore", "pipe"],
      },
    );
    let stderr = "";
    child.stderr.on("data", (chunk) => (stderr += chunk));
    const exited = new Promise((resolve) => child.on("exit", (code) => resolve(code)));
    try {
      let port = null;
      for (let i = 0; i < 600 && port === null; i += 1) {
        if (existsSync(portFile)) port = readFileSync(portFile, "ascii");
        else await delay(100);
      }
      assert.match(port ?? "", /^[1-9]\d{0,4}$/, `no port announced; stderr: ${stderr}`);
      const res = await fetch(`http://127.0.0.1:${port}/`);
      assert.equal(res.status, 200);
      await res.arrayBuffer();

      child.stdin.end();
      const code = await Promise.race([exited, delay(15_000, "timeout")]);
      assert.equal(code, 0, `exit ${code}; stderr: ${stderr}`);
      assert.equal(existsSync(portFile), false, "port file must be removed on shutdown");
    } finally {
      if (child.exitCode === null) child.kill();
      rmSync(dir, { recursive: true, force: true });
    }
  },
);
