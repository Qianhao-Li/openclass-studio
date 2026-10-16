import { test, expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

async function openLocalCourses(page: Page) {
  await page.getByRole("button", { name: "OpenClass User Avatar", exact: true }).click();
  await page.getByRole("menuitem", { name: "Courses on this device", exact: true }).click();
}

async function createLocalCourse(page: Page) {
  const previous = new URL(page.url()).searchParams.get("lesson");
  const loaded = page.waitForResponse(response => {
    const selected = new URL(page.url()).searchParams.get("lesson");
    const target = new URL(response.url());
    return Boolean(selected && selected !== previous && target.pathname === "/api/course-package" && target.searchParams.get("lesson_id") === selected);
  });
  await page.getByRole("button", { name: "New course", exact: true }).click();
  await (await loaded).finished();
  await expect(page.locator(".ProseMirror").first()).toHaveAttribute("contenteditable", "true");
  await expect(page.locator(".ProseMirror").first()).toBeEmpty();
}

test("local Studio edits and reopens a course without any platform session or network", async ({ page, context, browser, baseURL }) => {
  const platformCalls: string[] = [];
  const errors: string[] = [];
  const api = process.env.OPENCLASS_STUDIO_E2E_API || "http://127.0.0.1:8510";
  await context.addCookies([{ name: "openclass.auth.token", value: "stale-platform-token", url: baseURL! }]);
  await context.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) { platformCalls.push(url.origin); return route.abort(); }
    if (/^\/api\/(?:auth|billing|account\/settings)(?:\/|$)/.test(url.pathname)) platformCalls.push(url.pathname);
    if (url.origin === api) expect(route.request().headers().authorization).toBeUndefined();
    await route.continue();
  });
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/studio");
  await openLocalCourses(page);
  await createLocalCourse(page);
  const editor = page.locator(".ProseMirror").first();
  await expect(editor).toHaveAttribute("contenteditable", "true");
  const marker = `Local course persistence ${Date.now()}`;
  const saved = page.waitForResponse(response => /\/document\/save(?:\?|$)/.test(response.url()) && response.request().method() === "POST");
  await editor.fill(marker);
  expect((await saved).ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator(".ProseMirror").first()).toContainText(marker);
  const url = page.url();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: /EXPORT COURSE PACKAGE/i }).click();
  const archive = await downloaded;
  expect(archive.suggestedFilename()).toMatch(/\.ridoc$/i);
  await expect(page.getByRole("button", { name: /Send message/i })).toBeDisabled();
  expect(platformCalls).toEqual([]);
  expect(errors).toEqual([]);
  const fresh = await browser.newContext();
  try {
    const reopened = await fresh.newPage();
    await reopened.goto(url);
    await expect(reopened.locator(".ProseMirror").first()).toContainText(marker);
    const bootstrap = await reopened.evaluate(async origin => (await fetch(origin + "/api/workbench/v1/bootstrap", { credentials: "include" })).json(), api);
    expect(bootstrap.principal.kind).toBe("local");
  } finally { await fresh.close(); }
  await page.screenshot({ path: test.info().outputPath("local-studio.png"), fullPage: true });
});

test("local originals keep exact bytes and a saved course reopens after its source is deleted", async ({ page, context }) => {
  const origin = process.env.OPENCLASS_STUDIO_E2E_API || "http://127.0.0.1:8510";
  await context.route("**/*", route => ["127.0.0.1", "localhost"].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort());
  await page.goto("/studio");
  await openLocalCourses(page);
  await createLocalCourse(page);
  const marker = `Independent saved course ${Date.now()}`;
  const saved = page.waitForResponse(response => /\/document\/save(?:\?|$)/.test(response.url()) && response.request().method() === "POST");
  await page.locator(".ProseMirror").first().fill(marker);
  expect((await saved).ok()).toBeTruthy();
  const original = "# Original heading\nExact original bytes, spacing, and source content.\n";
  const record = await page.evaluate(async ({ origin, original }) => {
    const course = await (await fetch(origin + "/api/course-package", { credentials: "include" })).json();
    const body = new FormData(); body.set("file", new File([original], "original.md", { type: "text/markdown" }));
    const response = await fetch(`${origin}/api/packages/${course.id}/sources`, { method: "POST", credentials: "include", body });
    if (!response.ok) throw new Error(await response.text());
    return { packageId: course.id, source: await response.json() };
  }, { origin, original });
  expect(record.source.metadata.source_backend).toBe("local");
  await expect.poll(async () => page.evaluate(async ({ origin, record }) => {
    const rows = await (await fetch(`${origin}/api/packages/${record.packageId}/sources`, { credentials: "include" })).json();
    return rows.find((row: { id: string }) => row.id === record.source.id)?.metadata?.basic_read_status;
  }, { origin, record })).toBe("ready");
  const downloaded = await page.evaluate(async ({ origin, record }) => {
    const response = await fetch(`${origin}/api/packages/${record.packageId}/sources/${record.source.id}/download`, { credentials: "include" });
    if (!response.ok) throw new Error(await response.text());
    return response.text();
  }, { origin, record });
  expect(downloaded).toBe(original);
  expect(await page.evaluate(async ({ origin, record }) => (await fetch(`${origin}/api/packages/${record.packageId}/sources/${record.source.id}`, { method: "DELETE", credentials: "include" })).status, { origin, record })).toBe(200);
  await page.reload();
  await expect(page.locator(".ProseMirror").first()).toContainText(marker);
});

test("a portable RIDOC renders its embedded image and equation without the author's profile", async ({ page, context }) => {
  const root = path.resolve(__dirname, "../../..");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "studio-portable-browser-"));
  try {
    const fixture = path.join(directory, "portable.ridoc");
    execFileSync(path.join(root, ".venv/bin/python"), [path.join(root, "apps/api/tests/test_studio_portable.py"), path.join(directory, "author"), fixture], { cwd: root, timeout: 60_000 });
    fs.rmSync(path.join(directory, "author"), { recursive: true });
    await context.route("**/*", route => ["127.0.0.1", "localhost"].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort());
    await page.goto("/studio");
    await openLocalCourses(page);
    await page.getByLabel("Import RIDOC file").setInputFiles(fixture);
    await expect(page.locator(".ProseMirror").first()).toContainText("Portable course fixture");
    const image = page.locator('.ProseMirror img[alt="Portable image"]').first();
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBe(1);
    await expect(page.locator(".ProseMirror .katex").first()).toBeVisible();
    await page.reload();
    await expect.poll(() => page.locator('.ProseMirror img[alt="Portable image"]').first().evaluate(element => (element as HTMLImageElement).naturalWidth)).toBe(1);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test("the avatar keeps sign in and local controls in a compact account menu", async ({ page, context }) => {
  await context.route("**/*", route => ["127.0.0.1", "localhost"].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort());
  await context.route("**/api/workbench/v1/platform/connect", route => route.fulfill({ status: 503, json: { detail: "Official sign-in is unavailable in this offline test." } }));
  const panelNavigations: string[] = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname === "/studio" && url.searchParams.has("platform")) panelNavigations.push(request.url());
  });
  await page.goto("/studio");
  const avatar = page.getByRole("button", { name: "OpenClass User Avatar", exact: true });
  await expect(avatar).toBeVisible();
  const avatarBounds = await avatar.boundingBox();
  expect(avatarBounds?.width).toBe(44);
  await expect(page.getByRole("button", { name: "OpenClass connection", exact: true })).toHaveCount(0);
  await avatar.click();
  await expect(page.getByRole("menuitem", { name: "Sign in to OpenClass", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(avatar).toBeFocused();
  await avatar.click();
  await page.getByRole("menuitem", { name: "Model settings", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Personal models", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await avatar.click();
  await page.getByRole("menuitem", { name: "Sign in to OpenClass", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "OpenClass connection", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect OpenClass account", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Close platform connection", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "OpenClass connection", exact: true })).toHaveCount(0);
  await avatar.click();
  await expect(page.getByRole("menu", { name: "Account menu", exact: true })).toBeVisible();
  expect(panelNavigations).toEqual([]);
  await page.screenshot({ path: test.info().outputPath("avatar-restored.png"), fullPage: true });
});

test("sign in opens official authorization and shows the approved account and credits after refresh", async ({ page, context }) => {
  let connected = false;
  let started = 0;
  let disconnected = 0;
  await context.route("**/api/workbench/v1/platform", route => {
    if (route.request().method() === "DELETE") {
      connected = false; disconnected += 1;
      return route.fulfill({ json: { connected: false, remote_revoked: true } });
    }
    return route.fulfill({ json: { connected, origin: "https://open-classes.com", ...(connected ? {
      account_id: "approved-account", email: "approved@example.test", scopes: ["models.invoke"],
      wallet: { balance_credits: 50, available_credits: 45, reserved_credits: 5 },
    } : {}) } });
  });
  await context.route("**/api/workbench/v1/platform/connect", route => {
    expect(route.request().postDataJSON()).toEqual({ scopes: ["models.invoke"] });
    started += 1;
    return route.fulfill({ json: { attempt_id: "test-attempt", user_code: "ABCD-EFGH", interval: 0.05, expires_in: 600,
      verification_uri_complete: "https://open-classes.com/studio-connect?user_code=ABCD-EFGH" } });
  });
  await context.route("**/api/workbench/v1/platform/connect/test-attempt", route => {
    connected = true;
    return route.fulfill({ json: { status: "connected" } });
  });
  await context.route("https://open-classes.com/studio-connect?user_code=ABCD-EFGH", route => route.fulfill({ body: "<h1>Official device authorization fixture</h1>", contentType: "text/html" }));
  await page.goto("/studio");
  await page.getByRole("button", { name: "OpenClass User Avatar", exact: true }).click();
  const popup = page.waitForEvent("popup");
  await page.getByRole("menuitem", { name: "Sign in to OpenClass", exact: true }).click();
  const official = await popup;
  await expect(official).toHaveURL("https://open-classes.com/studio-connect?user_code=ABCD-EFGH");
  const dialog = page.getByRole("dialog", { name: "OpenClass connection", exact: true });
  await expect(dialog).toContainText("approved@example.test");
  await expect(dialog).toContainText("Available credits: 45");
  expect(started).toBe(1);
  await page.getByRole("button", { name: "Close platform connection", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "OpenClass User Avatar", exact: true }).click();
  await page.getByRole("menuitem", { name: "OpenClass account", exact: true }).click();
  await expect(dialog).toContainText("approved@example.test");
  expect(started).toBe(1);
  await page.getByRole("button", { name: "Disconnect platform", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Disconnected");
  expect(disconnected).toBe(1);
  await page.getByRole("button", { name: "Close platform connection", exact: true }).click();
  await expect(page.locator(".ProseMirror").first()).toHaveAttribute("contenteditable", "true");
  await official.close();
});

test("a sign-in failure keeps the local editor available and does not show a connected account", async ({ page, context }) => {
  await context.route("**/api/workbench/v1/platform", route => route.fulfill({ json: { connected: false, origin: "https://open-classes.com" } }));
  await context.route("**/api/workbench/v1/platform/connect", route => route.fulfill({ status: 503, json: { detail: "Official sign-in is temporarily unavailable." } }));
  await page.goto("/studio");
  await page.getByRole("button", { name: "OpenClass User Avatar", exact: true }).click();
  await page.getByRole("menuitem", { name: "Sign in to OpenClass", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("temporarily unavailable");
  await expect(page.getByRole("button", { name: "Connect OpenClass account", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Close platform connection", exact: true }).click();
  await expect(page.locator(".ProseMirror").first()).toHaveAttribute("contenteditable", "true");
});
