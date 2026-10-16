import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";

const LANGUAGE_ALIASES: Record<string, string> = {
  js: "javascript",
  ts: "typescript",
  py: "python",
  sh: "bash",
  zsh: "bash",
  yml: "yaml",
  md: "markdown",
  html: "xml",
  txt: "plaintext",
  text: "plaintext",
  plain: "plaintext",
  plaintext: "plaintext",
};

const LANGUAGE_LABELS: Record<string, string> = {
  rust: "Rust",
  python: "Python",
  javascript: "JavaScript",
  typescript: "TypeScript",
  bash: "Bash",
  shell: "Shell",
  json: "JSON",
  sql: "SQL",
  java: "Java",
  go: "Go",
  css: "CSS",
  markdown: "Markdown",
  xml: "HTML",
  plaintext: "Plain Text",
};

let registered = false;

function registerLanguages() {
  if (registered) {
    return;
  }
  registered = true;
  hljs.registerLanguage("bash", bash);
  hljs.registerLanguage("css", css);
  hljs.registerLanguage("go", go);
  hljs.registerLanguage("java", java);
  hljs.registerLanguage("javascript", javascript);
  hljs.registerLanguage("json", json);
  hljs.registerLanguage("markdown", markdown);
  hljs.registerLanguage("plaintext", plaintext);
  hljs.registerLanguage("python", python);
  hljs.registerLanguage("rust", rust);
  hljs.registerLanguage("shell", shell);
  hljs.registerLanguage("sql", sql);
  hljs.registerLanguage("typescript", typescript);
  hljs.registerLanguage("xml", xml);
}

export function normalizeCodeLanguage(language: string | null | undefined) {
  const normalized = (language || "plaintext").trim().toLowerCase();
  return LANGUAGE_ALIASES[normalized] || normalized;
}

export function codeLanguageLabel(language: string | null | undefined) {
  const normalized = normalizeCodeLanguage(language);
  return LANGUAGE_LABELS[normalized] || normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

export function highlightCode(code: string, language: string | null | undefined) {
  registerLanguages();
  const normalized = normalizeCodeLanguage(language);
  if (hljs.getLanguage(normalized)) {
    return hljs.highlight(code, { language: normalized }).value;
  }
  return hljs.highlight(code, { language: "plaintext" }).value;
}
