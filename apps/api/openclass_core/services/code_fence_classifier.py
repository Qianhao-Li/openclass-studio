from __future__ import annotations
import re
from collections.abc import Callable
from typing import Literal
FencedBlockKind = Literal['code', 'formula', 'paragraph']
_CODE_LANGUAGE_RE = re.compile('^(?:assembly|asm|bash|c|c\\+\\+|csharp|css|dart|diff|dockerfile|go|graphql|html|java|javascript|js|json|jsx|kotlin|lua|makefile|markdown|md|objective-c|perl|php|powershell|ps1|python|py|r|ruby|rust|scala|shell|sh|sql|swift|toml|ts|tsx|typescript|xml|yaml|yml|zsh)$', re.IGNORECASE)
_CODE_DECLARATION_RE = re.compile('^\\s*(?:async\\s+)?(?:def|class|function|fn|func|interface|struct|enum|type|const|let|var|import|from|export|package|namespace|using|public|private|protected|static|void|return|throw|try|catch|switch|case|if|elif|else|for|while|do|SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\\b', re.IGNORECASE)
_CODE_SHELL_RE = re.compile('^\\s*(?:[$#]\\s*)?(?:cd|curl|echo|export|git|ls|mkdir|npm|pip|pnpm|python|uv|yarn)\\b')
_CODE_CALL_RE = re.compile('^\\s*[A-Za-z_$][\\w$]*(?:\\.[A-Za-z_$][\\w$]*)+\\s*\\(')
_CODE_STRUCTURE_RE = re.compile('(?:=>|\\{\\s*[\\[\\"\']|[;}])')

def classify_fenced_block(language: str | None, content: str, *, is_formula: Callable[[str], bool]) -> FencedBlockKind:
    """Classify a Markdown fence by its content semantics, not its fence alone."""
    candidate = content.strip()
    if candidate and is_formula(candidate):
        return 'formula'
    normalized_language = (language or '').strip().lower()
    if _CODE_LANGUAGE_RE.fullmatch(normalized_language):
        return 'code'
    if not candidate or re.search('[\\u3400-\\u9fff]', candidate):
        return 'paragraph'
    lines = [line for line in candidate.splitlines() if line.strip()]
    if any((_CODE_DECLARATION_RE.match(line) or _CODE_SHELL_RE.match(line) or _CODE_CALL_RE.match(line) for line in lines)):
        return 'code'
    if len(lines) > 1 and _CODE_STRUCTURE_RE.search(candidate):
        return 'code'
    return 'paragraph'
