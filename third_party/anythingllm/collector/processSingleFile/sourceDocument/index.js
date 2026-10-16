"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { htmlContent, safeArchive } = require("./html");
const { SourceDocumentError } = require("./error");

const PARSER = "anythingllm_positioned_document_v2";
const digest = value => crypto.createHash("sha256").update(value).digest("hex");
const charLength = value => Array.from(value).length;

function positioned(parts) {
  const units = [];
  for (const part of parts) {
    const chars = Array.from(part.text);
    let start = 0;
    do {
      let end = Math.min(start + 1500, chars.length);
      while (end > start && Buffer.byteLength(chars.slice(start, end).join("")) > 2500) end--;
      units.push({ id: units.length + 1, text: chars.slice(start, end).join(""),
        text_start: start, text_end: end, locator: part.locator,
        original_images: (part.images || []).filter(image => image.offset >= start &&
          (image.offset < end || end === chars.length && image.offset === end)) });
      start = end;
    } while (start < chars.length);
  }
  return units;
}

async function pdf(file) {
  const PDFLoader = require("../convert/asPDF/PDFLoader");
  const loader = new PDFLoader(file, { splitPages: true, includeEmptyPages: true, preserveLayoutSpaces: true });
  const documents = await loader.load();
  const parts = documents.map(doc => ({ text: doc.pageContent,
    locator: { kind: "pdf_pages", start: doc.metadata.loc.pageNumber, end: doc.metadata.loc.pageNumber,
      metadata: { requires_original_image: !doc.pageContent.trim() || /[\u0000\ufffd]/u.test(doc.pageContent),
        text_layer_incomplete: /[\u0000\ufffd]/u.test(doc.pageContent), pagination: doc.metadata.pagination } } }));
  const entries = loader.nativeMetadata.outline.map(entry => ({ title: entry.title, level: entry.level,
    range: { kind: "pdf_pages", start: entry.page, end: entry.page } }));
  return { parts, entries, authored: entries.length > 0, reader: "AnythingLLM PDFLoader",
    page_labels: loader.nativeMetadata.pageLabels, page_count: loader.nativeMetadata.totalPages };
}

async function docx(file) {
  const archive = safeArchive(file);
  const { DocxLoader } = require("langchain/document_loaders/fs/docx");
  await new DocxLoader(file).load();
  const document = archive.xml("word/document.xml");
  const parts = [], entries = [];
  for (const paragraph of document.getElementsByTagName("w:p")) {
    const text = paragraph.getElementsByTagName("w:t").map(node => node.text).join("");
    const locator = { kind: "docx_paragraphs", start: parts.length + 1, end: parts.length + 1 };
    parts.push({ text, locator });
    const style = paragraph.getElementsByTagName("w:pStyle")[0]?.getAttribute("w:val") || "";
    const match = /^(?:heading|标题)([1-9])$/i.exec(style);
    if (match && text.trim()) entries.push({ title: text.trim(), level: Number(match[1]), range: locator });
  }
  return { parts, entries, authored: false, reader: "AnythingLLM DocxLoader" };
}

async function office(file, extension) {
  const archive = safeArchive(file);
  await require("officeparser").parseOfficeAsync(file);
  const parts = [], entries = [];
  if (extension === ".pptx") {
    const names = [...archive.names].filter(name => /^ppt\/slides\/slide\d+\.xml$/.test(name))
      .sort((a, b) => Number(a.match(/\d+/)[0]) - Number(b.match(/\d+/)[0]));
    for (const name of names) {
      const document = archive.xml(name);
      const texts = document.getElementsByTagName("a:p").map(node => node.getElementsByTagName("a:t").map(n => n.text).join(""));
      const locator = { kind: "ppt_slides", start: parts.length + 1, end: parts.length + 1 };
      parts.push({ text: texts.join("\n"), locator });
      entries.push({ title: texts.find(text => text.trim()) || "Slide " + locator.start, level: 1, range: locator });
    }
  } else if (extension === ".xlsx" || extension === ".xlsm") {
    const workbook = archive.xml("xl/workbook.xml"), rels = archive.xml("xl/_rels/workbook.xml.rels");
    const targets = new Map(rels.getElementsByTagName("Relationship").map(node => [node.getAttribute("Id"), node.getAttribute("Target")]));
    const strings = archive.names.has("xl/sharedStrings.xml")
      ? archive.xml("xl/sharedStrings.xml").querySelectorAll("si").map(n => n.querySelectorAll("t").map(t => t.text).join("")) : [];
    for (const sheet of workbook.querySelectorAll("sheet")) {
      const name = sheet.getAttribute("name");
      const target = targets.get(sheet.getAttribute("r:id"));
      const fileName = require("./html").archiveName("xl/workbook.xml", target);
      const rows = archive.xml(fileName).querySelectorAll("row");
      let first = null, last = null;
      for (const row of rows) {
        const index = Number(row.getAttribute("r"));
        if (!Number.isSafeInteger(index) || index < 1) throw new Error("An original worksheet row has no position.");
        const values = row.querySelectorAll("c").map(cell => {
          const value = cell.querySelector("v")?.text || "";
          if (cell.getAttribute("t") === "s") return strings[Number(value)] ?? "";
          if (cell.getAttribute("t") === "inlineStr") return cell.querySelectorAll("t").map(n => n.text).join("");
          return value;
        });
        const locator = { kind: "sheet_rows", start: index, end: index, container: name };
        parts.push({ text: JSON.stringify(values), locator });
        first ??= index; last = index;
      }
      if (first !== null) entries.push({ title: name, level: 1,
        range: { kind: "sheet_rows", start: first, end: last, container: name } });
    }
  } else {
    const document = archive.xml("content.xml");
    const nodes = [...document.getElementsByTagName("text:h"), ...document.getElementsByTagName("text:p")];
    for (const node of nodes) {
      const text = node.text;
      const locator = { kind: "docx_paragraphs", start: parts.length + 1, end: parts.length + 1 };
      parts.push({ text, locator });
      if (node.tagName.toLowerCase() === "text:h") entries.push({ title: text.trim(),
        level: Number(node.getAttribute("text:outline-level") || 1), range: locator });
    }
  }
  return { parts, entries, authored: false, reader: "AnythingLLM officeparser" };
}

async function text(file, extension) {
  const asTxt = require("../convert/asTxt");
  let content = asTxt.decodeText(fs.readFileSync(file));
  if (!content) throw new Error("The original text could not be decoded.");
  if (content.includes("\0")) throw new Error("This binary document is not a supported text format.");
  if (extension === ".html" || extension === ".htm") content = htmlContent(content).text;
  const parts = content.split(/\r?\n/).map((value, index) => ({ text: value,
    locator: { kind: "text_lines", start: index + 1, end: index + 1 } }));
  return { parts, entries: [], authored: false, reader: "AnythingLLM asTxt" };
}

async function readSourceDocument(file, source, { fileName, mimeType = "" }) {
  const raw = fs.readFileSync(file);
  if (digest(raw) !== source.content_hash) throw new Error("The original source hash changed during collection.");
  const extension = path.extname(fileName).toLowerCase();
  let read, format = "native";
  try {
    if (extension === ".pdf") { read = await pdf(file); format = "pdf"; }
    else if (extension === ".epub") read = await require("./epub").readEpub(file);
    else if (extension === ".docx") read = await docx(file);
    else if ([".pptx", ".xlsx", ".xlsm", ".odt", ".odp"].includes(extension)) read = await office(file, extension);
    else if ([".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff", ".tif", ".bmp"].includes(extension)) {
      const info = await require("sharp")(raw, { animated: true }).metadata();
      const frames = info.pages || 1;
      read = { parts: Array.from({ length: frames }, (_, i) => ({ text: "",
        locator: { kind: "image_frames", start: i + 1, end: i + 1 } })), entries: [], authored: false,
        reader: "AnythingLLM original image adapter", frame_count: frames };
      format = "visual";
    } else if ([".txt", ".md", ".rst", ".org", ".adoc", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".tex"].includes(extension))
      read = await text(file, extension);
    else throw new SourceDocumentError("Unsupported source format: " + (extension || "unknown") + ".");
  } catch (error) {
    if (error.name === "PasswordException" || /password|encrypt/i.test(error.message))
      throw new SourceDocumentError("The original document is encrypted. Upload an unlocked original.");
    if (error instanceof SourceDocumentError) throw error;
    throw new SourceDocumentError("The original document is damaged or could not be parsed as its declared format.");
  }
  const units = positioned(read.parts);
  if (!units.length) throw new Error("The original document contains no readable objects.");
  return { schema: "source_basic_content_v1", source_id: source.id, source_version: source.source_version,
    content_hash: source.content_hash, parser_provider: "anythingllm", parser_version: PARSER,
    format, native_format: extension.slice(1), units, native_entries: read.entries, authored: read.authored,
    visual_only: units.every(unit => !unit.text.trim()), frame_count: read.frame_count,
    page_count: read.page_count, page_labels: read.page_labels, reader: read.reader,
    configuration: { schema: "source_basic_content_v1", parser_provider: "anythingllm", parser_version: PARSER } };
}

module.exports = { readSourceDocument, PARSER, positioned, charLength };
