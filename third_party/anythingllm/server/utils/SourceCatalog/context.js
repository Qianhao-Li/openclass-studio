"use strict";

class SourceCatalogError extends Error {
  constructor(message) { super(message); this.publicMessage = message; }
}

const HEADINGS = new Set(["contents", "tableofcontents", "目录", "目次", "总目录", "inhalt", "sommaire"]);
const codepoints = value => Array.from(value);
const length = value => codepoints(value).length;

function folded(value) {
  const chars = codepoints(String(value)), text = [], offsets = [], ends = [];
  const accents = { "´": "\u0301", "˝": "\u030b", "¨": "\u0308", "ˇ": "\u030c" };
  for (let i = 0; i < chars.length; i++) {
    const start = i;
    let part = chars[i];
    if (accents[part]) {
      let next = i + 1;
      while (/\s/u.test(chars[next] || "")) next++;
      if (/^[a-z]$/i.test(chars[next] || "")) { part = chars[next] + accents[part]; i = next; }
    }
    if (/[-‐‑–—]/.test(part)) {
      part = "-";
      while (/^[-‐‑–—]$/.test(chars[i + 1] || "")) i++;
    }
    part = part.normalize("NFKD").toLowerCase().replace(/[‘’]/g, "'").replace(/∗/g, "*")
      .replace(/\s|\p{Cf}/gu, "");
    for (const char of part) { text.push(char); offsets.push(start); ends.push(i + 1); }
  }
  return { text: text.join(""), offsets, ends };
}
const canonical = value => folded(value).text;

function label(value) {
  return canonical(String(value).replace(/^(?:(?:chapter|section|part|appendix)\s+)?(?:\d+(?:\.\d+)*[.)]?\s*|[IVXLCDM]+[.)]?\s+|[A-Z][.)]\s*)/i, "")
    .replace(/\s*(?:[*†‡⇤]+|\([*†‡⇤]+\))\s*$/, ""));
}

function containers(data) {
  const groups = [];
  for (const unit of data.units) {
    const key = [unit.locator.kind, unit.locator.start, unit.locator.container || ""].join(":");
    let group = groups.at(-1);
    if (group?.key !== key) { group = { key, locator: unit.locator, units: [], text: "" }; groups.push(group); }
    group.units.push(unit);
    group.text += unit.text;
  }
  return groups;
}

function isTocHeading(line) {
  const key = canonical(line);
  return [...HEADINGS].some(word => {
    const position = key.indexOf(word);
    return position >= 0 && /^[ivxlcdm\d]*$/.test(key.slice(0, position)) &&
      /^[ivxlcdm\d]*$/.test(key.slice(position + word.length));
  });
}

function printedRows(group, state = { pending: [], first: null }) {
  const result = [], lines = group.text.split(/\r?\n/);
  let pending = state.pending;
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index].trim();
    if (!line || isTocHeading(line)) continue;
    if (/^[.·…\s]+$/.test(line)) continue;
    // An isolated roman header at the edge of a directory page is pagination.
    if (group.locator.metadata?.pagination?.includes(line) && (index < 3 || index >= lines.length - 2)) continue;
    const match = /^(?:part|chapter|section)\s+(?:\d+|[ivxlcdm]+)[.)]?$/i.test(line) ? null :
      /^(.+?)\s*(?:[.·…]\s*){2,}(\d+|[ivxlcdm]+)$/i.exec(line)
      || /^(.+?\S)\s+(\d+|[ivxlcdm]+)$/i.exec(line);
    if (match) {
      if (/^[*†‡]\s/.test(pending[0] || "") && /^(?:\d+(?:\.\d+)*[.)]?|[A-Z][.)])\s+/.test(match[1]))
        pending = [];
      const title = [...pending, match[1]].join(" ").trim();
      if (title) result.push({ title, printed_page_label: match[2], toc_unit: state.first || group.units[0].id });
      pending = []; state.first = null;
    } else if (/^(?:\d+|[ivxlcdm]+)$/i.test(line) && pending.length) {
      result.push({ title: pending.join(" "), printed_page_label: line, toc_unit: state.first || group.units[0].id });
      pending = []; state.first = null;
    } else { if (!pending.length) state.first = group.units[0].id; pending.push(line); }
  }
  state.pending = pending;
  return result;
}

function directoryScope(data) {
  const all = containers(data);
  if (data.format !== "pdf") return { groups: all, printed: [], mode: "complete_original" };
  const first = all.findIndex(group => group.text.split(/\r?\n/).some(isTocHeading));
  if (first < 0) return { groups: all, printed: [], mode: "complete_original" };
  const picked = [], printed = [];
  const state = { pending: [], first: null };
  const start = all[first].locator.start;
  const body = data.native_entries.filter(entry => entry.range.start > start && /^\d+[.)]?\s*\D/.test(entry.title))
    .map(entry => entry.range.start);
  const stop = body.length ? Math.min(...body) : Infinity;
  for (const group of all.slice(first)) {
    if (group.locator.start >= stop) break;
    const rows = printedRows(group, state);
    if (!rows.length) break;
    picked.push(group); printed.push(...rows);
  }
  if (!printed.length) return { groups: all, printed: [], mode: "complete_original" };
  return { groups: picked, printed, mode: "original_directory" };
}

function attachmentText(groups) {
  return groups.flatMap(group => group.units.map(unit => "[original position=" + unit.id +
    "; " + unit.locator.kind + "=" + unit.locator.start +
    (unit.locator.container ? "; container=" + unit.locator.container : "") + "]\n" + unit.text)).join("\n\n");
}

function batches(groups, limit, imageMode = false, maxImages = 6) {
  const result = []; let current = [], used = 0, images = 0;
  for (const group of groups) {
    const image = group.locator.kind === "image_frames" || group.locator.kind === "pdf_pages" &&
      (!group.text.trim() || group.locator.metadata?.requires_original_image);
    const size = Buffer.byteLength(attachmentText([group])) + 100;
    if (size > limit) throw new SourceCatalogError("An original document object exceeds the selected model's request capacity.");
    if (current.length && (used + size > limit || images + Number(Boolean(image)) > maxImages || imageMode && current.length >= maxImages)) {
      result.push(current); current = []; used = 0; images = 0;
    }
    current.push(group); used += size; images += Number(Boolean(image));
  }
  if (current.length) result.push(current);
  return result;
}

module.exports = { SourceCatalogError, canonical, folded, label, containers, printedRows, directoryScope, attachmentText, batches, codepoints, length };
