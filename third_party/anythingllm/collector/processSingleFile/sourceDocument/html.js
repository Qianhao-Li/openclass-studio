"use strict";
const { parse } = require("node-html-parser");
const path = require("node:path").posix;

function archiveName(base, value) {
  if (typeof value !== "string" || /^(?:[a-z]+:|\/|\\)/i.test(value))
    throw new Error("An original archive reference is not a local object.");
  const name = path.normalize(path.join(path.dirname(base), decodeURIComponent(value.split("#")[0])));
  if (name === ".." || name.startsWith("../") || name.includes("\0") || name.includes("\\"))
    throw new Error("An original archive reference escapes its file.");
  return name;
}

function htmlContent(value, container = "") {
  const root = parse(value, { comment: false });
  const body = root.querySelector("body") || root;
  let text = "";
  const anchors = {}, headings = [], images = [];
  const length = () => Array.from(text).length;
  function visit(node) {
    const tag = (node.tagName || "").toLowerCase();
    if (["script", "style", "head", "noscript"].includes(tag)) return;
    if (["p", "div", "li", "tr", "section", "article", "br", "h1", "h2", "h3", "h4", "h5", "h6"].includes(tag))
      text += "\n";
    const start = length();
    for (const id of [node.getAttribute?.("id"), node.getAttribute?.("name")].filter(Boolean))
      anchors[id] = Object.hasOwn(anchors, id) ? null : start;
    if (tag === "img" && node.getAttribute("src")) {
      images.push({ container: archiveName(container, node.getAttribute("src")), offset: start });
    }
    if (node.nodeType === 3) text += node.text;
    else for (const child of node.childNodes || []) visit(child);
    if (/^h[1-6]$/.test(tag) && node.text.trim())
      headings.push({ title: node.text.trim(), level: Number(tag[1]), offset: start });
  }
  visit(body);
  return { text, anchors, headings, images };
}

function safeArchive(file) {
  const zip = new (require("adm-zip"))(file);
  const entries = zip.getEntries();
  if (entries.length > 20000 || entries.reduce((n, e) => n + e.header.size, 0) > 1024 * 1024 * 1024)
    throw new Error("The original archive exceeds the parsing capacity.");
  const names = new Set();
  for (const entry of entries) {
    if (entry.entryName.includes("\\") || entry.entryName.startsWith("/") ||
        entry.entryName.split("/").includes("..") || names.has(entry.entryName))
      throw new Error("The original archive contains ambiguous or unsafe objects.");
    names.add(entry.entryName);
  }
  return {
    names,
    bytes(name) {
      const entry = zip.getEntry(name);
      if (!entry || entry.isDirectory || entry.header.size > 128 * 1024 * 1024)
        throw new Error("An original archive object is missing or exceeds the parsing capacity.");
      return entry.getData();
    },
    xml(name) {
      const text = this.bytes(name).toString("utf8");
      if (/<!ENTITY/i.test(text)) throw new Error("External XML entities are not supported.");
      return parse(text, { comment: false });
    },
  };
}

module.exports = { archiveName, htmlContent, safeArchive };
