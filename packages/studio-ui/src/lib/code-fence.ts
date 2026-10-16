import { formulaOnlyLatex } from "./latex-fragments";

export type FencedBlockKind = "code" | "formula" | "paragraph";

const CODE_LANGUAGE_RE = /^(?:assembly|asm|bash|c|c\+\+|csharp|css|dart|diff|dockerfile|go|graphql|html|java|javascript|js|json|jsx|kotlin|lua|makefile|markdown|md|objective-c|perl|php|powershell|ps1|python|py|r|ruby|rust|scala|shell|sh|sql|swift|toml|ts|tsx|typescript|xml|yaml|yml|zsh)$/i;
const CODE_DECLARATION_RE = /^\s*(?:async\s+)?(?:def|class|function|fn|func|interface|struct|enum|type|const|let|var|import|from|export|package|namespace|using|public|private|protected|static|void|return|throw|try|catch|switch|case|if|elif|else|for|while|do|SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b/i;
const CODE_SHELL_RE = /^\s*(?:[$#]\s*)?(?:cd|curl|echo|export|git|ls|mkdir|npm|pip|pnpm|python|uv|yarn)\b/;
const CODE_CALL_RE = /^\s*[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*\(/;
const CODE_STRUCTURE_RE = /(?:=>|\{\s*[\["']|[;}])/;

export function classifyFencedBlock(language: string | null, content: string): FencedBlockKind {
  const candidate = content.trim();
  if (candidate && formulaOnlyLatex(candidate)) {
    return "formula";
  }

  if (CODE_LANGUAGE_RE.test((language ?? "").trim())) {
    return "code";
  }
  if (!candidate || /[\u3400-\u9fff]/.test(candidate)) {
    return "paragraph";
  }

  const lines = candidate.split(/\r?\n/).filter((line) => line.trim());
  if (lines.some((line) => CODE_DECLARATION_RE.test(line) || CODE_SHELL_RE.test(line) || CODE_CALL_RE.test(line))) {
    return "code";
  }
  return lines.length > 1 && CODE_STRUCTURE_RE.test(candidate) ? "code" : "paragraph";
}
