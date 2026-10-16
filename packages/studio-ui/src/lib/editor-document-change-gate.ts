import type { Node } from "@tiptap/pm/model";
import type { BoardDocument } from "../types";

export function editorDocumentMatchesJson(document: Node, json: Record<string, unknown>) {
  try { return document.eq(document.type.schema.nodeFromJSON(json)); }
  catch { return false; }
}

/** UI transactions and editor serialization are not learning-content edits. */
export class EditorDocumentChangeGate {
  private documentId: string | null = null;
  private document: Node | null = null;
  private sourceTree: Node | null = null;
  private sourceContent: Pick<BoardDocument, "content_json" | "content_html" | "content_text"> | null = null;

  hydrate(documentId: string, document: Node, source: BoardDocument) {
    // Parent echoes of an emitted edit are not a new external source document.
    if (this.documentId !== documentId || !this.document?.eq(document)) {
      this.sourceTree = document;
      this.sourceContent = { content_json: source.content_json, content_html: source.content_html, content_text: source.content_text };
    }
    this.documentId = documentId;
    this.document = document;
  }

  originalContent(documentId: string, document: Node) {
    return this.documentId === documentId && this.sourceTree?.eq(document) ? this.sourceContent : null;
  }

  accept(documentId: string, docChanged: boolean, document: Node) {
    if (!docChanged || documentId !== this.documentId || !this.document || this.document.eq(document)) return false;
    this.document = document;
    return true;
  }
}
