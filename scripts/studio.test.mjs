import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { acquireStudioLease, studioEnvironment } from "./studio.mjs";

test("local launch excludes platform and ambient model credentials", () => {
  const env = studioEnvironment({ PATH: "/bin", OPENCLASS_PAYPAL_CLIENT_SECRET: "private", OPENCLASS_DATABASE_PATH: "/production/db", OPENAI_API_KEY: "personal-but-not-authorized", CODEX_ACCESS_TOKEN: "private" }, "/local/data", "new-launch-token", 3510, 8510);
  assert.equal(env.OPENCLASS_PAYPAL_CLIENT_SECRET, undefined);
  assert.equal(env.OPENCLASS_DATABASE_PATH, undefined);
  assert.equal(env.OPENAI_API_KEY, undefined);
  assert.equal(env.CODEX_ACCESS_TOKEN, undefined);
  assert.equal(env.OPENCLASS_STUDIO_API_ORIGIN, "http://127.0.0.1:8510");
});

test("one local backend owns a profile, and release preserves a changed lease", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "studio-launcher-test-"));
  try {
    const release = acquireStudioLease(root);
    assert.throws(() => acquireStudioLease(root), /already running/);
    fs.writeFileSync(path.join(root, "server.lock"), "another-owner");
    release();
    assert.equal(fs.readFileSync(path.join(root, "server.lock"), "utf8"), "another-owner");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a surviving backend keeps a crashed launcher's workspace locked", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "studio-orphan-test-"));
  try {
    fs.writeFileSync(path.join(root, "server.lock"), JSON.stringify({ pid: 2_000_000_000, children: [process.pid] }));
    assert.throws(() => acquireStudioLease(root), /already running/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("sync retries retain one operation identity after refresh and clear it after success", async () => {
  const file = ["../packages/studio-ui/src/lib/studio-sync-operation.ts", "../apps/web/src/lib/studio-sync-operation.ts"]
    .map(name => new URL(name, import.meta.url)).find(value => fs.existsSync(value));
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const values = new Map();
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
    getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key),
  } });
  try {
    const first = await import(file.href + "?first");
    const opened = first.pendingSyncOperation("profile:one:pull", () => ({ operation_id: "operation_first_123", revision: "saved" }));
    const refreshed = await import(file.href + "?refreshed");
    assert.deepEqual(refreshed.pendingSyncOperation("profile:one:pull", () => assert.fail("A retry needs its original identity")), opened);
    assert.equal(refreshed.pendingSyncOperation("profile:two:pull", () => ({ operation_id: "operation_other_123" })).operation_id, "operation_other_123");
    refreshed.completeSyncOperation("profile:one:pull");
    assert.equal(refreshed.pendingSyncOperation("profile:one:pull", () => ({ operation_id: "operation_next_123" })).operation_id, "operation_next_123");
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous); else delete globalThis.localStorage;
  }
});
