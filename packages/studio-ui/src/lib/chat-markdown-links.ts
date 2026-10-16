import MarkdownIt from "markdown-it";

const parser = new MarkdownIt("commonmark", { html: false, linkify: false });

export function readChatMarkdownLink(value: string, start: number) {
  if (value[start] !== "[" || value[start - 1] === "!") return null;
  let escapes = 0;
  for (let index = start - 1; index >= 0 && value[index] === "\\"; index--) escapes++;
  if (escapes % 2) return null;
  const state = new parser.inline.State(value, parser, {}, []);
  const labelEnd = parser.helpers.parseLinkLabel(state, start, true);
  if (labelEnd < 0 || value[labelEnd + 1] !== "(") return null;
  let cursor = labelEnd + 2;
  while (/\s/.test(value[cursor] ?? "")) cursor++;
  const destination = parser.helpers.parseLinkDestination(value, cursor, value.length);
  if (!destination.ok) return null;
  cursor = destination.pos;
  while (/\s/.test(value[cursor] ?? "")) cursor++;
  if (value[cursor] !== ")") {
    const title = parser.helpers.parseLinkTitle(value, cursor, value.length);
    if (!title.ok) return null;
    cursor = title.pos;
    while (/\s/.test(value[cursor] ?? "")) cursor++;
    if (value[cursor] !== ")") return null;
  }
  const href = parser.normalizeLink(destination.str);
  if (!parser.validateLink(href)) return null;
  try {
    const url = new URL(href);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
  } catch {
    return null;
  }
  const tokens = parser.parseInline(value.slice(start + 1, labelEnd), {})[0]?.children ?? [];
  const label = tokens.filter((token) => ["text", "code_inline", "softbreak"].includes(token.type))
    .map((token) => token.type === "softbreak" ? " " : token.content).join("");
  return { href, label: label || href, end: cursor + 1 };
}
