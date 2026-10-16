const PYTHON_DEDENT_RE = /^(elif\b|else:|except\b|finally:)/;
const PYTHON_COLON_RE = /:\s*(#.*)?$/;

function skipStringLiterals(input: string, visitor: (char: string, index: number) => void) {
  let inString = false;
  let stringChar = "";

  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    if (inString) {
      if (char === stringChar && input[index - 1] !== "\\") {
        inString = false;
      }
      continue;
    }
    if (char === '"' || char === "'" || char === "`") {
      inString = true;
      stringChar = char;
      continue;
    }
    visitor(char, index);
  }
}

function braceIndentDelta(line: string) {
  let delta = 0;
  skipStringLiterals(line, (char) => {
    if (char === "{") {
      delta += 1;
    } else if (char === "}") {
      delta -= 1;
    }
  });
  return delta;
}

export function codeNeedsIndentation(code: string) {
  const lines = code.replace(/\r\n?/g, "\n").split("\n");
  const nonEmpty = lines.filter((line) => line.trim().length > 0);
  if (nonEmpty.length === 0) {
    return false;
  }
  const indented = nonEmpty.filter((line) => /^\s+\S/.test(line));
  return indented.length < nonEmpty.length * 0.25;
}

function formatBraceIndentation(code: string, indentSize = 4) {
  const lines = code.replace(/\r\n?/g, "\n").split("\n");
  const result: string[] = [];
  let level = 0;

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      result.push("");
      continue;
    }
    if (stripped.startsWith("}")) {
      level = Math.max(0, level - 1);
    }
    result.push(" ".repeat(level * indentSize) + stripped);
    level = Math.max(0, level + braceIndentDelta(stripped));
  }

  return result.join("\n").replace(/\n$/, "");
}

function formatPythonIndentation(code: string, indentSize = 4) {
  const lines = code.replace(/\r\n?/g, "\n").split("\n");
  const result: string[] = [];
  let level = 0;

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped) {
      result.push("");
      continue;
    }
    if (PYTHON_DEDENT_RE.test(stripped)) {
      level = Math.max(0, level - 1);
    }
    result.push(" ".repeat(level * indentSize) + stripped);
    if (PYTHON_COLON_RE.test(stripped)) {
      level += 1;
    }
  }

  return result.join("\n").replace(/\n$/, "");
}

export function formatCodeIndentation(code: string, language: string | null | undefined) {
  if (!code.trim() || !codeNeedsIndentation(code)) {
    return code;
  }

  const normalized = (language || "").trim().toLowerCase();
  if (normalized === "python" || normalized === "py") {
    return formatPythonIndentation(code);
  }
  if (
    normalized === "rust" ||
    normalized === "javascript" ||
    normalized === "js" ||
    normalized === "typescript" ||
    normalized === "ts" ||
    normalized === "java" ||
    normalized === "go" ||
    normalized === "c" ||
    normalized === "cpp" ||
    normalized === "csharp" ||
    normalized === "cs" ||
    normalized === "swift" ||
    normalized === "kotlin" ||
    normalized === "scala" ||
    normalized === "php" ||
    normalized === "json" ||
    normalized === "plaintext" ||
    normalized === "text" ||
    normalized === "bash" ||
    normalized === "shell" ||
    normalized === "sh" ||
    normalized === ""
  ) {
    if (normalized === "json") {
      return code;
    }
    if (normalized === "bash" || normalized === "shell" || normalized === "sh") {
      return code;
    }
    return formatBraceIndentation(code);
  }

  return formatBraceIndentation(code);
}
