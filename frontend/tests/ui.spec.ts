import { test, expect, type Page } from "@playwright/test";
import { column, id, metadata, summary } from "./fixture";

const path = `/datasets/${id}`;
const api = `/api/v1/datasets/${id}`;
const csv = {
  name: "research.csv",
  mimeType: "text/csv",
  buffer: Buffer.from("amount\n1\n2\n"),
};
async function mockMetadata(page: Page, body = JSON.stringify(metadata())) {
  await page.route(`**${api}`, (route) =>
    route.fulfill({ contentType: "application/json", body }),
  );
}
test("successful upload uses file multipart, navigates and refreshes with GET only", async ({
  page,
}) => {
  let uploads = 0,
    reads = 0;
  await page.route("**/api/v1/datasets/upload", async (route) => {
    uploads++;
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["content-type"]).toContain(
      "multipart/form-data; boundary=",
    );
    expect(route.request().postDataBuffer()?.toString()).toContain(
      'name="file"',
    );
    await route.fulfill({ status: 201, json: summary });
  });
  await page.route(`**${api}`, (route) => {
    reads++;
    return route.fulfill({ json: metadata() });
  });
  await page.goto("/");
  await page.getByLabel("Bring your data into focus.").setInputFiles(csv);
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page).toHaveURL(new RegExp(path));
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  expect(uploads).toBe(1);
  expect(reads).toBe(2);
  await page.getByRole("button", { name: "Refresh metadata" }).click();
  await expect.poll(() => reads).toBe(3);
});
test("invalid, empty and multiple file selections never upload", async ({
  page,
}) => {
  let uploads = 0;
  await page.route("**/api/**", (route) => {
    uploads++;
    return route.abort();
  });
  await page.goto("/");
  const input = page.locator("input[type=file]");
  await input.setInputFiles({
    name: "image.png",
    mimeType: "image/png",
    buffer: Buffer.from("x"),
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    ".csv extension",
  );
  await expect(
    page.getByRole("button", { name: "Create data profile" }),
  ).toBeDisabled();
  await input.setInputFiles({
    name: "empty.csv",
    mimeType: "text/csv",
    buffer: Buffer.alloc(0),
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "file is empty",
  );
  await input.setInputFiles([]);
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Choose a CSV file first",
  );
  await page.locator('label[for="csv-file"]').evaluate((label) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(
      new File(["a\n1\n"], "one.csv", { type: "text/csv" }),
    );
    dataTransfer.items.add(
      new File(["b\n2\n"], "two.csv", { type: "text/csv" }),
    );
    label.dispatchEvent(new DragEvent("drop", { bubbles: true, dataTransfer }));
  });
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "one CSV file at a time",
  );
  expect(uploads).toBe(0);
});
test("loading feedback and immediate duplicate submit lock", async ({
  page,
}) => {
  let uploads = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/datasets/upload", async (route) => {
    uploads++;
    await gate;
    await route.fulfill({ status: 500, body: "" });
  });
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles(csv);
  await page.locator("form").evaluate((form) => {
    (form as HTMLFormElement).requestSubmit();
    (form as HTMLFormElement).requestSubmit();
  });
  await expect(
    page.getByRole("button", { name: "Uploading & profiling" }),
  ).toBeDisabled();
  await expect(page.getByRole("status")).toContainText(
    "Uploading and analysing",
  );
  await expect(page.locator("input[type=file]")).toBeDisabled();
  expect(uploads).toBe(1);
  release();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "local service could not complete",
  );
  await expect(
    page.getByRole("button", { name: "Create data profile" }),
  ).toBeEnabled();
});
for (const [status, fragment] of [
  [400, "could not be accepted"],
  [404, "Dataset not found"],
  [413, "size limit"],
  [422, "processed safely"],
  [500, "could not complete"],
  [504, "timed out"],
] as const) {
  for (const screen of ["upload", "dashboard"] as const) {
    test(`${screen} handles HTTP ${status} without exposing diagnostics`, async ({
      page,
    }) => {
      await page.route("**/api/**", (route) =>
        route.fulfill({
          status,
          contentType: "application/problem+json",
          body: JSON.stringify({
            type: "about:blank",
            title: "sensitive",
            detail: "C:\\private\\secret.csv TRACEBACK private-value",
            instance: api,
            code: "ERR_TEST",
            status,
          }),
        }),
      );
      if (screen === "upload") {
        await page.goto("/");
        await page.locator("input[type=file]").setInputFiles(csv);
        await page.getByRole("button", { name: "Create data profile" }).click();
      } else await page.goto(path);
      await expect(page.getByRole("main").getByRole("alert")).toContainText(
        fragment,
      );
      await expect(page.locator("body")).not.toContainText(
        /TRACEBACK|private-value|secret.csv/,
      );
    });
  }
}
test("network failure and metadata recovery", async ({ page }) => {
  await page.route("**/api/**", (route) => route.abort("connectionrefused"));
  await page.goto(path);
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Cannot reach",
  );
  await page.unroute("**/api/**");
  await mockMetadata(page);
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
});
test("upload network error permits deliberate retry", async ({ page }) => {
  await page.route("**/api/**", (route) => route.abort("connectionrefused"));
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles(csv);
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "Cannot reach",
  );
  await expect(
    page.getByRole("button", { name: "Create data profile" }),
  ).toBeEnabled();
});
for (const response of [
  { contentType: "text/html", body: "<html>C:/private/traceback</html>" },
  { contentType: "application/json", body: '{"broken":' },
  { contentType: "application/json", body: '{"dataset_id":"../escape"}' },
  {
    contentType: "application/json",
    body: JSON.stringify({ ...metadata(), quality: null }),
  },
]) {
  test(`rejects invalid metadata ${response.body.slice(0, 26)}`, async ({
    page,
  }) => {
    await page.route("**/api/**", (route) => route.fulfill(response));
    await page.goto(path);
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      "invalid response",
    );
    await expect(page.locator("body")).not.toContainText("private/traceback");
  });
}
test("invalid upload summary does not navigate", async ({ page }) => {
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 201, json: { dataset_id: "https://bad.example" } }),
  );
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles(csv);
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "invalid response",
  );
  await expect(page).toHaveURL(/\/$/);
});
for (const response of [
  { contentType: "text/html", body: "<html>private diagnostic</html>" },
  { contentType: "application/json", body: '{"broken":' },
]) {
  test(`invalid upload response ${response.contentType}`, async ({ page }) => {
    await page.route("**/api/**", (route) =>
      route.fulfill({ status: 201, ...response }),
    );
    await page.goto("/");
    await page.locator("input[type=file]").setInputFiles(csv);
    await page.getByRole("button", { name: "Create data profile" }).click();
    await expect(page.getByRole("main").getByRole("alert")).toContainText(
      "invalid response",
    );
    await expect(page.locator("body")).not.toContainText("private diagnostic");
  });
}
test("metadata loading is accessible", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/**", async (route) => {
    await gate;
    await route.fulfill({ json: metadata() });
  });
  await page.goto(path);
  await expect(page.getByRole("status")).toContainText(
    "Retrieving your profile",
  );
  release();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
});
test("client timeout is recoverable and never automatically reuploads", async ({
  page,
}) => {
  await page.clock.install();
  await page.route("**/api/**", () => {});
  await page.goto("/");
  await page.locator("input[type=file]").setInputFiles(csv);
  await page.getByRole("button", { name: "Create data profile" }).click();
  await expect(page.getByRole("status")).toBeVisible();
  await page.clock.fastForward(120_001);
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "may still have been saved",
  );
});
test("warnings are deduplicated and scores are explained", async ({ page }) => {
  const d = metadata();
  d.warnings = ["Column is constant.", "Check missingness."];
  d.quality.warnings = ["Column is constant."];
  await mockMetadata(page, JSON.stringify(d));
  await page.goto(path);
  await expect(
    page.getByText("Column is constant.", { exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByText("Check missingness.", { exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByText(/does not measure semantic validity/),
  ).toBeVisible();
});
test("many warnings are progressively revealed as safe text with keyboard controls", async ({
  page,
}) => {
  const d = metadata();
  d.quality.warnings = Array.from({ length: 101 }, (_, i) => `Note ${i + 1}`);
  d.quality.warnings[100] = '<img src=x onerror="alert(1)">';
  d.warnings = ["Note 1", ...d.quality.warnings];
  await mockMetadata(page, JSON.stringify(d));
  await page.goto(path);
  const section = page.locator('section[aria-labelledby="warnings-title"]');
  const items = section.locator("li");
  await expect(items).toHaveCount(20);
  await expect(section.getByRole("status")).toHaveText(
    "Showing 20 of 101 notes.",
  );
  for (let i = 0; i < 5; i++) {
    await section.getByRole("button", { name: "Show more notes" }).focus();
    await page.keyboard.press("Enter");
  }
  await expect(items).toHaveCount(101);
  await expect(items.last()).toHaveText('<img src=x onerror="alert(1)">');
  await expect(section.locator("img")).toHaveCount(0);
  await section.getByRole("button", { name: "Show less notes" }).focus();
  await page.keyboard.press("Enter");
  await expect(items).toHaveCount(20);
});
test("header-only dataset qualifies its score and empty statistics", async ({
  page,
}) => {
  const d = metadata();
  d.row_count = 0;
  d.quality.total_cells = 0;
  d.warnings = ["Dataset has 0 rows of data."];
  d.columns = [
    {
      ...column("empty", "string", {}),
      total_count: 0,
      distinct_count: 0,
      is_unique: false,
      inference_reason: "ALL_NULL",
    },
  ];
  await mockMetadata(page, JSON.stringify(d));
  await page.goto(path);
  await expect(page.getByText(/not evidence of data quality/)).toBeVisible();
  await page.locator("summary").click();
  await expect(
    page.getByText("No observations.", { exact: false }),
  ).toBeVisible();
});
test("all-missing column does not invent statistics", async ({ page }) => {
  const d = metadata();
  d.columns = [
    {
      ...column("empty", "string", {}),
      null_count: 2,
      null_ratio: 1,
      distinct_count: 0,
      is_unique: false,
    },
  ];
  await mockMetadata(page, JSON.stringify(d));
  await page.goto(path);
  await page.locator("summary").click();
  await expect(
    page.getByText("All values are missing.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("false", { exact: true })).toBeVisible();
});
test("type-specific stats preserve large integers, -0, zero, false and missing", async ({
  page,
}) => {
  const d = metadata();
  d.column_count = 6;
  d.columns = [
    column("large"),
    column("decimal", "float", { min: "NEG_ZERO", max: 0, mean: null }),
    column("text", "string", {
      min_length: 0,
      max_length: 18,
      avg_length: 9,
      empty_string_count: 0,
    }),
    column("flag", "boolean", { true_count: 0, false_count: 2 }),
    column("day", "date", { min_date: "2026-01-01", max_date: "2026-10-29" }),
    column("time", "datetime", {
      min_date: "2026-10-29 10:00:00+00:00",
      max_date: "2026-10-29 11:00:00+00:00",
    }),
  ];
  d.columns[0].stats = { min: "BIG_INTEGER", max: "BIG_INTEGER" };
  d.columns[0].is_unique = false;
  const body = JSON.stringify(d)
    .replaceAll('"BIG_INTEGER"', "9223372036854775807")
    .replace('"NEG_ZERO"', "-0.0");
  await mockMetadata(page, body);
  await page.goto(path);
  await expect(page.locator("summary")).toHaveCount(6);
  for (const el of await page.locator("summary").all()) await el.click();
  await expect(
    page.getByText("9223372036854775807", { exact: true }),
  ).toHaveCount(2);
  await expect(page.getByText("-0.0", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Not available", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("false", { exact: true })).toBeVisible();
  await expect(page.getByText("False values", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Shortest length", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("2026-01-01", { exact: true })).toBeVisible();
  await expect(
    page.getByText("2026-10-29 10:00:00+00:00", { exact: true }),
  ).toBeVisible();
});
test("rows, samples, arbitrary stats and raw CSV never enter rendered DOM", async ({
  page,
}) => {
  const d = metadata();
  const secret = "PRIVATE_ROW_VALUE_75cfe";
  const body = JSON.stringify({
    ...d,
    records: [{ content: secret }],
    raw_csv: secret,
    columns: [
      {
        ...d.columns[0],
        sample_values: [secret],
        stats: { ...d.columns[0].stats, sample: secret },
      },
    ],
  });
  await mockMetadata(page, body);
  await page.goto(path);
  await page.locator("summary").click();
  await expect(page.locator("html")).not.toContainText(secret);
});
test("dataset IDs are validated before requests and metadata uses no-cache fetch", async ({
  page,
}) => {
  let calls = 0;
  await page.route("**/api/**", (route) => {
    calls++;
    return route.fulfill({ json: metadata() });
  });
  await page.goto("/datasets/CON");
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "link is invalid",
  );
  expect(calls).toBe(0);
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = (input, init) => {
      if (String(input).startsWith("/api/"))
        document.documentElement.dataset.fetchCache = String(init?.cache);
      return original(input, init);
    };
  });
  await page.goto(path);
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
  expect(await page.locator("html").getAttribute("data-fetch-cache")).toBe(
    "no-store",
  );
});
test("keyboard flow, focus and mobile layout", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.screenshot({
    path: testInfo.outputPath("landing-desktop.png"),
    fullPage: true,
  });
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Data Intelligence Lab home" }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("navigation", { name: "Workspace" }).getByRole("link", {
      name: "Upload CSV",
    }),
  ).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("input[type=file]")).toBeFocused();
  // The skip link bypasses the sidebar and lands keyboard users on the form.
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await expect(page.locator("input[type=file]")).toBeFocused();
  await mockMetadata(page);
  await page.goto(path);
  await page.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("details")).toHaveAttribute("open", "");
  for (const width of [375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await expect(
      page.getByRole("heading", { name: "Data Health Dashboard" }),
    ).toBeVisible();
    if (width === 375 || width === 1440)
      await page.screenshot({
        path: testInfo.outputPath(`dashboard-${width}.png`),
        fullPage: true,
      });
  }
  await page.goto("/");
  await page.setViewportSize({ width: 375, height: 900 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("landing-mobile.png"),
    fullPage: true,
  });
});

test("B: oversized metadata shows a recoverable controlled error", async ({
  page,
}) => {
  await page.route(`**${api}`, (route) =>
    route.fulfill({
      contentType: "application/json",
      body: " ".repeat(8 * 1024 * 1024 + 1),
    }),
  );
  await page.goto(path);
  await expect(page.getByRole("main").getByRole("alert")).toContainText(
    "8 MiB display limit",
  );
  await page.unroute(`**${api}`);
  await mockMetadata(page);
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(
    page.getByRole("heading", { name: "Data Health Dashboard" }),
  ).toBeVisible();
});

test("B: 1000 columns render in bounded pages with lazy statistics and clipped text", async ({
  page,
}) => {
  const d = metadata();
  d.column_count = 1000;
  d.quality.total_cells = 2000;
  d.filename = "long".repeat(1000) + ".csv";
  d.columns = Array.from({ length: 1000 }, (_, index) => ({
    ...column(index === 0 ? "a".repeat(10000) : `column_${index}`),
    index,
  }));
  d.warnings = ["Warning ".repeat(1000)];
  d.quality.warnings = [...d.warnings];
  const body = JSON.stringify(d);
  const budget = 8 * 1024 * 1024;
  // Legal trailing JSON whitespace exercises an exact-budget metadata response.
  await mockMetadata(page, body + " ".repeat(budget - Buffer.byteLength(body)));
  const start = Date.now();
  await page.goto(path);
  await expect(page.locator("details")).toHaveCount(25);
  await expect(page.getByRole("status")).toContainText("of 1000");
  await expect(page.locator("details dd")).toHaveCount(0);
  expect(
    (await page.locator("details summary span").first().innerText()).length,
  ).toBeLessThanOrEqual(241);
  expect(
    (await page.locator("main li").first().innerText()).length,
  ).toBeLessThanOrEqual(401);
  await page.locator("summary").first().click();
  await expect(page.locator("details dd")).toHaveCount(14);
  console.info(
    `Synthetic 1000-column, 8 MiB response render: ${Date.now() - start} ms, 25 profiles, 14 statistic/count cells after one expansion.`,
  );
  await page.getByRole("button", { name: "Next columns" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("status")).toContainText("Columns 26–50 of 1000");
  await expect(
    page.getByRole("heading", { name: "Column profiles" }),
  ).toBeFocused();
  await expect(page.locator("details")).toHaveCount(25);
  await expect(page.locator("details dd")).toHaveCount(0);
  await page.getByRole("button", { name: "Previous columns" }).click();
  await expect(page.getByRole("status")).toContainText("Columns 1–25 of 1000");
});
