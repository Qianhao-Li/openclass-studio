const test = require("node:test");
const assert = require("node:assert/strict");
const { length, tokenBounded } = require("./token-chunks.cjs");

test("token limits retain every Unicode character and source offset", async () => {
  const tokenizer = async text => ({ input_ids: { data: new Array(Array.from(text).length + 2) } });
  const text = "😀中文甲乙丙丁戊";
  const chunks = await tokenBounded(text, tokenizer, 6);
  assert.equal(chunks.map(chunk => chunk.text).join(""), text);
  assert.equal(length(text), 8);
  assert.equal(chunks[0].end, chunks[1].start);
  assert.ok(chunks.every(chunk => length(chunk.text) <= 4));
  await assert.rejects(tokenBounded("字", tokenizer, 2));
});
