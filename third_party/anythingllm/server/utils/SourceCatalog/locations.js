"use strict";
const crypto = require("node:crypto");
const { SourceCatalogError, canonical, folded, label, containers, codepoints, length } = require("./context");
const digest = value => crypto.createHash("sha256").update(value).digest("hex");

function unitAt(group, offset) {
  const unit = group.units.find(unit => unit.text_start <= offset &&
    (offset < unit.text_end || unit.text_start === unit.text_end && offset === unit.text_start));
  return unit ? { unit: unit.id, char: offset - unit.text_start } : null;
}

function heading(group, value, ordinal = "") {
  const map = folded(group.text);
  const needle = canonical(value);
  if (!needle) return [];
  const result = [];
  let start = 0;
  while ((start = map.text.indexOf(needle, start)) >= 0) {
    const offset = map.offsets[start], end = map.ends[start + needle.length - 1];
    const chars = codepoints(group.text);
    const prefix = chars.slice(0, offset).join("").split("\n").at(-1).trim();
    const tail = chars.slice(end).join("").split("\n")[0].trim();
    if ((!prefix || /^(?:part|chapter|section|appendix)$/i.test(prefix) ||
        ordinal && structuralNumber(prefix) === ordinal) &&
        (!ordinal || structuralNumber(prefix) === ordinal || structuralNumber(value) === ordinal) &&
        (!tail || /^[*†‡⇤]+$/.test(tail))) {
      const before = chars.slice(0, offset).join("");
      const originalStart = prefix ? length(before.slice(0, before.lastIndexOf("\n") + 1)) : offset;
      const position = unitAt(group, originalStart);
      if (position) result.push(position);
    }
    start++;
  }
  return result;
}

function structuralNumber(value) {
  const original = String(value).trim().replace(/^(?:part|chapter|section|appendix)[.\s]+/i, "");
  return (/^(\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])(?:[.)]|\s|$)/.exec(original)?.[1] ||
    /^(\d+(?:\.\d+)*)(?=[A-Za-z])/.exec(original)?.[1] || "").toLowerCase();
}

function nativeMatches(data, row) {
  const number = structuralNumber(row.title);
  return data.native_entries.filter(entry => entry.level === row.level && label(entry.title) === label(row.title) &&
    (!number || structuralNumber(entry.title) === number));
}

function bodyTitle(value) {
  return String(value).replace(/^(?:(?:part|chapter|section|appendix)[.\s]+)?(?:\d+(?:\.\d+)*[.)]?\s*|[IVXLCDM]+[.)]?\s+|[A-Z][.)]\s*)/i, "")
    .replace(/\s*(?:[*†‡⇤]+|\([*†‡⇤]+\))\s*$/, "");
}

function nativeOffsets(data, rows) {
  const values = [];
  for (const row of rows) {
    if (!/^\d+$/.test(row.printed_page_label || "")) continue;
    const matches = nativeMatches(data, row);
    if (matches.length === 1 && matches[0].range.kind === "pdf_pages")
      values.push(matches[0].range.start - Number(row.printed_page_label));
  }
  const counts = new Map();
  values.forEach(value => counts.set(value, (counts.get(value) || 0) + 1));
  const ranked = [...counts].sort((a, b) => b[1] - a[1]);
  return ranked[0]?.[1] >= 2 && (!ranked[1] || ranked[0][1] > ranked[1][1]) ? ranked[0][0] : null;
}

function locate(data, rows, scope, verifiedImages = []) {
  const groups = containers(data), after = new Set(scope.groups.map(group => group.key));
  const offset = nativeOffsets(data, rows);
  const pageLabels = new Map();
  for (const group of groups) {
    const labels = [...(group.locator.metadata?.pagination || []), data.page_labels?.[group.locator.start - 1]].filter(Boolean);
    for (const value of new Set(labels)) {
      if (!pageLabels.has(value)) pageLabels.set(value, []);
      pageLabels.get(value).push(group);
    }
  }
  const results = [], unresolved = [];
  for (let index = 0; index < rows.length; index++) {
    const row = rows[index];
    const parent = rows.slice(0, index).findLast(other => other.level < row.level);
    const qualifier = parent && !structuralNumber(row.title) ? structuralNumber(parent.title)
      ? "Chapter " + structuralNumber(parent.title) : /^appendix[.\s]/i.test(parent.title) ? "Appendix" : "" : "";
    const native = nativeMatches(data, row);
    let candidates = [], chosen = null;
    const page = offset !== null && /^\d+$/.test(row.printed_page_label || "")
      ? Number(row.printed_page_label) + offset : null;
    let inspected = groups;
    if (native.length === 1) inspected = groups.filter(group => group.locator.kind === native[0].range.kind &&
      group.locator.start === native[0].range.start && (group.locator.container || "") === (native[0].range.container || ""));
    else if (pageLabels.get(row.printed_page_label)?.length === 1) inspected = pageLabels.get(row.printed_page_label);
    else if (page !== null) inspected = groups.filter(group => group.locator.start === page);
    else if (row.body_unit) inspected = groups.filter(group => group.units.some(unit => unit.id === row.body_unit));
    const anchored = native.length === 1 || pageLabels.get(row.printed_page_label)?.length === 1 || page !== null;
    if ((row.body_heading || qualifier) && anchored) inspected = groups.filter(group => inspected.some(primary =>
      primary.locator.kind === group.locator.kind && primary.locator.kind === "pdf_pages" &&
      Math.abs(primary.locator.start - group.locator.start) <= 1));
    if (row.body_unit && !inspected.some(group => group.units.some(unit => unit.id === row.body_unit))) {
      const actual = groups.find(group => group.units.some(unit => unit.id === row.body_unit));
      if (actual && inspected.some(group => group.locator.kind === "pdf_pages" &&
          Math.abs(group.locator.start - actual.locator.start) <= 1)) inspected = [actual];
    }
    for (const group of inspected) {
      if (scope.mode === "original_directory" && after.has(group.key)) continue;
      const entry = native.find(entry => entry.range.kind === group.locator.kind &&
        entry.range.start === group.locator.start && (entry.range.container || "") === (group.locator.container || ""));
      if (entry?.range.metadata?.native_char_offset != null) {
        const point = unitAt(group, entry.range.metadata.native_char_offset);
        if (point) { candidates.push(point); continue; }
      }
      const ordinalVariant = row.title.replace(/^((?:(?:part|chapter|section)[.\s]*)?(?:\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z]))[.)]\s+/i, "$1 ")
        .replace(/^(appendix)[.]\s+/i, "$1 ");
      const bodyAllowed = row.body_heading && (anchored && native.length === 1 ||
        canonical(row.body_heading).includes(label(row.title)) ||
        canonical(row.body_heading).includes(canonical(ordinalVariant)));
      const titles = bodyAllowed ? [row.body_heading] : [...new Set([row.title, ordinalVariant, entry?.title,
        qualifier && row.title + "—" + qualifier].filter(Boolean))];
      for (const title of titles) {
        candidates.push(...heading(group, title));
        candidates.push(...heading(group, bodyTitle(title), structuralNumber(title)));
      }
      if (entry && !candidates.length && scope.mode === "original_directory" && group.locator.start < Math.min(...scope.groups.map(group => group.locator.start)))
        candidates.push({ unit: group.units[0].id, char: 0, native_object: true });
      if (verifiedImages.includes(row.body_unit || row.start_unit) && group.units.some(unit => unit.id === (row.body_unit || row.start_unit)))
        candidates.push({ unit: row.body_unit || row.start_unit, char: 0, original_image: true });
    }
    candidates = [...new Map(candidates.map(value => [value.unit + ":" + value.char, value])).values()];
    if (row.body_unit && !anchored) candidates = candidates.filter(value => value.unit === row.body_unit);
    if (candidates.length === 1) chosen = candidates[0];
    if (!chosen && !scope.printed.length && !data.authored && data.units[row.start_unit - 1]) {
      const unit = data.units[row.start_unit - 1];
      if (unit.text.trim() || verifiedImages.includes(unit.id)) chosen = { unit: unit.id, char: 0,
        original_image: !unit.text.trim(), generated: true };
    }
    if (chosen) results.push({ ...row, ...chosen });
    else unresolved.push({ index, title: row.title, level: row.level, printed_page_label: row.printed_page_label });
  }
  return { rows: results, unresolved };
}

module.exports = { locate, heading, nativeOffsets, nativeMatches };
