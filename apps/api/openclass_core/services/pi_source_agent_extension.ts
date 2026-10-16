import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { createReadStream } from "node:fs";
import { chmod, lstat, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { promisify } from "node:util";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const execFileAsync = promisify(execFile);
const MAX_TEXT_OUTPUT = 120_000;
const MAX_ARCHIVE_ENTRIES = 50_001;
const MAX_ARCHIVE_LIST_PAGE_ENTRIES = 2_000;
const MAX_ARCHIVE_ENTRY_CHARACTERS = 120_000;
const MAX_ARCHIVE_ENTRY_BUFFER_BYTES = 16 * 1024 * 1024;
const MAX_CATALOG_BYTES = 16 * 1024 * 1024;
const MAX_TEXT_LINES_PER_CALL = 500;
const MAX_PDF_PAGE_SPAN = 32;
const MAX_PDF_SEARCH_MATCHES = 200;

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required OpenClass source runtime setting: ${name}`);
  return value;
}

const workspace = resolve(process.cwd());
const sourcePath = resolve(workspace, requiredEnvironment("OPENCLASS_PI_SOURCE_FILE"));
const scratchPath = resolve(workspace, requiredEnvironment("OPENCLASS_PI_SOURCE_SCRATCH"));
const toolboxBin = resolve(requiredEnvironment("OPENCLASS_PI_SOURCE_TOOLBOX_BIN"));
const inspectionScope = requiredEnvironment("OPENCLASS_PI_SOURCE_INSPECTION_SCOPE");
if (inspectionScope !== "directory_only" && inspectionScope !== "source" && inspectionScope !== "catalog_v2" && inspectionScope !== "catalog_v3" && inspectionScope !== "repository") {
  throw new Error("OpenClass source runtime received an unsupported inspection scope");
}
const AGENT_CATALOG_SCHEMA_VERSION = inspectionScope === "catalog_v3" ? "agent_catalog_v3" : "agent_catalog_v2";
const repositoryBrokerUrl = (process.env.OPENCLASS_PI_REPOSITORY_BROKER_URL ?? "").trim().replace(/\/$/, "");
const repositoryBrokerToken = (process.env.OPENCLASS_PI_REPOSITORY_BROKER_TOKEN ?? "").trim();
const repositoryBrokerMode = inspectionScope === "repository" && Boolean(repositoryBrokerUrl);
if (repositoryBrokerMode) {
  const parsed = new URL(repositoryBrokerUrl);
  if (parsed.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(parsed.hostname) || !parsed.port || parsed.pathname !== "/" || parsed.search || parsed.hash || repositoryBrokerToken.length < 32) {
    throw new Error("OpenClass repository runtime received an invalid host broker");
  }
}
const archivePrefix = (process.env.OPENCLASS_PI_SOURCE_ARCHIVE_PREFIX ?? "").trim().replace(/^\/+|\/+$/g, "");
if (
  inspectionScope === "repository" &&
  !repositoryBrokerMode &&
  (!archivePrefix || archivePrefix.includes("\\") || archivePrefix.includes("\0") || archivePrefix.split("/").length !== 1 || archivePrefix === "..")
) {
  throw new Error("OpenClass repository runtime received an invalid archive root prefix");
}
const repositoryReadablePathsFile = inspectionScope === "repository" && !repositoryBrokerMode
  ? requiredEnvironment("OPENCLASS_PI_REPOSITORY_READABLE_PATHS")
  : "";
const catalogPath = join(scratchPath, "catalog.json");
const catalogReceiptPath = join(scratchPath, "catalog-receipt.json");
const catalogHeaderPath = join(scratchPath, "catalog-header.json");
const catalogNodesPath = join(scratchPath, "catalog-nodes.json");
const pythonBin = requiredEnvironment("OPENCLASS_PI_PYTHON_BIN");

function assertWorkspacePath(path: string, expectedParent: string): void {
  if (dirname(path) !== expectedParent) {
    throw new Error("OpenClass source runtime rejected a path outside its isolated workspace");
  }
}

assertWorkspacePath(sourcePath, workspace);
assertWorkspacePath(catalogPath, scratchPath);
assertWorkspacePath(catalogReceiptPath, scratchPath);
assertWorkspacePath(catalogHeaderPath, scratchPath);
assertWorkspacePath(catalogNodesPath, scratchPath);
const repositoryReadablePathsPath = repositoryReadablePathsFile
  ? resolve(workspace, repositoryReadablePathsFile)
  : "";
if (repositoryReadablePathsPath) assertWorkspacePath(repositoryReadablePathsPath, workspace);

let catalogMutationQueue: Promise<void> = Promise.resolve();
let catalogPublished = false;

function conflictResolutionKeys(header: Record<string, unknown>): Set<string> {
  const result = new Set<string>();
  const work = Array.isArray(header.remaining_work) ? header.remaining_work : [];
  for (const item of work) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const typed = item as Record<string, unknown>;
    if (typed.kind !== "conflict_resolution" || !Array.isArray(typed.node_keys)) continue;
    for (const key of typed.node_keys) if (typeof key === "string") result.add(key);
  }
  return result;
}

function isVerifiedCatalogNode(node: Record<string, unknown>): boolean {
  return node.mapping_status === "verified" && Boolean(node.source_range);
}

async function assertCatalogReadAllowed(
  tool: "pdf_text" | "pdf_page_image" | "source_range_preview",
  pageStart?: number,
  pageEnd?: number,
): Promise<void> {
  if (inspectionScope !== "catalog_v3") return;
  if (catalogPublished) throw new Error("The catalog turn is closed after snapshot publication");
  const state = await checkpointState();
  const baselineCitableCount = Number.isInteger(state.header.baseline_citable_count)
    ? Number(state.header.baseline_citable_count)
    : 0;
  if (baselineCitableCount === 0 && state.nodes.some(isVerifiedCatalogNode)) {
    throw new Error("The first citable node is ready; publish the first snapshot before any further source read");
  }
  const work = Array.isArray(state.header.remaining_work)
    ? state.header.remaining_work.filter(
        (item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)),
      )
    : [];
  if (!work.length) return;
  const kinds = new Set(work.map((item) => String(item.kind ?? "")));
  if ([...kinds].every((kind) => kind === "range_mapping")) {
    throw new Error("range_mapping is mechanical; body-page reads are forbidden after exact locators are retained");
  }
  if (tool === "source_range_preview" && !kinds.has("conflict_resolution") && !kinds.has("pagination_calibration")) {
    throw new Error("source_range_preview requires a persisted conflict or pagination-calibration work item");
  }
  if (
    (tool === "pdf_text" || tool === "pdf_page_image") &&
    kinds.has("directory_page_attribution") &&
    !kinds.has("directory_discovery") &&
    !kinds.has("pagination_calibration") &&
    !kinds.has("conflict_resolution")
  ) {
    const allowedRanges = work
      .filter((item) => item.kind === "directory_page_attribution" && Array.isArray(item.page_ranges))
      .flatMap((item) => item.page_ranges as Array<Record<string, unknown>>)
      .filter((range) => Number.isInteger(range.start) && Number.isInteger(range.end));
    const first = pageStart ?? 0;
    const last = pageEnd ?? first;
    if (!allowedRanges.some((range) => first >= Number(range.start) && last <= Number(range.end))) {
      throw new Error("directory_page_attribution may read only its declared directory-page ranges");
    }
  }
}

async function withCatalogMutation<T>(operation: () => Promise<T>): Promise<T> {
  const result = catalogMutationQueue.then(operation, operation);
  catalogMutationQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

function textResult(text: string, details: Record<string, unknown> = {}) {
  return { content: [{ type: "text" as const, text }], details };
}

function normalizedCatalogOperations(raw: unknown): Array<Record<string, unknown>> {
  if (Array.isArray(raw)) return raw as Array<Record<string, unknown>>;
  if (!raw || typeof raw !== "object") return [];
  const shorthand = raw as Record<string, unknown>;
  const operations: Array<Record<string, unknown>> = [];
  for (const op of ["add", "replace"] as const) {
    const nodes = shorthand[op];
    if (nodes === undefined) continue;
    if (!Array.isArray(nodes)) throw new Error(`${op} must be an array of catalog nodes`);
    for (const node of nodes) operations.push({ op, node });
  }
  const removals = shorthand.remove;
  if (removals !== undefined) {
    if (!Array.isArray(removals)) throw new Error("remove must be an array of node keys");
    for (const item of removals) {
      const key = typeof item === "string"
        ? item
        : item && typeof item === "object" && !Array.isArray(item)
          ? (item as Record<string, unknown>).key
          : undefined;
      operations.push({ op: "remove", key });
    }
  }
  return operations;
}

function boundedText(value: string, limit = MAX_TEXT_OUTPUT): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}\n[output truncated by OpenClass]`;
}

async function sourceSha256(): Promise<string> {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(sourcePath)) digest.update(chunk);
  return digest.digest("hex");
}

async function verifiedSourcePath(): Promise<string> {
  const sourceStat = await lstat(sourcePath);
  if (!sourceStat.isFile() || sourceStat.isSymbolicLink()) {
    throw new Error("The staged OpenClass source is not a regular file");
  }
  const resolved = await realpath(sourcePath);
  if (resolved !== sourcePath) throw new Error("The staged OpenClass source changed identity");
  return resolved;
}

function toolPath(name: "pdfinfo" | "pdftotext" | "pdftoppm"): string {
  return join(toolboxBin, name);
}

const PDF_NAVIGATION_SCRIPT = String.raw`
import json, sys
from pypdf import PdfReader
reader = PdfReader(sys.argv[1])
start = max(0, int(sys.argv[2]))
limit = max(1, min(500, int(sys.argv[3])))
result = []
def visit(items, level=1):
    for item in items:
        if isinstance(item, list):
            visit(item, level + 1)
            continue
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            continue
        try:
            page = reader.get_destination_page_number(item) + 1
        except Exception:
            page = None
        result.append({"title": title, "level": level, "page": page})
visit(getattr(reader, "outline", []) or [])
items = result[start:start + limit]
end = start + len(items)
print(json.dumps({
    "page_count": len(reader.pages),
    "total": len(result),
    "start_index": start,
    "end_index": end,
    "complete": end >= len(result),
    "next_start_index": None if end >= len(result) else end,
    "items": items,
}, ensure_ascii=False))
`;

const PDF_TOC_CANDIDATES_SCRIPT = String.raw`
import json, re, sys
from pypdf import PdfReader

reader = PdfReader(sys.argv[1])
marker_re = re.compile(r"(?:table\s+of\s+contents|contents|\u76ee\s*\u5f55|\u76ee\s*\u6b21)", re.I)
entry_re = re.compile(r"(?:\.{2,}|\s{2,})(?:[ivxlcdm]+|\d{1,5})\s*$", re.I)
candidates = []
for index, page in enumerate(reader.pages):
    text = page.extract_text() or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marker_hits = sum(1 for line in lines if marker_re.search(line))
    entry_hits = sum(1 for line in lines if entry_re.search(line))
    score = marker_hits * 20 + min(entry_hits, 20)
    if marker_hits or entry_hits >= 6:
        candidates.append({"page": index + 1, "score": score, "marker_hits": marker_hits, "entry_line_hits": entry_hits})

ranges = []
for item in candidates:
    if ranges and item["page"] == ranges[-1]["end"] + 1:
        ranges[-1]["end"] = item["page"]
        ranges[-1]["max_score"] = max(ranges[-1]["max_score"], item["score"])
        ranges[-1]["marker_hits"] += item["marker_hits"]
        ranges[-1]["entry_line_hits"] += item["entry_line_hits"]
    else:
        ranges.append({"start": item["page"], "end": item["page"], "max_score": item["score"], "marker_hits": item["marker_hits"], "entry_line_hits": item["entry_line_hits"]})
print(json.dumps({"page_count": len(reader.pages), "candidate_ranges": ranges[:64], "truncated": len(ranges) > 64}, ensure_ascii=False))
`;

const EPUB_NAVIGATION_SCRIPT = String.raw`
import json, sys
from pathlib import Path
from openclass_core.services.source_directory_extractor import _extract_epub

path = Path(sys.argv[1])
start = max(0, int(sys.argv[2]))
limit = max(1, min(100, int(sys.argv[3])))
result = _extract_epub(path)
items = []
for candidate in result.candidates[start:start + limit]:
    items.append({
        "key": candidate.local_key,
        "title": candidate.title,
        "number": candidate.number,
        "level": candidate.level,
        "order_index": candidate.order_index,
        "source_locator": candidate.source_locator,
        "mapping_status": candidate.mapping_status,
        "source_range": candidate.source_range.model_dump(mode="json") if candidate.source_range else None,
        "evidence": [item.model_dump(mode="json") for item in candidate.evidence],
        "metadata": candidate.metadata,
    })
end = start + len(items)
print(json.dumps({
    "total": len(result.candidates),
    "start_index": start,
    "end_index": end,
    "complete": end >= len(result.candidates),
    "next_start_index": None if end >= len(result.candidates) else end,
    "items": items,
    "warnings": list(result.warnings),
    "metadata": result.metadata,
}, ensure_ascii=False))
`;

const SOURCE_RANGE_PREVIEW_SCRIPT = String.raw`
import json, sys
from pathlib import Path
from openclass_core.services.source_range_reader import (
    _read_epub_spine,
    _read_pdf_pages,
    _validate_range,
)

path = Path(sys.argv[1])
source_range = json.loads(sys.argv[2])
_validate_range(source_range)
kind = str(source_range.get("kind") or "")
warnings = []
if kind == "epub_spine":
    units = _read_epub_spine(path, source_range)
elif kind == "pdf_pages":
    units, warnings = _read_pdf_pages(path, source_range)
else:
    raise ValueError("source_range_preview currently supports pdf_pages and epub_spine")
text = "\n".join(unit.text for unit in units).strip()
print(json.dumps({
    "readable": bool(text),
    "excerpt": text[:12000],
    "character_count": len(text),
    "unit_count": len(units),
    "warnings": warnings,
}, ensure_ascii=False))
`;

async function runTool(executable: string, args: string[], maxBuffer = MAX_TEXT_OUTPUT * 4) {
  const result = await execFileAsync(executable, args, {
    cwd: workspace,
    encoding: "utf8",
    maxBuffer,
    timeout: 60_000,
    env: { PATH: process.env.PATH ?? "/usr/bin:/bin", LANG: "en_US.UTF-8" },
  });
  return { stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

type ArchiveIndex = {
  visibleEntries: string[];
  rawByVisibleEntry: Map<string, string>;
};

let archiveIndex: ArchiveIndex | null = null;
let repositoryReadablePaths: Set<string> | null = null;

async function repositoryBrokerRequest(endpoint: "tree" | "read", payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (!repositoryBrokerMode) {
    throw new Error("The host repository broker is unavailable for this source task");
  }
  const response = await fetch(`${repositoryBrokerUrl}/${endpoint}`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${repositoryBrokerToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(60_000),
  });
  const raw = await response.text();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`Repository ${endpoint} returned invalid JSON`);
  }
  if (!response.ok) {
    const message = parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? String((parsed as Record<string, unknown>).error ?? `HTTP ${response.status}`)
      : `HTTP ${response.status}`;
    throw new Error(message);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`Repository ${endpoint} returned an invalid response`);
  }
  return parsed as Record<string, unknown>;
}

async function loadRepositoryReadablePaths(): Promise<Set<string>> {
  if (inspectionScope !== "repository" || !repositoryReadablePathsPath) {
    throw new Error("Repository file reading is unavailable for this source task");
  }
  if (repositoryReadablePaths) return repositoryReadablePaths;
  const raw = JSON.parse(await readFile(repositoryReadablePathsPath, "utf8")) as unknown;
  if (
    !Array.isArray(raw) ||
    !raw.length ||
    raw.length > MAX_ARCHIVE_ENTRIES ||
    raw.some((entry) => typeof entry !== "string" || !entry || entry.includes("\\") || entry.includes("\0") || entry.split("/").includes(".."))
  ) {
    throw new Error("The OpenClass repository-readable path manifest is invalid");
  }
  repositoryReadablePaths = new Set(raw as string[]);
  if (repositoryReadablePaths.size !== raw.length) {
    throw new Error("The OpenClass repository-readable path manifest contains duplicates");
  }
  return repositoryReadablePaths;
}

async function loadArchiveEntries(): Promise<ArchiveIndex> {
  if (archiveIndex) return archiveIndex;
  await verifiedSourcePath();
  const { stdout } = await runTool("/usr/bin/unzip", ["-Z1", sourcePath], 4 * 1024 * 1024);
  const rawEntries = stdout.split(/\r?\n/).filter(Boolean);
  if (rawEntries.length > MAX_ARCHIVE_ENTRIES) {
    throw new Error("The source archive contains too many entries for directory inspection");
  }
  const rawByVisibleEntry = new Map<string, string>();
  const repositoryPrefix = `${archivePrefix}/`;
  for (const rawEntry of rawEntries) {
    const visibleEntry = inspectionScope === "repository"
      ? rawEntry.startsWith(repositoryPrefix)
        ? rawEntry.slice(repositoryPrefix.length)
        : ""
      : rawEntry;
    if (!visibleEntry) continue;
    if (rawByVisibleEntry.has(visibleEntry)) {
      throw new Error("The source archive contains duplicate visible entries");
    }
    rawByVisibleEntry.set(visibleEntry, rawEntry);
  }
  archiveIndex = {
    visibleEntries: [...rawByVisibleEntry.keys()],
    rawByVisibleEntry,
  };
  return archiveIndex;
}

function normalizedArchivePrefix(prefix: string | undefined): string {
  const normalized = (prefix ?? "").trim().replace(/^\/+|\/+$/g, "");
  if (normalized.includes("\\") || normalized.includes("\0") || normalized.split("/").includes("..")) {
    throw new Error("archive_list received an unsafe repository prefix");
  }
  return normalized;
}

function filteredArchiveEntries(entries: string[], prefix: string, recursive: boolean): string[] {
  const base = prefix ? `${prefix}/` : "";
  const selected = new Set<string>();
  for (const entry of entries) {
    if (prefix && entry !== prefix && !entry.startsWith(base)) continue;
    if (recursive || inspectionScope !== "repository") {
      selected.add(entry);
      continue;
    }
    if (entry === prefix) {
      selected.add(entry);
      continue;
    }
    const remainder = entry.slice(base.length);
    if (!remainder) continue;
    const separator = remainder.indexOf("/");
    selected.add(separator >= 0 ? `${base}${remainder.slice(0, separator)}/` : `${base}${remainder}`);
  }
  return [...selected].sort((left, right) => left.localeCompare(right));
}

async function atomicJsonWrite(path: string, value: unknown): Promise<Buffer> {
  const bytes = Buffer.from(JSON.stringify(value), "utf8");
  if (bytes.length < 2 || bytes.length > MAX_CATALOG_BYTES) {
    throw new Error("The catalog checkpoint is outside the OpenClass size limit");
  }
  const temporaryPath = join(scratchPath, `.${basename(path)}-${randomUUID()}.tmp`);
  await writeFile(temporaryPath, bytes, { flag: "wx", mode: 0o600 });
  await chmod(temporaryPath, 0o600);
  await rename(temporaryPath, path);
  return bytes;
}

async function readJsonFile(path: string): Promise<unknown> {
  return JSON.parse(await readFile(path, "utf8")) as unknown;
}

async function checkpointState(): Promise<{
  started: boolean;
  header: Record<string, unknown>;
  nodes: Array<Record<string, unknown>>;
}> {
  try {
    const rawHeader = await readJsonFile(catalogHeaderPath);
    const nodes = await readJsonFile(catalogNodesPath);
    if (!rawHeader || typeof rawHeader !== "object" || Array.isArray(rawHeader)) {
      throw new Error("The OpenClass catalog header checkpoint is invalid");
    }
    if (!Array.isArray(nodes) || nodes.some((node) => !node || typeof node !== "object" || Array.isArray(node))) {
      throw new Error("The OpenClass catalog node checkpoint is invalid");
    }
    return {
      started: true,
      header: rawHeader as Record<string, unknown>,
      nodes: nodes as Array<Record<string, unknown>>,
    };
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { started: false, header: {}, nodes: [] };
    }
    throw error;
  }
}

function validateAgentCatalogNodes(nodes: Array<Record<string, unknown>>): void {
  const byKey = new Map<string, Record<string, unknown>>();
  for (const node of nodes) {
    const key = node.key;
    const parentKey = node.parent_key;
    const level = node.level;
    if (typeof key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(key) || byKey.has(key)) {
      throw new Error("A catalog node has an invalid or duplicate key");
    }
    if (parentKey !== null && typeof parentKey !== "string") {
      throw new Error("A catalog parent_key must be a string or null");
    }
    if (typeof node.number !== "string" || typeof node.title !== "string" || !node.title.trim()) {
      throw new Error("A catalog node requires string number and non-empty title fields");
    }
    if (!Number.isInteger(level) || (level as number) < 1) {
      throw new Error("A catalog node requires a positive integer level");
    }
    const mappingStatus = node.mapping_status ?? "unmapped";
    if (mappingStatus !== "unmapped" && mappingStatus !== "verified") {
      throw new Error("A catalog mapping_status must be verified or unmapped");
    }
    if (mappingStatus === "verified" && (!node.source_range || typeof node.source_range !== "object" || Array.isArray(node.source_range))) {
      throw new Error("A node claimed as verified must include a source_range");
    }
    if (inspectionScope === "catalog_v3") {
      const locatorSource = node.locator_source ?? "unmapped";
      if (!["native_navigation", "printed_directory", "authored_navigation", "legacy_range", "unmapped"].includes(String(locatorSource))) {
        throw new Error("An agent_catalog_v3 node has an invalid locator_source");
      }
    }
    byKey.set(key, node);
  }
  for (const node of nodes) {
    const parentKey = node.parent_key as string | null;
    const level = node.level as number;
    if (parentKey === null) {
      if (level !== 1) throw new Error("A root catalog node must use level 1");
      continue;
    }
    const parent = byKey.get(parentKey);
    if (!parent || parent.level !== level - 1) {
      throw new Error("Catalog parents must exist and use the immediately preceding level");
    }
    const seen = new Set<string>();
    let cursor: Record<string, unknown> | undefined = node;
    while (cursor) {
      const cursorKey = cursor.key as string;
      if (seen.has(cursorKey)) throw new Error("The catalog parent graph contains a cycle");
      seen.add(cursorKey);
      const next = cursor.parent_key;
      cursor = typeof next === "string" ? byKey.get(next) : undefined;
    }
  }
}

async function recordToolActivity(activity: Record<string, unknown>): Promise<void> {
  if (inspectionScope !== "catalog_v2" && inspectionScope !== "catalog_v3") return;
  await withCatalogMutation(async () => {
    const state = await checkpointState();
    if (!state.started) return;
    const previous = Array.isArray(state.header.tool_activity) ? state.header.tool_activity : [];
    await atomicJsonWrite(catalogHeaderPath, {
      ...state.header,
      tool_activity: [...previous.slice(-39), activity],
    });
  });
}

function validateCheckpointNodes(existing: Array<Record<string, unknown>>, additions: Array<Record<string, unknown>>): void {
  if (!additions.length || additions.length > 100) {
    throw new Error("catalog_append requires between 1 and 100 directory nodes");
  }
  const levels = new Map<string, number>();
  for (const node of existing) {
    if (typeof node.key === "string" && Number.isInteger(node.level)) {
      levels.set(node.key, node.level as number);
    }
  }
  for (const node of additions) {
    const key = node.key;
    const parentKey = node.parent_key;
    const level = node.level;
    if (typeof key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(key) || levels.has(key)) {
      throw new Error("A checkpoint node has an invalid or duplicate key");
    }
    if (typeof node.title !== "string" || !node.title.trim() || !Number.isInteger(level) || (level as number) < 1) {
      throw new Error("A checkpoint node has an invalid title or level");
    }
    if (parentKey === null) {
      if (level !== 1) throw new Error("A root checkpoint node must use level 1");
    } else {
      if (typeof parentKey !== "string" || levels.get(parentKey) !== (level as number) - 1) {
        throw new Error("Checkpoint nodes must use parent-first contiguous levels");
      }
    }
    levels.set(key, level as number);
  }

}

export function parentConsistentPreorder(nodes: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const children = new Map<string | null, Array<Record<string, unknown>>>();
  for (const node of nodes) {
    const parentKey = node.parent_key as string | null;
    const siblings = children.get(parentKey) ?? [];
    siblings.push(node);
    children.set(parentKey, siblings);
  }
  const ordered: Array<Record<string, unknown>> = [];
  const visit = (node: Record<string, unknown>): void => {
    ordered.push(node);
    for (const child of children.get(node.key as string) ?? []) {
      visit(child);
    }
  };
  for (const root of children.get(null) ?? []) {
    visit(root);
  }
  if (ordered.length !== nodes.length) {
    throw new Error("The Pi-authored directory graph contains an unreachable node");
  }
  return ordered;
}

export default function openClassPiSourceTools(pi: ExtensionAPI) {
  pi.registerTool({
    name: "source_info",
    label: "Source information",
    description: "Return metadata and SHA-256 for the sole staged OpenClass source.",
    parameters: Type.Object({}),
    async execute() {
      await verifiedSourcePath();
      const sourceStat = await stat(sourcePath);
      const suffixMatch = basename(sourcePath)
        .toLowerCase()
        .match(/\.[^.]+$/);
      const suffix = suffixMatch?.[0] ?? "";
      let pdfInfo = "";
      if (suffix === ".pdf") {
        pdfInfo = boundedText((await runTool(toolPath("pdfinfo"), [sourcePath])).stdout, 16_000);
      }
      return textResult(
        JSON.stringify({
          file_name: basename(sourcePath),
          suffix,
          byte_count: sourceStat.size,
          sha256: await sourceSha256(),
          pdf_info: pdfInfo,
          inspection_scope: inspectionScope,
          archive_prefix: archivePrefix || null,
        }),
      );
    },
  });

  pi.registerTool({
    name: "pdf_text",
    label: "Read bounded PDF pages",
    description: `Read layout-preserving text from ${MAX_PDF_PAGE_SPAN} or fewer one-based PDF pages.`,
    parameters: Type.Object({
      first_page: Type.Integer({ minimum: 1 }),
      last_page: Type.Integer({ minimum: 1 }),
    }),
    async execute(_id, params) {
      await verifiedSourcePath();
      await assertCatalogReadAllowed("pdf_text", params.first_page, params.last_page);
      if (params.last_page < params.first_page || params.last_page - params.first_page + 1 > MAX_PDF_PAGE_SPAN) {
        throw new Error(`PDF inspection must cover between 1 and ${MAX_PDF_PAGE_SPAN} pages`);
      }
      const { stdout } = await runTool(toolPath("pdftotext"), [
        "-f",
        String(params.first_page),
        "-l",
        String(params.last_page),
        "-layout",
        "-enc",
        "UTF-8",
        sourcePath,
        "-",
      ]);
      await recordToolActivity({ tool: "pdf_text", first_page: params.first_page, last_page: params.last_page });
      return textResult(boundedText(stdout), {
        first_page: params.first_page,
        last_page: params.last_page,
      });
    },
  });

  if (inspectionScope === "source") {
    pi.registerTool({
      name: "pdf_search",
      label: "Search PDF text",
      description: "Search the PDF text layer for an Agent-chosen phrase and return matching physical pages with short snippets.",
      parameters: Type.Object({
        query: Type.String({ minLength: 1, maxLength: 240 }),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_PDF_SEARCH_MATCHES })),
      }),
      async execute(_id, params) {
        await verifiedSourcePath();
        await assertCatalogReadAllowed("source_range_preview");
        const query = params.query.trim();
        if (!query) throw new Error("pdf_search requires visible query text");
        const { stdout } = await runTool(
          toolPath("pdftotext"),
          ["-layout", "-enc", "UTF-8", sourcePath, "-"],
          64 * 1024 * 1024,
        );
        const normalizedQuery = query.toLocaleLowerCase();
        const limit = params.limit ?? 80;
        const matches: Array<{ page: number; snippet: string }> = [];
        const pages = stdout.split("\f");
        for (let pageIndex = 0; pageIndex < pages.length && matches.length < limit; pageIndex += 1) {
          const lines = pages[pageIndex].split(/\r?\n/);
          for (const line of lines) {
            if (!line.toLocaleLowerCase().includes(normalizedQuery)) continue;
            matches.push({ page: pageIndex + 1, snippet: line.trim().slice(0, 500) });
            if (matches.length >= limit) break;
          }
        }
        return textResult(JSON.stringify({ query, matches, truncated: matches.length >= limit }), {
          query,
          match_count: matches.length,
        });
      },
    });
  }

  if (inspectionScope === "catalog_v3") {
    pi.registerTool({
      name: "epub_navigation",
      label: "Read native EPUB navigation",
      description: "Read one bounded page of authored EPUB NCX/nav entries together with mechanically resolved spine and fragment ranges when available.",
      parameters: Type.Object({
        start_index: Type.Optional(Type.Integer({ minimum: 0 })),
        limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
      }),
      async execute(_id, params) {
        await verifiedSourcePath();
        const startIndex = params.start_index ?? 0;
        const limit = params.limit ?? 100;
        const { stdout } = await runTool(
          pythonBin,
          ["-c", EPUB_NAVIGATION_SCRIPT, sourcePath, String(startIndex), String(limit)],
          16 * 1024 * 1024,
        );
        const result = JSON.parse(stdout) as { total?: number; items?: unknown[]; complete?: boolean };
        await recordToolActivity({
          tool: "epub_navigation",
          start_index: startIndex,
          item_count: Array.isArray(result.items) ? result.items.length : 0,
          total: result.total ?? null,
          complete: result.complete ?? false,
        });
        return textResult(boundedText(JSON.stringify(result)), {
          start_index: startIndex,
          item_count: Array.isArray(result.items) ? result.items.length : 0,
          total: result.total ?? null,
          complete: result.complete ?? false,
        });
      },
    });

    pi.registerTool({
      name: "source_range_preview",
      label: "Preview a proposed source range",
      description: "Read a bounded excerpt from an Agent-proposed pdf_pages or epub_spine range so the Agent can judge whether it supports a catalog node.",
      parameters: Type.Object({
        source_range_json: Type.String({ minLength: 2, maxLength: 64 * 1024 }),
      }),
      async execute(_id, params) {
        await verifiedSourcePath();
        const parsed = JSON.parse(params.source_range_json) as unknown;
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("source_range_json must contain one range object");
        }
        const compact = JSON.stringify(parsed);
        const { stdout } = await runTool(
          pythonBin,
          ["-c", SOURCE_RANGE_PREVIEW_SCRIPT, sourcePath, compact],
          16 * 1024 * 1024,
        );
        const result = JSON.parse(stdout) as { readable?: boolean; character_count?: number; unit_count?: number };
        await recordToolActivity({
          tool: "source_range_preview",
          range_kind: String((parsed as Record<string, unknown>).kind ?? ""),
          readable: result.readable ?? false,
          character_count: result.character_count ?? 0,
          unit_count: result.unit_count ?? 0,
        });
        return textResult(boundedText(JSON.stringify(result)), {
          readable: result.readable ?? false,
          character_count: result.character_count ?? 0,
          unit_count: result.unit_count ?? 0,
        });
      },
    });

    pi.registerTool({
      name: "pdf_p_calculate",
      label: "Calculate a PDF pagination offset",
      description: "Calculate P = PDF file page - printed page + 1 for Agent-selected anchors and report whether they agree. The Agent decides how to use the result.",
      parameters: Type.Object({
        anchors: Type.Array(
          Type.Object({
            pdf_file_page: Type.Integer({ minimum: 1 }),
            printed_page: Type.Integer({ minimum: 1 }),
          }),
          { minItems: 1, maxItems: 32 },
        ),
      }),
      async execute(_id, params) {
        const values = params.anchors.map((anchor) => anchor.pdf_file_page - anchor.printed_page + 1);
        const uniqueValues = [...new Set(values)];
        const result = {
          consistent: uniqueValues.length === 1,
          page_offset_p: uniqueValues.length === 1 ? uniqueValues[0] : null,
          values,
          anchors: params.anchors,
        };
        await recordToolActivity({
          tool: "pdf_p_calculate",
          anchor_count: params.anchors.length,
          consistent: result.consistent,
          page_offset_p: result.page_offset_p,
        });
        return textResult(JSON.stringify(result), result);
      },
    });

    pi.registerTool({
      name: "pdf_toc_candidates",
      label: "Locate directory-page candidates",
      description: "Mechanically score likely printed-directory pages and return only page ranges and signals, never body text.",
      parameters: Type.Object({}),
      async execute() {
        await verifiedSourcePath();
        const { stdout } = await runTool(pythonBin, ["-c", PDF_TOC_CANDIDATES_SCRIPT, sourcePath], 8 * 1024 * 1024);
        const result = JSON.parse(stdout) as { candidate_ranges?: unknown[] };
        const rangeCount = Array.isArray(result.candidate_ranges) ? result.candidate_ranges.length : 0;
        await recordToolActivity({ tool: "pdf_toc_candidates", candidate_range_count: rangeCount });
        return textResult(boundedText(JSON.stringify(result)), { candidate_range_count: rangeCount });
      },
    });
  }

  pi.registerTool({
    name: "pdf_navigation",
    label: "Read native PDF navigation",
    description: "Read a bounded source-order page of native PDF outline/bookmark titles, levels, and physical destinations when present.",
    parameters: Type.Object({
      start_index: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500 })),
    }),
    async execute(_id, params) {
      await verifiedSourcePath();
      const startIndex = params.start_index ?? 0;
      const limit = params.limit ?? 500;
      const { stdout } = await runTool(
        pythonBin,
        ["-c", PDF_NAVIGATION_SCRIPT, sourcePath, String(startIndex), String(limit)],
        8 * 1024 * 1024,
      );
      const navigation = JSON.parse(stdout) as { items?: unknown[]; total?: number; next_start_index?: number | null };
      const itemCount = Array.isArray(navigation.items) ? navigation.items.length : 0;
      await recordToolActivity({
        tool: "pdf_navigation",
        item_count: itemCount,
        start_index: startIndex,
        total: navigation.total ?? itemCount,
      });
      return textResult(boundedText(JSON.stringify(navigation)), {
        item_count: itemCount,
        start_index: startIndex,
        total: navigation.total ?? itemCount,
        next_start_index: navigation.next_start_index ?? null,
      });
    },
  });

  pi.registerTool({
    name: "pdf_page_image",
    label: "Render one PDF page",
    description:
      "Render one one-based PDF page as a PNG for inspection by the selected cloud vision model when page text is empty or unusable. This tool performs no OCR or text recognition. Prefer pdf_text first. Do not use for EPUB or text-layer pages that already yield readable text.",
    parameters: Type.Object({ page: Type.Integer({ minimum: 1 }) }),
    async execute(_id, params) {
      await verifiedSourcePath();
      await assertCatalogReadAllowed("pdf_page_image", params.page, params.page);
      const prefix = join(scratchPath, `page-${params.page}-${randomUUID()}`);
      const imagePath = `${prefix}.png`;
      try {
        await runTool(
          toolPath("pdftoppm"),
          ["-f", String(params.page), "-l", String(params.page), "-singlefile", "-scale-to", "1800", "-png", sourcePath, prefix],
          16 * 1024 * 1024,
        );
        const image = await readFile(imagePath);
        if (!image.length || image.length > 12 * 1024 * 1024) {
          throw new Error("Rendered PDF page is empty or exceeds the OpenClass image limit");
        }
        await recordToolActivity({ tool: "pdf_page_image", page: params.page });
        return {
          content: [
            {
              type: "text" as const,
              text: `Rendered physical PDF page ${params.page}.`,
            },
            {
              type: "image" as const,
              data: image.toString("base64"),
              mimeType: "image/png",
            },
          ],
          details: { page: params.page },
        };
      } finally {
        await rm(imagePath, { force: true });
      }
    },
  });

  pi.registerTool({
    name: "archive_list",
    label: "List source archive entries",
    description: "List a bounded page of entries in the sole staged source archive without extracting it. Repository inspection supports non-recursive prefix navigation.",
    parameters: Type.Object({
      prefix: Type.Optional(Type.String({ maxLength: 2_048 })),
      recursive: Type.Optional(Type.Boolean()),
      start_index: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_ARCHIVE_ENTRIES })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_ARCHIVE_LIST_PAGE_ENTRIES })),
    }),
    async execute(_id, params) {
      const index = await loadArchiveEntries();
      const prefix = normalizedArchivePrefix(params.prefix);
      const recursive = params.recursive ?? inspectionScope !== "repository";
      const entries = filteredArchiveEntries(index.visibleEntries, prefix, recursive);
      const startIndex = params.start_index ?? 0;
      const limit = params.limit ?? 500;
      if (startIndex > entries.length) {
        throw new Error("archive_list start_index falls beyond the filtered archive entries");
      }
      const page: string[] = [];
      let endIndex = startIndex;
      let characterCount = 0;
      while (endIndex < entries.length && page.length < limit) {
        const entry = entries[endIndex];
        const nextCharacters = entry.length + (page.length ? 1 : 0);
        if (page.length && characterCount + nextCharacters > MAX_TEXT_OUTPUT) break;
        page.push(entry);
        characterCount += nextCharacters;
        endIndex += 1;
      }
      const complete = endIndex >= entries.length;
      return textResult(page.join("\n"), {
        prefix,
        recursive,
        total_entry_count: index.visibleEntries.length,
        filtered_entry_count: entries.length,
        start_index: startIndex,
        end_index: endIndex,
        complete,
        next_start_index: complete ? null : endIndex,
      });
    },
  });

  pi.registerTool({
    name: "archive_read",
    label: "Read one source archive entry",
    description: "Read one bounded character segment from an exact archive entry. Continue from next_start_character until complete=true.",
    parameters: Type.Object({
      entry: Type.String({ minLength: 1, maxLength: 2_048 }),
      start_character: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_ARCHIVE_ENTRY_BUFFER_BYTES })),
    }),
    async execute(_id, params) {
      const index = await loadArchiveEntries();
      const rawEntry = index.rawByVisibleEntry.get(params.entry);
      if (!rawEntry) throw new Error("The requested archive entry does not exist");
      if (inspectionScope === "repository" && !(await loadRepositoryReadablePaths()).has(params.entry)) {
        throw new Error("The requested repository entry is not a verified readable regular file");
      }
      const { stdout } = await runTool("/usr/bin/unzip", ["-p", sourcePath, rawEntry], MAX_ARCHIVE_ENTRY_BUFFER_BYTES);
      const startCharacter = params.start_character ?? 0;
      if (startCharacter > stdout.length) {
        throw new Error("archive_read start_character falls beyond the decoded archive entry");
      }
      const endCharacter = Math.min(stdout.length, startCharacter + MAX_ARCHIVE_ENTRY_CHARACTERS);
      const complete = endCharacter >= stdout.length;
      return textResult(stdout.slice(startCharacter, endCharacter), {
        entry: params.entry,
        start_character: startCharacter,
        end_character: endCharacter,
        total_character_count: stdout.length,
        complete,
        next_start_character: complete ? null : endCharacter,
      });
    },
  });

  pi.registerTool({
    name: "repository_tree",
    label: "Navigate immutable repository tree",
    description: "List a bounded page of readable files or child directories from the repository's fixed commit.",
    parameters: Type.Object({
      prefix: Type.Optional(Type.String({ maxLength: 2_048 })),
      recursive: Type.Optional(Type.Boolean()),
      start_index: Type.Optional(Type.Integer({ minimum: 0, maximum: MAX_ARCHIVE_ENTRIES })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: MAX_ARCHIVE_LIST_PAGE_ENTRIES })),
    }),
    async execute(_id, params) {
      if (inspectionScope !== "repository" || !repositoryBrokerMode) {
        throw new Error("repository_tree is available only for host-brokered repository inspection");
      }
      const result = await repositoryBrokerRequest("tree", {
        prefix: params.prefix ?? "",
        recursive: params.recursive ?? false,
        start_index: params.start_index ?? 0,
        limit: params.limit ?? 500,
      });
      await recordToolActivity({
        tool: "repository_tree",
        prefix: params.prefix ?? "",
        recursive: params.recursive ?? false,
        returned_entry_count: Array.isArray(result.entries) ? result.entries.length : 0,
      });
      return textResult(JSON.stringify(result), result);
    },
  });

  pi.registerTool({
    name: "repository_read",
    label: "Read verified repository lines",
    description: `Read at most ${MAX_TEXT_LINES_PER_CALL} line-numbered lines from one verified readable regular repository file.`,
    parameters: Type.Object({
      entry: Type.String({ minLength: 1, maxLength: 2_048 }),
      start_line: Type.Integer({ minimum: 1, maximum: 10_000_000 }),
      end_line: Type.Integer({ minimum: 1, maximum: 10_000_000 }),
    }),
    async execute(_id, params) {
      if (inspectionScope !== "repository") {
        throw new Error("repository_read is available only for repository inspection");
      }
      if (params.end_line < params.start_line || params.end_line - params.start_line + 1 > MAX_TEXT_LINES_PER_CALL) {
        throw new Error(`Repository inspection must cover between 1 and ${MAX_TEXT_LINES_PER_CALL} lines`);
      }
      if (repositoryBrokerMode) {
        const result = await repositoryBrokerRequest("read", {
          entry: params.entry,
          start_line: params.start_line,
          end_line: params.end_line,
        });
        await recordToolActivity({
          tool: "repository_read",
          entry: params.entry,
          start_line: params.start_line,
          end_line: result.end_line,
        });
        return textResult(String(result.content ?? ""), result);
      }
      const readable = await loadRepositoryReadablePaths();
      if (!readable.has(params.entry)) {
        throw new Error("The requested repository entry is not a verified readable regular file");
      }
      const index = await loadArchiveEntries();
      const rawEntry = index.rawByVisibleEntry.get(params.entry);
      if (!rawEntry) throw new Error("The requested repository entry does not exist");
      const { stdout } = await runTool("/usr/bin/unzip", ["-p", sourcePath, rawEntry], MAX_ARCHIVE_ENTRY_BUFFER_BYTES);
      const lines = stdout.split(/\r\n|\n|\r/);
      if (lines.at(-1) === "") lines.pop();
      if (!lines.length) lines.push("");
      if (params.start_line > lines.length) {
        throw new Error("repository_read start_line falls beyond the verified file boundary");
      }
      const actualEnd = Math.min(params.end_line, lines.length);
      const selected = lines
        .slice(params.start_line - 1, actualEnd)
        .map((line, index) => `${params.start_line + index}: ${line}`)
        .join("\n");
      return textResult(boundedText(selected), {
        entry: params.entry,
        start_line: params.start_line,
        end_line: actualEnd,
        total_line_count: lines.length,
      });
    },
  });

  pi.registerTool({
    name: "text_read",
    label: "Read bounded source lines",
    description: `Read at most ${MAX_TEXT_LINES_PER_CALL} lines from a plain-text source.`,
    parameters: Type.Object({
      start_line: Type.Integer({ minimum: 1 }),
      end_line: Type.Integer({ minimum: 1 }),
    }),
    async execute(_id, params) {
      await verifiedSourcePath();
      if (params.end_line < params.start_line || params.end_line - params.start_line + 1 > MAX_TEXT_LINES_PER_CALL) {
        throw new Error(`Text inspection must cover between 1 and ${MAX_TEXT_LINES_PER_CALL} lines`);
      }
      const lines: string[] = [];
      let lineNumber = 0;
      const reader = createInterface({
        input: createReadStream(sourcePath, { encoding: "utf8" }),
        crlfDelay: Infinity,
      });
      for await (const line of reader) {
        lineNumber += 1;
        if (lineNumber >= params.start_line) lines.push(line);
        if (lineNumber >= params.end_line) break;
      }
      return textResult(boundedText(lines.join("\n")), {
        start_line: params.start_line,
        end_line: Math.min(params.end_line, lineNumber),
      });
    },
  });

  pi.registerTool({
    name: "catalog_status",
    label: "Read catalog checkpoint status",
    description: "Report whether a resumable catalog checkpoint exists and which nodes are already saved.",
    parameters: Type.Object({}),
    async execute() {
      const state = await checkpointState();
      const nodesByKey = new Map<string, Record<string, unknown>>();
      for (const node of state.nodes) {
        if (typeof node.key === "string") nodesByKey.set(node.key, node);
      }
      const openAncestorChain: Array<Record<string, unknown>> = [];
      const seenAncestorKeys = new Set<string>();
      let ancestorCursor = state.nodes.at(-1);
      while (ancestorCursor && openAncestorChain.length < 64) {
        const key = ancestorCursor.key;
        if (typeof key !== "string" || seenAncestorKeys.has(key)) break;
        seenAncestorKeys.add(key);
        openAncestorChain.unshift({
          key,
          parent_key: ancestorCursor.parent_key ?? null,
          title: ancestorCursor.title ?? "",
          level: ancestorCursor.level ?? null,
        });
        const parentKey = ancestorCursor.parent_key;
        ancestorCursor = typeof parentKey === "string" ? nodesByKey.get(parentKey) : undefined;
      }
      return textResult(
        JSON.stringify({
          started: state.started,
          node_count: state.nodes.length,
          last_keys: state.nodes.slice(-8).map((node) => node.key),
          last_nodes: state.nodes.slice(-8).map((node) => ({
            key: node.key,
            title: node.title,
            level: node.level,
            source_locator: node.source_locator,
          })),
          open_ancestor_chain: openAncestorChain,
          schema_version: state.header.schema_version ?? null,
          phase: state.header.phase ?? null,
          directory_status: state.header.directory_status ?? null,
          index_status: state.header.index_status ?? null,
          work_state: state.header.work_state ?? null,
          summary: state.header.summary ?? "",
          next_plan: state.header.next_plan ?? "",
          next_action: state.header.next_action ?? "",
          stop_reason: state.header.stop_reason ?? "",
          completion_reason: state.header.completion_reason ?? "",
          directory_gaps: Array.isArray(state.header.directory_gaps) ? state.header.directory_gaps : [],
          remaining_work: Array.isArray(state.header.remaining_work) ? state.header.remaining_work : [],
          snapshot_reason: state.header.snapshot_reason ?? null,
          no_progress_turns: state.header.no_progress_turns ?? 0,
          directory_evidence_count: Array.isArray(state.header.directory_evidence) ? state.header.directory_evidence.length : 0,
          pagination_regime_count: Array.isArray(state.header.pagination_regimes) ? state.header.pagination_regimes.length : 0,
          attempted_action_fingerprints: Array.isArray(state.header.attempted_action_fingerprints)
            ? state.header.attempted_action_fingerprints
            : [],
          revision: state.header.revision ?? 0,
          tool_activity: Array.isArray(state.header.tool_activity) ? state.header.tool_activity.slice(-20) : [],
          pdf: state.header.pdf ?? null,
        }),
      );
    },
  });

  pi.registerTool({
    name: "catalog_apply",
    label: "Revise catalog workspace",
    description: "Atomically add, replace, or remove catalog nodes. operations_json accepts either [{\"op\":\"add\",\"node\":{...}}] or {\"add\":[...],\"replace\":[...],\"remove\":[\"key\"]}. Invalid operations leave the previous workspace unchanged.",
    parameters: Type.Object({
      operations_json: Type.String({ minLength: 2, maxLength: 4 * 1024 * 1024 }),
    }),
    async execute(_id, params) {
      return withCatalogMutation(async () => {
        if (inspectionScope !== "catalog_v2" && inspectionScope !== "catalog_v3") {
          throw new Error("catalog_apply is available only for agent catalog workspaces");
        }
        if (catalogPublished) throw new Error("The catalog turn is closed after snapshot publication");
        const state = await checkpointState();
        if (!state.started || state.header.schema_version !== AGENT_CATALOG_SCHEMA_VERSION) {
          throw new Error(`catalog_apply requires an ${AGENT_CATALOG_SCHEMA_VERSION} workspace`);
        }
        const rawOperations = JSON.parse(params.operations_json) as unknown;
        const operations = normalizedCatalogOperations(rawOperations);
        if (!operations.length || operations.length > 500) {
          throw new Error("operations_json must contain 1-500 catalog operations");
        }
        const next = new Map<string, Record<string, unknown>>(
          state.nodes.map((node) => [String(node.key), node]),
        );
        const conflictKeys = conflictResolutionKeys(state.header);
        for (const rawOperation of operations) {
          if (!rawOperation || typeof rawOperation !== "object" || Array.isArray(rawOperation)) {
            throw new Error("Every catalog operation must be an object");
          }
          const operation = rawOperation as Record<string, unknown>;
          const op = operation.op;
          if (op === "add" || op === "replace") {
            const node = operation.node;
            if (!node || typeof node !== "object" || Array.isArray(node)) {
              throw new Error(`${op} requires one node object`);
            }
            const typedNode = node as Record<string, unknown>;
            const key = typedNode.key;
            if (typeof key !== "string") throw new Error(`${op} requires a node key`);
            if (op === "add" && next.has(key)) throw new Error(`add cannot overwrite existing key ${key}`);
            if (op === "replace" && !next.has(key)) throw new Error(`replace cannot find key ${key}`);
            const previousNode = next.get(key);
            if (
              op === "replace" &&
              previousNode &&
              isVerifiedCatalogNode(previousNode) &&
              !conflictKeys.has(key) &&
              JSON.stringify(previousNode) !== JSON.stringify(typedNode)
            ) {
              throw new Error(`verified node ${key} is frozen until a conflict-resolution work item is published`);
            }
            next.set(key, typedNode);
          } else if (op === "remove") {
            const key = operation.key;
            const previousNode = typeof key === "string" ? next.get(key) : undefined;
            if (previousNode && isVerifiedCatalogNode(previousNode) && !conflictKeys.has(String(key))) {
              throw new Error(`verified node ${String(key)} is frozen until a conflict-resolution work item is published`);
            }
            if (typeof key !== "string" || !next.delete(key)) {
              throw new Error("remove requires an existing key");
            }
          } else {
            throw new Error("A catalog operation op must be add, replace, or remove");
          }
        }
        const nodes = [...next.values()];
        validateAgentCatalogNodes(nodes);
        await atomicJsonWrite(catalogNodesPath, nodes);
        return textResult(JSON.stringify({ applied: operations.length, node_count: nodes.length }));
      });
    },
  });

  pi.registerTool({
    name: "catalog_publish_snapshot",
    label: "Publish catalog snapshot",
    description: "Atomically publish the current usable catalog snapshot. Publishing working does not mean the investigation is complete.",
    parameters: Type.Object({
      work_state: Type.Union([Type.Literal("working"), Type.Literal("paused"), Type.Literal("satisfied"), Type.Literal("partial")]),
      summary: Type.String({ maxLength: 4_096 }),
      next_plan: Type.String({ maxLength: 4_096 }),
      stop_reason: Type.String({ maxLength: 4_096 }),
      v3_state_json: Type.Optional(Type.String({ minLength: 2, maxLength: 4 * 1024 * 1024 })),
    }),
    async execute(_id, params) {
      return withCatalogMutation(async () => {
        if (inspectionScope !== "catalog_v2" && inspectionScope !== "catalog_v3") {
          throw new Error("catalog_publish_snapshot is available only for agent catalog workspaces");
        }
        if (catalogPublished) throw new Error("Only one catalog snapshot may be published per turn");
        const state = await checkpointState();
        if (!state.started || state.header.schema_version !== AGENT_CATALOG_SCHEMA_VERSION) {
          throw new Error(`catalog_publish_snapshot requires an ${AGENT_CATALOG_SCHEMA_VERSION} workspace`);
        }
        validateAgentCatalogNodes(state.nodes);
        const orderedNodes = parentConsistentPreorder(state.nodes);
        const revision = Number.isInteger(state.header.revision) ? Number(state.header.revision) + 1 : 1;
        let v3State: Record<string, unknown> = {};
        if (inspectionScope === "catalog_v3") {
          if (!params.v3_state_json) {
            throw new Error("agent_catalog_v3 publication requires v3_state_json");
          }
          const parsed = JSON.parse(params.v3_state_json) as unknown;
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("v3_state_json must contain one JSON object");
          }
          v3State = parsed as Record<string, unknown>;
          if (v3State.schema_version !== AGENT_CATALOG_SCHEMA_VERSION || v3State.work_state !== params.work_state) {
            throw new Error("v3_state_json schema_version and work_state must match the publication arguments");
          }
          if ("nodes" in v3State) {
            throw new Error("v3_state_json must not contain nodes; publish the retained workspace nodes");
          }
        }
        const header = {
          ...state.header,
          ...v3State,
          schema_version: AGENT_CATALOG_SCHEMA_VERSION,
          work_state: params.work_state,
          summary: params.summary.trim(),
          next_plan: params.next_plan.trim(),
          stop_reason: params.stop_reason.trim(),
          revision,
        };
        await atomicJsonWrite(catalogHeaderPath, header);
        const artifact = inspectionScope === "catalog_v3"
          ? {
              schema_version: AGENT_CATALOG_SCHEMA_VERSION,
              phase: v3State.phase,
              directory_status: v3State.directory_status,
              index_status: v3State.index_status,
              work_state: params.work_state,
              summary: params.summary.trim(),
              next_plan: params.next_plan.trim(),
              next_action: v3State.next_action,
              stop_reason: params.stop_reason.trim(),
              completion_reason: v3State.completion_reason,
              directory_gaps: v3State.directory_gaps,
              remaining_work: v3State.remaining_work ?? [],
              snapshot_reason: v3State.snapshot_reason ?? "budget_increment",
              progress_fingerprint: v3State.progress_fingerprint ?? "",
              no_progress_turns: v3State.no_progress_turns ?? 0,
              directory_evidence: v3State.directory_evidence,
              directory_page_ranges: v3State.directory_page_ranges,
              pagination_regimes: v3State.pagination_regimes,
              attempted_action_fingerprints: v3State.attempted_action_fingerprints,
              nodes: orderedNodes,
            }
          : {
              schema_version: AGENT_CATALOG_SCHEMA_VERSION,
              work_state: params.work_state,
              summary: params.summary.trim(),
              next_plan: params.next_plan.trim(),
              stop_reason: params.stop_reason.trim(),
              nodes: orderedNodes,
            };
        const bytes = await atomicJsonWrite(catalogPath, artifact);
        const receipt = {
          artifact_path: "scratch/catalog.json",
          sha256: createHash("sha256").update(bytes).digest("hex"),
          byte_count: bytes.length,
          node_count: orderedNodes.length,
          revision,
          work_state: params.work_state,
        };
        await atomicJsonWrite(catalogReceiptPath, receipt);
        catalogPublished = true;
        return textResult(JSON.stringify(receipt), receipt);
      });
    },
  });

  pi.registerTool({
    name: "catalog_start",
    label: "Start catalog checkpoint",
    description: "Start a resumable catalog. Pass the validated PDF task object, or null for a non-PDF source.",
    parameters: Type.Object({
      pdf_json: Type.String({ minLength: 4, maxLength: 16_000 }),
    }),
    async execute(_id, params) {
      return withCatalogMutation(async () => {
        if (inspectionScope === "catalog_v2" || inspectionScope === "catalog_v3") {
          throw new Error("catalog_start is not available in agent catalog workspaces; resume the seeded workspace");
        }
        const state = await checkpointState();
        if (state.started && state.nodes.length) {
          throw new Error("A non-empty catalog checkpoint already exists; resume it instead of restarting");
        }
        const pdf = JSON.parse(params.pdf_json) as unknown;
        if (pdf !== null && (!pdf || typeof pdf !== "object" || Array.isArray(pdf))) {
          throw new Error("pdf_json must contain one PDF task object or null");
        }
        await atomicJsonWrite(catalogHeaderPath, { pdf });
        await atomicJsonWrite(catalogNodesPath, []);
        return textResult(JSON.stringify({ started: true, node_count: 0 }));
      });
    },
  });

  pi.registerTool({
    name: "catalog_append",
    label: "Append catalog checkpoint nodes",
    description: "Append 1-100 complete Pi-authored directory nodes in parent-before-child order. Final submission mechanically orders the unchanged parent graph.",
    parameters: Type.Object({
      nodes_json: Type.String({ minLength: 4, maxLength: 4 * 1024 * 1024 }),
    }),
    async execute(_id, params) {
      return withCatalogMutation(async () => {
        if (inspectionScope === "catalog_v2" || inspectionScope === "catalog_v3") {
          throw new Error("catalog_append is not available in agent catalog workspaces; use catalog_apply");
        }
        const state = await checkpointState();
        if (!state.started) throw new Error("Call catalog_start before appending nodes");
        const additions = JSON.parse(params.nodes_json) as unknown;
        if (!Array.isArray(additions) || additions.some((node) => !node || typeof node !== "object" || Array.isArray(node))) {
          throw new Error("nodes_json must contain one JSON array of directory node objects");
        }
        const typedAdditions = additions as Array<Record<string, unknown>>;
        validateCheckpointNodes(state.nodes, typedAdditions);
        const nodes = [...state.nodes, ...typedAdditions];
        await atomicJsonWrite(catalogNodesPath, nodes);
        return textResult(
          JSON.stringify({
            appended: typedAdditions.length,
            node_count: nodes.length,
            last_key: nodes.at(-1)?.key ?? null,
          }),
        );
      });
    },
  });

  pi.registerTool({
    name: "write_catalog",
    label: "Submit source directory catalog",
    description: "Assemble the resumable checkpoint and atomically write the final OpenClass directory catalog artifact.",
    parameters: Type.Object({}),
    async execute() {
      return withCatalogMutation(async () => {
        if (inspectionScope === "catalog_v2" || inspectionScope === "catalog_v3") {
          throw new Error("write_catalog is not available in agent catalog workspaces; use catalog_publish_snapshot");
        }
        const state = await checkpointState();
        if (!state.started || !state.nodes.length) {
          throw new Error("A non-empty catalog checkpoint is required before final submission");
        }
        const orderedNodes = parentConsistentPreorder(state.nodes);
        const artifact = inspectionScope === "directory_only" ? { complete: true, pdf: state.header.pdf ?? null, nodes: orderedNodes } : { complete: true, nodes: orderedNodes };
        const bytes = await atomicJsonWrite(catalogPath, artifact);
        const receipt = {
          artifact_path: "scratch/catalog.json",
          sha256: createHash("sha256").update(bytes).digest("hex"),
          byte_count: bytes.length,
          node_count: orderedNodes.length,
        };
        return textResult(JSON.stringify(receipt), receipt);
      });
    },
  });
}
