import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAX_METADATA_RESPONSE_BYTES,
  readJsonBody,
  ResponseBudgetError,
} from "../src/services/json-body.mjs";

function streamed(chunks, length) {
  let cancelled = 0;
  let pulled = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (pulled < chunks.length) controller.enqueue(chunks[pulled++]);
      else controller.close();
    },
    cancel() {
      cancelled++;
    },
  });
  const response = new Response(stream, {
    headers: length === undefined ? {} : { "content-length": String(length) },
  });
  return { response, cancelled: () => cancelled, pulled: () => pulled };
}
const bytes = (text) => new TextEncoder().encode(text);
test("B: accept exact byte boundary with or without Content-Length", async () => {
  for (const length of [undefined, 11]) {
    const f = streamed([bytes('{"value":0}')], length);
    assert.equal(await readJsonBody(f.response, 11), '{"value":0}');
    assert.equal(f.response.body.locked, false);
  }
});
test("B: reject missing-length chunked overflow and cancel producer", async () => {
  const f = streamed([
    bytes("aaaa"),
    bytes("bbbb"),
    bytes("cccc"),
    bytes("dddd"),
  ]);
  await assert.rejects(readJsonBody(f.response, 7), ResponseBudgetError);
  assert.equal(f.cancelled(), 1);
  assert.equal(f.response.body.locked, false);
  assert.ok(f.pulled() < 4);
});
test("B: misleading small, invalid, or absent Content-Length never bypasses metering", async () => {
  for (const length of [0, 1, "invalid", undefined]) {
    const f = streamed(
      [bytes("12345"), bytes("67890"), bytes("remaining")],
      length,
    );
    await assert.rejects(readJsonBody(f.response, 9), ResponseBudgetError);
    assert.equal(f.cancelled(), 1);
  }
});
test("B: oversized declaration fails closed and cancels the reader", async () => {
  const f = streamed([bytes("small"), bytes("more")], 99);
  await assert.rejects(readJsonBody(f.response, 10), ResponseBudgetError);
  assert.equal(f.cancelled(), 1);
});
test("B: count UTF-8 bytes across chunk boundaries and reject invalid encoding", async () => {
  const encoded = bytes('"é"');
  const f = streamed([encoded.slice(0, 2), encoded.slice(2)]);
  assert.equal(await readJsonBody(f.response, 4), '"é"');
  const invalid = streamed([new Uint8Array([0xff]), bytes("more")]);
  await assert.rejects(readJsonBody(invalid.response));
  assert.equal(invalid.cancelled(), 1);
});
test("B: default 8 MiB cap accepts a near-limit normal JSON response", async () => {
  const body = JSON.stringify({
    padding: "x".repeat(MAX_METADATA_RESPONSE_BYTES - 14),
  });
  assert.equal(bytes(body).length, MAX_METADATA_RESPONSE_BYTES);
  const f = streamed([
    bytes(body.slice(0, body.length / 2)),
    bytes(body.slice(body.length / 2)),
  ]);
  assert.equal(
    (await readJsonBody(f.response)).length,
    MAX_METADATA_RESPONSE_BYTES,
  );
});
test("B: default cap rejects one excess byte without a declared length", async () => {
  const f = streamed([
    new Uint8Array(MAX_METADATA_RESPONSE_BYTES),
    bytes("x"),
    bytes("remaining"),
  ]);
  await assert.rejects(readJsonBody(f.response), ResponseBudgetError);
  assert.equal(f.cancelled(), 1);
});
