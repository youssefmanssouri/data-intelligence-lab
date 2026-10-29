// Response bytes, not CSV upload bytes. This is a UI budget, not a process RAM ceiling.
export const MAX_METADATA_RESPONSE_BYTES = 8 * 1024 * 1024;
export class ResponseBudgetError extends Error {}

export async function readJsonBody(
  response,
  maxBytes = MAX_METADATA_RESPONSE_BYTES,
) {
  if (!response.body) throw new Error("Missing JSON response stream");
  const reader = response.body.getReader();
  const declared = response.headers.get("content-length");
  try {
    if (
      declared &&
      /^\d+$/.test(declared) &&
      BigInt(declared) > BigInt(maxBytes)
    ) {
      throw new ResponseBudgetError();
    }
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const parts = [];
    let received = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > maxBytes) throw new ResponseBudgetError();
      parts.push(decoder.decode(value, { stream: true }));
    }
    parts.push(decoder.decode());
    return parts.join("");
  } catch (error) {
    try {
      await reader.cancel();
    } catch {
      /* Cancellation is best effort. */
    }
    throw error;
  } finally {
    reader.releaseLock();
  }
}
