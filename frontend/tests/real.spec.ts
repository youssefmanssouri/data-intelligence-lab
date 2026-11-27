import { test, expect } from "@playwright/test";
import http from "node:http";
import { once } from "node:events";

test("real CSV → production proxy → backend → dashboard, then refresh without reupload", async ({
  page,
  request,
}) => {
  let uploads = 0;
  const pageErrors: string[] = [];
  const externalRequests: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.name));
  page.on("request", (req) => {
    if (!req.url().startsWith("http://127.0.0.1:3101/"))
      externalRequests.push(new URL(req.url()).origin);
    if (req.method() === "POST" && req.url().includes("/upload")) uploads++;
  });
  await expect((await request.get("/health")).status()).toBe(200);
  expect(await (await request.get("/ready")).json()).toEqual({
    status: "ready",
  });
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles({
    name: "real.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "amount,flag,label\n9223372036854775807,false,PRIVATE_CELL_a\n9223372036854775806,false,PRIVATE_CELL_b\n",
    ),
  });
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page).toHaveURL(/\/datasets\/ds_[a-f0-9]{16}$/);
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  await page.locator("summary").filter({ hasText: "amount" }).click();
  await expect(
    page.getByText("9223372036854775807", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("body")).not.toContainText(
    /PRIVATE_CELL_a|PRIVATE_CELL_b/,
  );
  const url = page.url();
  const response = await request.get(
    `/api/v1/datasets/${url.split("/").at(-1)}`,
  );
  expect(response.headers()["cache-control"]).toContain("no-store");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  expect(uploads).toBe(1);
  expect(pageErrors).toEqual([]);
  expect(externalRequests).toEqual([]);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});
test("real header-only and all-missing profiles", async ({ page }) => {
  for (const [name, contents, message] of [
    ["header.csv", "a,b\n", "not evidence of data quality"],
    ["missing.csv", "a,b\n,\n,\n", "All values are missing."],
  ]) {
    await page.goto("/");
    await page.locator("input[type=file]").setInputFiles({
      name,
      mimeType: "text/csv",
      buffer: Buffer.from(contents),
    });
    await page.getByRole("button", { name: "Create data profile" }).click();
    await expect(
      page.getByRole("heading", { name: "Data Health Dashboard" }),
    ).toBeVisible();
    await page.locator("summary").first().click();
    await expect(
      page.getByText(message, { exact: false }).first(),
    ).toBeVisible();
  }
});
test("real negative-zero numeric profile", async ({ page }) => {
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles({
    name: "zero.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("reading\n-0.0\n1.0\n"),
  });
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  await page.locator("summary").click();
  await expect(page.getByText("-0.0", { exact: true })).toBeVisible();
});
test("real API errors through frontend: 400, 404, 422", async ({ request }) => {
  const empty = await request.post("/api/v1/datasets/upload", {
    multipart: {
      file: {
        name: "empty.csv",
        mimeType: "text/csv",
        buffer: Buffer.alloc(0),
      },
    },
  });
  expect(empty.status()).toBe(400);
  expect((await empty.json()).code).toBe("ERR_EMPTY_DATASET");
  const malformed = await request.post("/api/v1/datasets/upload", {
    multipart: {
      file: {
        name: "bad.csv",
        mimeType: "text/csv",
        buffer: Buffer.from('a,b\n1,"open\n'),
      },
    },
  });
  expect(malformed.status()).toBe(422);
  expect((await malformed.json()).code).toBe("ERR_MALFORMED_CSV");
  const missing = await request.get("/api/v1/datasets/ds_missing");
  expect(missing.status()).toBe(404);
});

test("real proxy rejects declared oversized body before client sends CSV bytes", async () => {
  const req = http.request("http://127.0.0.1:3101/api/v1/datasets/upload", {
    method: "POST",
    headers: {
      "Content-Type": "multipart/form-data; boundary=dil-size",
      "Content-Length": String(200 * 1024 * 1024 + 1),
    },
  });
  const responsePromise = once(req, "response");
  req.on("error", () => {});
  req.flushHeaders();
  const [res] = await responsePromise;
  let body = "";
  for await (const chunk of res) body += chunk;
  expect(res.statusCode).toBe(413);
  expect(JSON.parse(body).code).toBe("ERR_FILE_SIZE_LIMIT_EXCEEDED");
  expect(JSON.parse(body).detail).toContain(
    "configured limit (209,715,200 bytes)",
  );
  expect(req.writableEnded).toBe(false);
  req.destroy();
});

test("real proxy enforces default 200 MiB cap during chunked multipart before client EOF", async () => {
  test.setTimeout(60_000);
  const req = http.request("http://127.0.0.1:3101/api/v1/datasets/upload", {
    method: "POST",
    headers: { "Content-Type": "multipart/form-data; boundary=dil-size" },
  });
  let status = 0;
  let body = "";
  const received = new Promise<void>((resolve, reject) => {
    req.on("response", (res) => {
      status = res.statusCode ?? 0;
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", resolve);
    });
    req.on("error", (error) => {
      if (!status) reject(error);
    });
  });
  req.write(
    '--dil-size\r\nContent-Disposition: form-data; name="file"; filename="oversized.csv"\r\nContent-Type: text/csv\r\n\r\n',
  );
  const chunk = Buffer.alloc(1024 * 1024, "x");
  for (let i = 0; i < 201 && !status; i++) {
    if (!req.write(chunk))
      await Promise.race([once(req, "drain").catch(() => {}), received]);
  }
  await received;
  expect(status).toBe(413);
  expect(JSON.parse(body).code).toBe("ERR_FILE_SIZE_LIMIT_EXCEEDED");
  expect(JSON.parse(body).detail).toContain(
    "configured limit (209,715,200 bytes)",
  );
  expect(req.writableEnded).toBe(false);
  req.destroy();
});
test("real insights: rule-based findings come from the local backend", async ({
  page,
}, testInfo) => {
  const rows = ["order_id,region,amount,discount,order_date"];
  for (let i = 0; i < 60; i++) {
    const region = i % 3 ? "North" : "South";
    const amount = i === 59 ? 9000 : (region === "North" ? 120 : 35) + (i % 5);
    const discount = i % 2 ? "" : String(amount / 10);
    const day = i < 30 ? i + 1 : i + 40;
    const date = new Date(Date.UTC(2024, 0, day)).toISOString().slice(0, 10);
    rows.push(`${i + 1},${region},${amount},${discount},${date}`);
  }
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles({
    name: "sales.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(rows.join("\n") + "\n"),
  });
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page).toHaveURL(/\/datasets\/ds_[a-f0-9]{16}$/);
  const panel = page.locator('section[aria-labelledby="insights-title"]');
  await expect(panel.locator("li").first()).toBeVisible();
  await expect(panel).toContainText("'discount' is missing 50% of its values");
  await panel.getByRole("button", { name: /Show all \d+ insights/ }).click();
  await expect(panel).toContainText("'order_id' is unique in every row");
  await expect(panel).toContainText("'order_date' has a gap of");
  await expect(panel).toContainText(
    "Typical 'amount' is 3.3x higher for 'North' (median 122)",
  );
  await page.setViewportSize({ width: 1280, height: 900 });
  await panel.screenshot({ path: testInfo.outputPath("insights-panel.png") });
  const first = await panel.locator("li").allInnerTexts();
  await page.reload();
  await panel.getByRole("button", { name: /Show all \d+ insights/ }).click();
  // Same file, same rules: the reloaded findings are identical.
  expect(await panel.locator("li").allInnerTexts()).toEqual(first);
});
