const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

test("Pi bounds image history while preserving current original bytes and refusing an oversized new batch", async () => {
  process.env.OPENCLASS_PI_REPOSITORY_BROKER_URL = "http://127.0.0.1:1";
  process.env.OPENCLASS_PI_REPOSITORY_BROKER_TOKEN = "a".repeat(64);
  const {loadExtensions} = await import(pathToFileURL(path.resolve(__dirname,"../../node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js")));
  const loaded = await loadExtensions([path.resolve(__dirname,"../../apps/api/app/services/pi_catalog_refinement_extension.ts")],process.cwd());
  assert.deepEqual(loaded.errors,[]);
  const extension=loaded.extensions[0], previous=globalThis.fetch;
  try {
    globalThis.fetch=async()=>({ok:true,json:async()=>({result:{image_transfer_limits:{request_bytes:5000}}})});
    await extension.tools.get("source_info").definition.execute("id",{});
  } finally { globalThis.fetch=previous; }
  const handler=extension.handlers.get("before_provider_request")[0];
  const old="data:image/png;base64,"+"a".repeat(4000), current="data:image/png;base64,"+"b".repeat(3200);
  const payload={messages:[{role:"user",content:[{type:"image_url",image_url:{url:old}}]},
    {role:"assistant",content:"read next page"},{role:"user",content:[{type:"image_url",image_url:{url:current}}]}]};
  const result=await handler({payload});
  assert.equal(result.messages[0].content[0].type,"text");
  assert.equal(result.messages[2].content[0].image_url.url,current);
  assert.equal(payload.messages[0].content[0].image_url.url,old);
  assert.ok(Buffer.byteLength(JSON.stringify(result))<=5000);
  await assert.rejects(async()=>handler({payload:{messages:[{role:"assistant",content:"read"},
    {role:"user",content:[{type:"image_url",image_url:{url:"x".repeat(6000)}}]}]}}),/Current original image batch/);
});
