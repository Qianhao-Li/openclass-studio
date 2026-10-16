import MarkdownIt from "markdown-it";
import type Token from "markdown-it/lib/token.mjs";

import { codeLanguageLabel, highlightCode } from "./code-highlight";
import { classifyFencedBlock } from "./code-fence";
import { formatCodeIndentation } from "./code-format";
import { katex } from "./katex-mhchem";
import { inlineMathSegments, normalizeLatex, stripOrphanMathDollars } from "./latex-fragments";
import { readChatMarkdownLink } from "./chat-markdown-links";

const BLOCK_MATH_PLACEHOLDER = "\uE000BLOCKMATH:{index}\uE001";
const BLOCK_MATH_PLACEHOLDER_RE = /^\uE000BLOCKMATH:\d+\uE001$/;
const INLINE_MATH_PIPE_PLACEHOLDER = "\uE002";
const CHINESE_ORDERED_RE = /^(\d+)、\s+/;
const MARKDOWN_HEADING_RE = /^#{1,6}\s+/;
const MARKDOWN_BULLET_RE = /^[-*]\s+/;
const MARKDOWN_ORDERED_RE = /^\d+[.、]\s+/;
const MARKDOWN_TABLE_ROW_RE = /^\s*\|/;
const MARKDOWN_FENCE_RE = /^```/;
const MARKDOWN_BLOCKQUOTE_RE = /^>\s?/;
const BLOCK_MATH_LINE_RE = /^(\\\[|\$\$)/;
const INLINE_MATH_RE = /\\\[[^\n]*?\\\]|\\\([^\n]*?\\\)|\$\$[^\n]*?\$\$|\$(?!\$)[^\n$]+?\$(?!\$)/g;

export type MarkdownRenderOptions = {
  closeUnclosedFences?: boolean;
};

type MarkdownSurfaceRenderer = {
  renderInline: (value: string) => string;
  renderBlockMath: (latex: string) => string;
  renderCodeBlock: (language: string | null, code: string) => string;
  renderHeading: (level: number, content: string) => string;
  renderParagraph: (content: string) => string;
  renderBlockquote: (content: string) => string;
  renderList: (tag: "ul" | "ol", items: string[]) => string;
  renderTable: (rows: string[][]) => string;
};

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function canOpenInlineMark(value: string, index: number, markerLength: number) {
  const previous = index === 0 ? "" : value[index - 1];
  const next = value[index + markerLength] ?? "";
  if (!next || /\s/.test(next)) {
    return false;
  }
  return !previous || /[\s([{"'，。；;:：、]/.test(previous);
}

function findClosingSingleAsterisk(value: string, startIndex: number) {
  for (let index = startIndex; index < value.length; index += 1) {
    const isSingleAsterisk = value[index] === "*" && value[index + 1] !== "*" && value[index - 1] !== "*";
    if (isSingleAsterisk && !/\s/.test(value[index - 1] ?? "")) {
      return index;
    }
  }
  return -1;
}

function renderInlineWithMath(
  value: string,
  renderMath: (latex: string) => string,
  allowLinks = false,
): string {
  value = stripOrphanMathDollars(value);
  const mathSegments = inlineMathSegments(value);
  const parts: string[] = [];
  let index = 0;
  let mathIndex = 0;

  while (index < value.length) {
    const math = mathSegments[mathIndex];
    if (math && math.start === index) {
      parts.push(renderMath(math.latex));
      index = math.end;
      mathIndex += 1;
      continue;
    }
    if (math && math.start < index) {
      mathIndex += 1;
      continue;
    }

    if (value[index] === "`") {
      const closeIndex = value.indexOf("`", index + 1);
      if (closeIndex > index + 1) {
        parts.push(`<code>${escapeHtml(value.slice(index + 1, closeIndex))}</code>`);
        index = closeIndex + 1;
        continue;
      }
    }

    if (allowLinks && value[index] === "[") {
      const link = readChatMarkdownLink(value, index);
      if (link) {
        parts.push(`<a href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer" class="underline underline-offset-2">${escapeHtml(link.label)}</a>`);
        index = link.end;
        continue;
      }
    }

    if (value.startsWith("**", index) && canOpenInlineMark(value, index, 2)) {
      const closeIndex = value.indexOf("**", index + 2);
      if (closeIndex > index + 2) {
        parts.push(`<strong>${escapeHtml(value.slice(index + 2, closeIndex))}</strong>`);
        index = closeIndex + 2;
        continue;
      }
      parts.push(`<strong>${escapeHtml(value.slice(index + 2))}</strong>`);
      break;
    }

    if (value[index] === "*" && value[index + 1] !== "*" && canOpenInlineMark(value, index, 1)) {
      const closeIndex = findClosingSingleAsterisk(value, index + 1);
      if (closeIndex > index + 1) {
        parts.push(`<em>${escapeHtml(value.slice(index + 1, closeIndex))}</em>`);
        index = closeIndex + 1;
        continue;
      }
      parts.push(`<em>${escapeHtml(value.slice(index + 1))}</em>`);
      break;
    }

    parts.push(escapeHtml(value[index]));
    index += 1;
  }

  return parts.join("");
}

function renderKatexMath(latex: string, displayMode: boolean) {
  const html = katex.renderToString(latex, {
    displayMode,
    throwOnError: false,
    strict: "ignore",
  });
  const className = displayMode
    ? "my-2 block max-w-full overflow-x-auto"
    : "inline-block max-w-full overflow-x-auto align-middle";
  return `<span class="${className}">${html}</span>`;
}

export function renderInlineChatMarkdown(value: string) {
  return renderInlineWithMath(value, (latex) => renderKatexMath(latex, false), true);
}

export function renderInlineMarkdown(value: string): string {
  return renderInlineWithMath(
    value,
    (latex) => `<span data-type="inline-math" data-latex="${escapeHtml(latex)}"></span>`,
  );
}

function isStructuralLine(line: string) {
  const stripped = line.trim();
  if (!stripped) {
    return false;
  }
  return (
    MARKDOWN_HEADING_RE.test(stripped) ||
    MARKDOWN_BULLET_RE.test(stripped) ||
    MARKDOWN_ORDERED_RE.test(stripped) ||
    MARKDOWN_TABLE_ROW_RE.test(stripped) ||
    MARKDOWN_FENCE_RE.test(stripped) ||
    MARKDOWN_BLOCKQUOTE_RE.test(stripped) ||
    BLOCK_MATH_LINE_RE.test(stripped)
  );
}

function normalizeChineseOrderedLists(text: string) {
  return text
    .split(/\r?\n/)
    .map((line) => CHINESE_ORDERED_RE.test(line.trim()) ? line.replace(CHINESE_ORDERED_RE, "$1. ") : line)
    .join("\n");
}

function protectTableMathPipes(text: string) {
  return text
    .split(/\r?\n/)
    .map((line) =>
      MARKDOWN_TABLE_ROW_RE.test(line)
        ? line.replace(INLINE_MATH_RE, (formula) => formula.replaceAll("|", INLINE_MATH_PIPE_PLACEHOLDER))
        : line
    )
    .join("\n");
}

function restoreTableMathPipes(value: string) {
  return value.replaceAll(INLINE_MATH_PIPE_PLACEHOLDER, "|");
}

function extractBlockMathAt(lines: string[], index: number): { latex: string; nextIndex: number } | null {
  const line = lines[index]?.trim() ?? "";
  const delimiters: Array<[string, string]> = [
    ["\\[", "\\]"],
    ["$$", "$$"],
  ];
  for (const [opener, closer] of delimiters) {
    if (line.startsWith(opener) && line.endsWith(closer) && line.length > opener.length + closer.length) {
      return { latex: normalizeLatex(line.slice(opener.length, -closer.length).trim()), nextIndex: index + 1 };
    }
    if (line !== opener) {
      continue;
    }
    const formulaLines: string[] = [];
    for (let cursor = index + 1; cursor < lines.length; cursor += 1) {
      if (lines[cursor]?.trim() === closer) {
        const latex = formulaLines.join("\n").trim();
        if (!latex) {
          return null;
        }
        return { latex: normalizeLatex(latex), nextIndex: cursor + 1 };
      }
      formulaLines.push(lines[cursor] ?? "");
    }
  }
  return null;
}

function extractBlockMath(text: string) {
  const lines = text.split(/\r?\n/);
  const placeholders: Record<string, string> = {};
  const output: string[] = [];
  let placeholderIndex = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    if (!line.trim()) {
      output.push(line);
      continue;
    }
    const extracted = extractBlockMathAt(lines, index);
    if (extracted) {
      const key = String(placeholderIndex);
      placeholders[key] = extracted.latex;
      if (output.length && output[output.length - 1]?.trim()) {
        output.push("");
      }
      output.push(BLOCK_MATH_PLACEHOLDER.replace("{index}", key));
      output.push("");
      placeholderIndex += 1;
      index = extracted.nextIndex - 1;
      continue;
    }
    output.push(line);
  }

  return { text: output.join("\n"), placeholders };
}

function insertParagraphBreaks(text: string) {
  const lines = text.split(/\r?\n/);
  const output: string[] = [];
  let previousWasText = false;

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      output.push("");
      previousWasText = false;
      continue;
    }
    if (BLOCK_MATH_PLACEHOLDER_RE.test(stripped)) {
      output.push(line);
      previousWasText = false;
      continue;
    }
    if (isStructuralLine(line)) {
      output.push(line);
      previousWasText = false;
      continue;
    }
    if (previousWasText) {
      output.push("");
    }
    output.push(line);
    previousWasText = true;
  }

  return output.join("\n");
}

function preprocessMarkdown(text: string) {
  let normalized = normalizeChineseOrderedLists(text);
  normalized = protectTableMathPipes(normalized);
  const extracted = extractBlockMath(normalized);
  normalized = insertParagraphBreaks(extracted.text);
  return { text: normalized, placeholders: extracted.placeholders };
}

function closeUnclosedFences(text: string) {
  const lines = text.split(/\r?\n/);
  let open = false;
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      open = !open;
    }
  }
  return open ? `${text}\n\`\`\`` : text;
}

function createParser() {
  return new MarkdownIt("commonmark", { html: false, linkify: false, breaks: true }).enable("table");
}

function inlineTokenContent(tokens: Token[], index: number) {
  return tokens[index]?.type === "inline" ? restoreTableMathPipes(tokens[index].content) : "";
}

function blockMathPlaceholderKey(value: string, placeholders: Record<string, string>) {
  const stripped = value.trim();
  for (const key of Object.keys(placeholders)) {
    if (stripped === BLOCK_MATH_PLACEHOLDER.replace("{index}", key)) {
      return key;
    }
  }
  return null;
}

function renderBlockMathHtml(latex: string) {
  return `<div data-type="block-math" data-latex="${escapeHtml(latex)}"></div>`;
}

function renderBoardCodeBlock(language: string | null, code: string) {
  const formatted = formatCodeIndentation(code, language);
  const escaped = escapeHtml(formatted);
  if (language) {
    return `<pre><code class="language-${escapeHtml(language)}">${escaped}</code></pre>`;
  }
  return `<pre><code>${escaped}</code></pre>`;
}

function renderChatCodeBlock(language: string | null, code: string) {
  const formatted = formatCodeIndentation(code, language);
  const highlighted = highlightCode(formatted, language);
  const languageClass = language ? ` language-${escapeHtml(language)}` : "";
  const label = codeLanguageLabel(language);
  return `<div class="overflow-hidden rounded-lg border border-gray-800 bg-gray-950"><div class="border-b border-gray-800 px-3 py-1.5 text-xs text-gray-300">${escapeHtml(label)}</div><pre class="custom-scrollbar overflow-x-auto px-3 py-2 text-[12px] leading-5 text-gray-50"><code class="hljs${languageClass}">${highlighted}</code></pre></div>`;
}

const boardMarkdownRenderer: MarkdownSurfaceRenderer = {
  renderInline: renderInlineMarkdown,
  renderBlockMath: renderBlockMathHtml,
  renderCodeBlock: renderBoardCodeBlock,
  renderHeading: (level, content) => `<h${level}>${renderInlineMarkdown(content)}</h${level}>`,
  renderParagraph: (content) => `<p>${renderInlineMarkdown(content)}</p>`,
  renderBlockquote: (content) => `<blockquote>${renderInlineMarkdown(content)}</blockquote>`,
  renderList: (tag, items) =>
    `<${tag}>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${tag}>`,
  renderTable: (rows) =>
    `<table><tbody>${rows
      .map((row, rowIndex) => {
        const cellTag = rowIndex === 0 ? "th" : "td";
        return `<tr>${row.map((cell) => `<${cellTag}>${renderInlineMarkdown(cell)}</${cellTag}>`).join("")}</tr>`;
      })
      .join("")}</tbody></table>`,
};

const chatHeadingClasses: Record<number, string> = {
  1: "text-[15px] font-semibold leading-6",
  2: "text-[14px] font-semibold leading-6",
  3: "text-[13px] font-semibold leading-6",
};

const chatMarkdownRenderer: MarkdownSurfaceRenderer = {
  renderInline: renderInlineChatMarkdown,
  renderBlockMath: (latex) => renderKatexMath(latex, true),
  renderCodeBlock: renderChatCodeBlock,
  renderHeading: (level, content) =>
    `<h${level} class="${chatHeadingClasses[level] ?? chatHeadingClasses[3]}">${renderInlineChatMarkdown(content)}</h${level}>`,
  renderParagraph: (content) => `<p class="break-words">${renderInlineChatMarkdown(content)}</p>`,
  renderBlockquote: (content) =>
    `<blockquote class="border-l-2 border-gray-300 pl-3 text-gray-600">${renderInlineChatMarkdown(content)}</blockquote>`,
  renderList: (tag, items) => {
    const listClass = tag === "ul" ? "list-disc" : "list-decimal";
    return `<${tag} class="${listClass} space-y-1 pl-4">${items
      .map((item) => `<li class="break-words">${renderInlineChatMarkdown(item)}</li>`)
      .join("")}</${tag}>`;
  },
  renderTable: (rows) =>
    `<div class="overflow-x-auto"><table class="min-w-full border-collapse text-sm"><tbody>${rows
      .map((row, rowIndex) => {
        const cellTag = rowIndex === 0 ? "th" : "td";
        const cellClass =
          rowIndex === 0
            ? "border border-gray-200 bg-gray-50 px-3 py-2 text-left font-semibold"
            : "border border-gray-200 px-3 py-2 align-top";
        return `<tr>${row
          .map((cell) => `<${cellTag} class="${cellClass}">${renderInlineChatMarkdown(cell)}</${cellTag}>`)
          .join("")}</tr>`;
      })
      .join("")}</tbody></table></div>`,
};

function tableRowsFromTokens(tokens: Token[], start: number) {
  const rows: string[][] = [];
  let index = start + 1;
  let currentRow: string[] = [];

  while (index < tokens.length) {
    const token = tokens[index];
    if (token.type === "table_close") {
      return { rows, nextIndex: index + 1 };
    }
    if (token.type === "thead_open" || token.type === "thead_close" || token.type === "tbody_open" || token.type === "tbody_close") {
      index += 1;
      continue;
    }
    if (token.type === "tr_open") {
      currentRow = [];
      index += 1;
      continue;
    }
    if (token.type === "tr_close") {
      if (currentRow.length) {
        rows.push(currentRow);
      }
      index += 1;
      continue;
    }
    if (token.type === "th_open" || token.type === "td_open") {
      currentRow.push(inlineTokenContent(tokens, index + 1));
      index += 3;
      continue;
    }
    index += 1;
  }

  return { rows, nextIndex: index };
}

function renderListFromTokens(
  tokens: Token[],
  start: number,
  listType: "bullet_list" | "ordered_list",
  renderer: MarkdownSurfaceRenderer,
) {
  const tag = listType === "bullet_list" ? "ul" : "ol";
  const items: string[] = [];
  let index = start + 1;

  while (index < tokens.length) {
    const token = tokens[index];
    if (token.type === `${listType}_close`) {
      return { html: renderer.renderList(tag, items), nextIndex: index + 1 };
    }
    if (token.type === "list_item_open") {
      items.push(inlineTokenContent(tokens, index + 2));
      index += 4;
      continue;
    }
    index += 1;
  }

  return { html: renderer.renderList(tag, items), nextIndex: index };
}

function renderTableFromTokens(tokens: Token[], start: number, renderer: MarkdownSurfaceRenderer) {
  const table = tableRowsFromTokens(tokens, start);
  return { html: renderer.renderTable(table.rows), nextIndex: table.nextIndex };
}

function renderMarkdownTokens(
  tokens: Token[],
  placeholders: Record<string, string>,
  renderer: MarkdownSurfaceRenderer,
) {
  const parts: string[] = [];
  let index = 0;

  while (index < tokens.length) {
    const token = tokens[index];

    if (token.type === "heading_open") {
      const level = Math.min(Number(token.tag.slice(1)), 3);
      parts.push(renderer.renderHeading(level, inlineTokenContent(tokens, index + 1)));
      index += 3;
      continue;
    }

    if (token.type === "paragraph_open") {
      const inlineContent = inlineTokenContent(tokens, index + 1);
      const placeholderKey = blockMathPlaceholderKey(inlineContent, placeholders);
      if (placeholderKey !== null) {
        parts.push(renderer.renderBlockMath(placeholders[placeholderKey] ?? ""));
      } else {
        parts.push(renderer.renderParagraph(inlineContent));
      }
      index += 3;
      continue;
    }

    if (token.type === "fence") {
      let language = token.info.trim() || null;
      if (language && ["text", "txt", "plain", "plaintext"].includes(language)) {
        language = null;
      }
      const content = token.content.replace(/\n$/, "");
      const kind = classifyFencedBlock(language, content);
      if (kind === "formula") {
        parts.push(renderer.renderBlockMath(normalizeLatex(content)));
      } else if (kind === "code") {
        parts.push(renderer.renderCodeBlock(language, content));
      } else {
        parts.push(renderer.renderParagraph(content));
      }
      index += 1;
      continue;
    }

    if (token.type === "blockquote_open") {
      parts.push(renderer.renderBlockquote(inlineTokenContent(tokens, index + 2)));
      index += 4;
      continue;
    }

    if (token.type === "bullet_list_open") {
      const rendered = renderListFromTokens(tokens, index, "bullet_list", renderer);
      parts.push(rendered.html);
      index = rendered.nextIndex;
      continue;
    }

    if (token.type === "ordered_list_open") {
      const rendered = renderListFromTokens(tokens, index, "ordered_list", renderer);
      parts.push(rendered.html);
      index = rendered.nextIndex;
      continue;
    }

    if (token.type === "table_open") {
      const rendered = renderTableFromTokens(tokens, index, renderer);
      parts.push(rendered.html);
      index = rendered.nextIndex;
      continue;
    }

    index += 1;
  }

  return parts.join("") || "<p></p>";
}

function renderMarkdown(contentText: string, renderer: MarkdownSurfaceRenderer, options: MarkdownRenderOptions = {}) {
  const source = options.closeUnclosedFences ? closeUnclosedFences(contentText) : contentText;
  const { text, placeholders } = preprocessMarkdown(source);
  const tokens = createParser().parse(text, {});
  return renderMarkdownTokens(tokens, placeholders, renderer);
}

export function markdownToHtml(contentText: string, options: MarkdownRenderOptions = {}) {
  return renderMarkdown(contentText, boardMarkdownRenderer, options);
}

export function markdownToChatHtml(contentText: string, options: MarkdownRenderOptions = {}) {
  return renderMarkdown(contentText, chatMarkdownRenderer, options);
}

export function streamingMarkdownToHtml(contentText: string) {
  return markdownToHtml(contentText, { closeUnclosedFences: true });
}

export { escapeHtml, normalizeLatex };
