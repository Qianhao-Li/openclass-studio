"use strict";

// All public source offsets count Unicode code points, matching the Python reader.
const length = text => Array.from(text).length;

async function tokenBounded(text, tokenizer, limit, prefix = "") {
  if (!tokenizer) return [{ text, start: 0, end: length(text) }];
  const characters = Array.from(text);
  const result = [];
  let start = 0;
  while (start < characters.length) {
    let low = 1, high = characters.length - start, fit = 0;
    while (low <= high) {
      const size = Math.floor((low + high) / 2);
      const input = prefix + characters.slice(start, start+size).join("");
      const encoded = await tokenizer(input, { truncation: false, padding: false });
      const count = encoded.input_ids.data.length;
      if (count <= limit) { fit = size; low = size+1; } else high = size-1;
    }
    if (!fit) throw new Error("One source character exceeds the embedding tokenizer capacity.");
    result.push({ text: characters.slice(start, start+fit).join(""), start, end: start+fit });
    start += fit;
  }
  return result;
}

module.exports = { length, tokenBounded };
