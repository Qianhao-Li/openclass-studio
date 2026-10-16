"use client";

import { NodeViewContent, NodeViewWrapper, type NodeViewProps } from "@tiptap/react";
import clsx from "clsx";
import { Check, Code2, Copy } from "lucide-react";
import { useMemo, useState } from "react";

import { codeLanguageLabel, highlightCode } from "../../lib/code-highlight";
import { writeTextToClipboard } from "../../lib/clipboard";
import { formatCodeIndentation } from "../../lib/code-format";

export function RichCodeBlockView({ node, editor }: NodeViewProps) {
  const language = typeof node.attrs.language === "string" ? node.attrs.language : null;
  const label = codeLanguageLabel(language);
  const code = node.textContent;
  const isEditable = editor.isEditable;
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "error">("idle");
  const displayCode = useMemo(() => formatCodeIndentation(code, language), [code, language]);
  const highlighted = useMemo(() => highlightCode(displayCode, language), [displayCode, language]);

  async function handleCopy() {
    if (!displayCode.trim()) {
      return;
    }
    const succeeded = await writeTextToClipboard(displayCode);
    setCopyStatus(succeeded ? "copied" : "error");
    window.setTimeout(() => setCopyStatus("idle"), 1600);
  }

  return (
    <NodeViewWrapper
      as="div"
      className="rich-code-block"
      data-language={language || "plaintext"}
      contentEditable={false}
    >
      <div className="rich-code-block__header">
        <div className="rich-code-block__title">
          <Code2 className="h-4 w-4" aria-hidden="true" />
          <span>{label}</span>
        </div>
        <button
          type="button"
          className="rich-code-block__copy"
          aria-label={copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Copy failed" : "Copy code"}
          onClick={() => void handleCopy()}
        >
          {copyStatus === "copied" ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </button>
      </div>
      {isEditable ? (
        <pre className="rich-code-block__pre">
          <code className={clsx("rich-code-block__code", language ? `language-${language}` : null)}>
            <NodeViewContent />
          </code>
        </pre>
      ) : (
        <pre className="rich-code-block__pre">
          <code
            className={clsx("rich-code-block__code hljs", language ? `language-${language}` : null)}
            dangerouslySetInnerHTML={{ __html: highlighted }}
          />
        </pre>
      )}
    </NodeViewWrapper>
  );
}
