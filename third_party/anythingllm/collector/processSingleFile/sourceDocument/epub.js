"use strict";
const { archiveName, htmlContent, safeArchive } = require("./html");
const crypto = require("node:crypto");

async function readEpub(file) {
  const archive = safeArchive(file);
  const container = archive.xml("META-INF/container.xml");
  const packageName = container.querySelector("rootfile")?.getAttribute("full-path");
  if (!packageName) throw new Error("The original EPUB has no package manifest.");
  const opf = archive.xml(packageName);
  const manifest = new Map(opf.querySelectorAll("manifest item").map(item => [item.getAttribute("id"), item]));
  const parts = [], entries = [], anchors = new Map();
  for (const itemref of opf.querySelectorAll("spine itemref")) {
    const item = manifest.get(itemref.getAttribute("idref"));
    if (!item) throw new Error("An original EPUB spine item is missing.");
    const name = archiveName(packageName, item.getAttribute("href"));
    const html = htmlContent(archive.bytes(name).toString("utf8"), name);
    const locator = { kind: "epub_spine", start: parts.length + 1, end: parts.length + 1, container: name };
    parts.push({ text: html.text, locator, images: html.images.map(image => ({ ...image,
      sha256: crypto.createHash("sha256").update(archive.bytes(image.container)).digest("hex") })) });
    anchors.set(name, { ...html, locator });
    for (const image of html.images) archive.bytes(image.container);
  }
  let nav = [...manifest.values()].find(item => (item.getAttribute("properties") || "").split(/\s+/).includes("nav"));
  const ncx = [...manifest.values()].find(item => item.getAttribute("media-type") === "application/x-dtbncx+xml");
  const navName = nav ? archiveName(packageName, nav.getAttribute("href")) : ncx ? archiveName(packageName, ncx.getAttribute("href")) : null;
  function add(title, href, level) {
    const name = archiveName(navName, href);
    const target = anchors.get(name);
    const fragment = decodeURIComponent(href.split("#")[1] || "");
    const offset = fragment ? target?.anchors[fragment] : 0;
    if (!target || offset == null || !title.trim())
      throw new Error("An original EPUB directory destination is missing or ambiguous.");
    entries.push({ title: title.trim(), level, range: { ...target.locator,
      metadata: { native_char_offset: offset, native_anchor: fragment,
        requires_original_image: !target.text.trim() && target.images.length > 0 } } });
  }
  if (navName) {
    const document = archive.xml(navName);
    if (nav) {
      const toc = document.querySelectorAll("nav").find(node => (node.getAttribute("epub:type") || node.getAttribute("type") || "").split(/\s+/).includes("toc"));
      function visit(list, level) {
        for (const li of list?.childNodes.filter(node => node.tagName?.toLowerCase() === "li") || []) {
          const link = li.childNodes.find(node => node.tagName?.toLowerCase() === "a");
          if (link) add(link.text, link.getAttribute("href"), level);
          const nested = li.childNodes.find(node => node.tagName?.toLowerCase() === "ol");
          if (nested) visit(nested, level + (link ? 1 : 0));
        }
      }
      visit(toc?.querySelector("ol"), 1);
    } else {
      function visit(node, level) {
        for (const point of node.childNodes.filter(n => n.tagName?.toLowerCase() === "navpoint")) {
          const content = point.childNodes.find(n => n.tagName?.toLowerCase() === "content");
          const label = point.childNodes.find(n => n.tagName?.toLowerCase() === "navlabel");
          if (!content || !label) throw new Error("An original EPUB directory label is invalid.");
          add(label.text, content.getAttribute("src"), level);
          visit(point, level + 1);
        }
      }
      const map = document.querySelector("navMap") || document.querySelector("navmap");
      if (map) visit(map, 1);
    }
  }
  // The same loader used by AnythingLLM's native EPUB converter reads valid
  // spine documents. Archive positions supplement its otherwise flat text.
  const { EPubLoader } = require("langchain/document_loaders/fs/epub");
  await new EPubLoader(file, { splitChapters: true }).load();
  return { parts, entries, authored: entries.length > 0, reader: "AnythingLLM EPubLoader" };
}

module.exports = { readEpub };
