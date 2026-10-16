import type { Node as ProseMirrorNode } from "@tiptap/pm/model";

export type CitationPageKind = "pdf" | "source" | "page";
export type CitationReferenceMember = { from: number; to: number; label: string; anchorId: string };
export type CitationReferenceGroup = {
  key: string;
  identity: string;
  pageKind: CitationPageKind | null;
  pageStart: number | null;
  pageEnd: number | null;
  members: CitationReferenceMember[];
};

/** Parse only an explicit final page locator; never infer a source or chapter. */
export function parseCitationPageReference(text: string) {
  const value = text.trim().replace(/\s+/g, " ");
  const english = /^(.*?)\s*(?:(PDF)\s*(?:pages?|pp?\.?)|(source)\s+pages?|(?:pp?\.|pages?))\s*(\d+)(?:\s*[-–—~～]\s*(\d+))?\s*[.。]?$/i.exec(value);
  const splitChinese = english ? null : /^(.*?)\s*(?:(PDF)\s*)?(?:第\s*)?(\d+)\s*页\s*[-–—~～]\s*(?:第\s*)?(\d+)\s*页\s*[.。]?$/i.exec(value);
  const chinese = english || splitChinese ? null : /^(.*?)\s*(?:(PDF)\s*)?(?:第\s*)?(\d+)\s*(?:[-–—~～]\s*(?:第\s*)?(\d+)\s*)?页\s*[.。]?$/i.exec(value);
  const match = english ?? splitChinese ?? chinese;
  if (!match) return null;
  const identity = match[1].trim().replace(/[\s;,，；·:：]+$/, "");
  const start = Number(english ? match[4] : match[3]);
  const end = Number((english ? match[5] : match[4]) || start);
  if (!identity || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return null;
  const pageKind: CitationPageKind = match[2] ? "pdf" : english && match[3] ? "source" : "page";
  return { identity, pageKind, start, end };
}

export function citationGroupRange(group: CitationReferenceGroup) {
  if (group.pageKind === null || group.pageStart === null || group.pageEnd === null) return "";
  const unit = group.pageKind === "pdf" ? "PDF pages" : group.pageKind === "source" ? "source pages" : "pages";
  const span = group.pageStart === group.pageEnd ? String(group.pageStart) : `${group.pageStart}–${group.pageEnd}`;
  return `${unit} ${span}`;
}

/** A quoted work plus its exact bibliographic prefix is a display identity.
 * Keep authors/edition intact; chapter, formula and figure details stay in the
 * original entries. Unquoted or ambiguous wording retains the page-only path.
 */
function parseCitationWorkReference(text: string) {
  const value = text.trim().replace(/\s+/g, " ");
  const title = /《[^《》]+》/.exec(value);
  if (!title) return null;
  const titleEnd = title.index + title[0].length;
  const tail = value.slice(titleEnd);
  const locator = /[,，;；]\s*(?:第\s*[\d零一二三四五六七八九十百]+(?:[.．\-–—]\d+)*\s*[章节篇卷]|(?:chapter|section|appendix|figure|equation)\b|§)/i.exec(tail);
  if (!locator) return null;
  const publication = tail.slice(0, locator.index)
    .replace(/[,，;；]\s*(?:PDF|EPUB|DOCX|PPTX|ODT|ODP|XLSX)\s*(?=[,，;；]|$)/gi, "")
    .replace(/[\s,，;；]+$/, "");
  return `${value.slice(0, titleEnd)}${publication}`;
}

export function buildCitationReferenceGroups(doc: ProseMirrorNode): CitationReferenceGroup[] {
  const groups: CitationReferenceGroup[] = [];
  let runIndex = 0;
  let inRun = false;
  let run = new Map<string, CitationReferenceGroup>();
  const flush = () => {
    groups.push(...Array.from(run.values()).filter((group) => group.members.length > 1));
    run = new Map();
    inRun = false;
  };
  doc.forEach((node, pos) => {
    if (node.type.name !== "citationReference") {
      if (inRun) flush();
      return;
    }
    if (!inRun) { runIndex += 1; inRun = true; }
    const work = parseCitationWorkReference(node.textContent);
    const parsed = work ? { identity: work, pageKind: null, start: null, end: null }
      : parseCitationPageReference(node.textContent);
    const label = String(node.attrs.label || "");
    if (!parsed || !label) return;
    // A known work retains its author/edition identity. Otherwise exact
    // non-page wording and PDF/source coordinates keep the legacy grouping.
    const links = new Set<string>();
    node.descendants((child) => {
      for (const mark of child.marks) {
        if (mark.type.name === "link" && typeof mark.attrs.href === "string") links.add(mark.attrs.href);
      }
    });
    const key = JSON.stringify([runIndex, parsed.pageKind, parsed.identity, [...links].sort()]);
    const group = run.get(key) ?? { key, identity: parsed.identity, pageKind: parsed.pageKind,
      pageStart: parsed.start, pageEnd: parsed.end, members: [] };
    if (group.pageStart !== null && parsed.start !== null) group.pageStart = Math.min(group.pageStart, parsed.start);
    if (group.pageEnd !== null && parsed.end !== null) group.pageEnd = Math.max(group.pageEnd, parsed.end);
    group.members.push({ from: pos, to: pos + node.nodeSize, label,
      anchorId: String(node.attrs.anchorId || `openclass-ref-${label}`) });
    run.set(key, group);
  });
  if (inRun) flush();
  return groups;
}
