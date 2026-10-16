import { test, expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

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
  await page.getByRole("button", { name: "Courses", exact: true }).click();
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
  await page.getByRole("button", { name: "Courses", exact: true }).click();
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
    await page.getByRole("button", { name: "Courses", exact: true }).click();
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
