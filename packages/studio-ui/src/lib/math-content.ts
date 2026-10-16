import type { Editor as TiptapEditor } from "@tiptap/core";
import type { EditorState } from "@tiptap/pm/state";
import type { Mark, Node as ProseMirrorNode, Schema } from "@tiptap/pm/model";

import {
  formulaOnlyLatex,
  hasStrongMathSignal,
  inlineMathSegments,
  normalizeLatex,
  stripOrphanMathDollars,
} from "./latex-fragments";

function topLevelBlockMathDelimiterReplacements(doc: ProseMirrorNode, blockMath: Schema["nodes"][string]) {
  const children: Array<{ node: ProseMirrorNode; offset: number; index: number }> = [];
  doc.forEach((node, offset, index) => {
    children.push({ node, offset, index });
  });

  const replacements: Array<{ pos: number; size: number; latex: string }> = [];
  let cursor = 0;
  while (cursor < children.length) {
    const openerText = children[cursor].node.type.name === "paragraph" ? children[cursor].node.textContent.trim() : "";
    const closerText = openerText === "\\[" ? "\\]" : openerText === "$$" ? "$$" : null;
    if (!closerText) {
      cursor += 1;
      continue;
    }

    const formulaParts: string[] = [];
    let closerIndex = -1;
    for (let index = cursor + 1; index < children.length; index += 1) {
      const current = children[index];
      const text = current.node.type.name === "paragraph" ? current.node.textContent.trim() : "";
      if (text === closerText) {
        closerIndex = index;
        break;
      }
      formulaParts.push(current.node.textContent);
    }

    const latex = normalizeLatex(formulaParts.join("\n"));
    if (
      closerIndex > cursor + 1 &&
      latex &&
      hasStrongMathSignal(latex) &&
      doc.canReplaceWith(children[cursor].index, children[closerIndex].index + 1, blockMath)
    ) {
      const start = children[cursor].offset;
      const end = children[closerIndex].offset + children[closerIndex].node.nodeSize;
      replacements.push({ pos: start, size: end - start, latex });
      cursor = closerIndex + 1;
      continue;
    }

    cursor += 1;
  }

  return replacements;
}

function nodesForTextWithMath(schema: Schema, text: string, marks: readonly Mark[]) {
  const normalizedText = stripOrphanMathDollars(text);
  const segments = inlineMathSegments(normalizedText);

  if (!segments.length && normalizedText === text) {
    return null;
  }

  const nodes: ProseMirrorNode[] = [];
  let cursor = 0;

  for (const segment of segments) {
    if (segment.start > cursor) {
      nodes.push(schema.text(normalizedText.slice(cursor, segment.start), marks));
    }
    nodes.push(schema.nodes.inlineMath.create({ latex: segment.latex }));
    cursor = segment.end;
  }

  if (cursor < normalizedText.length) {
    nodes.push(schema.text(normalizedText.slice(cursor), marks));
  }

  return nodes;
}

export function editorMathTransaction(state: EditorState, preciseInlineRanges = false) {
  const { blockMath, inlineMath } = state.schema.nodes;

  if (!blockMath || !inlineMath) {
    return;
  }

  let tr = state.tr;
  let changed = false;

  const delimitedBlockReplacements = topLevelBlockMathDelimiterReplacements(tr.doc, blockMath);
  for (const replacement of delimitedBlockReplacements.reverse()) {
    tr = tr.replaceWith(
      replacement.pos,
      replacement.pos + replacement.size,
      blockMath.create({ latex: replacement.latex })
    );
    changed = true;
  }

  const blockReplacements: Array<{ pos: number; size: number; latex: string }> = [];

  tr.doc.descendants((node, pos, parent, index) => {
    if (node.type.name !== "paragraph" || parent?.type.name !== "doc") {
      return;
    }

    const latex = formulaOnlyLatex(node.textContent);
    if (!latex || !parent.canReplaceWith(index, index + 1, blockMath)) {
      return;
    }

    blockReplacements.push({ pos, size: node.nodeSize, latex });
  });

  for (const replacement of blockReplacements.reverse()) {
    tr = tr.replaceWith(
      replacement.pos,
      replacement.pos + replacement.size,
      blockMath.create({ latex: replacement.latex })
    );
    changed = true;
  }

  const inlineReplacements: Array<{ pos: number; size: number; nodes: ProseMirrorNode[]; uncertain?: boolean }> = [];

  tr.doc.descendants((node, pos, parent) => {
    if (!node.isText || !node.text || parent?.type.name === "codeBlock") {
      return;
    }

    if (preciseInlineRanges && stripOrphanMathDollars(node.text) === node.text) {
      for (const segment of inlineMathSegments(node.text)) inlineReplacements.push({
        pos: pos + segment.start, size: segment.end - segment.start,
        nodes: [state.schema.nodes.inlineMath.create({ latex: segment.latex })],
      });
      return;
    }
    const nodes = nodesForTextWithMath(state.schema, node.text, node.marks);
    if (!nodes) {
      return;
    }

    inlineReplacements.push({ pos, size: node.nodeSize, nodes, uncertain: preciseInlineRanges });
  });

  for (const replacement of inlineReplacements.reverse()) {
    if (replacement.uncertain) tr.setMeta("historyRangeUncertainSteps", [...(tr.getMeta("historyRangeUncertainSteps") ?? []), tr.steps.length]);
    tr = tr.replaceWith(replacement.pos, replacement.pos + replacement.size, replacement.nodes);
    changed = true;
  }

  if (!changed) {
    return;
  }

  tr.setMeta("addToHistory", false);
  tr.setMeta("preventUpdate", true);
  return tr;
}

export function normalizeEditorMath(editor: TiptapEditor) {
  const transaction = editorMathTransaction(editor.state);
  if (transaction) editor.view.dispatch(transaction);
}

export const MATH_TEXT_SERIALIZERS = {
  inlineMath: ({ node }: { node: ProseMirrorNode }) => `$${node.attrs.latex ?? ""}$`,
  blockMath: ({ node }: { node: ProseMirrorNode }) => `$$\n${node.attrs.latex ?? ""}\n$$`,
};
