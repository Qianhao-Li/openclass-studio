"""Validate authored math with the same locked KaTeX runtime as the editor."""
from __future__ import annotations
import json
import shutil
import subprocess
from functools import lru_cache
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.config import ROOT_DIR

class DocumentMathError(ValueError):
    pass
_VALIDATOR = "\nconst fs = require('node:fs');\nconst katex = require('katex');\nrequire('katex/contrib/mhchem');\nconst formulas = JSON.parse(fs.readFileSync(0, 'utf8'));\nconst errors = [];\nformulas.forEach(([latex, displayMode], index) => {\n  try {\n    katex.renderToString(latex, {\n      displayMode, throwOnError: true, strict: 'ignore', trust: false,\n      maxExpand: 1000, maxSize: 100,\n    });\n  } catch (error) {\n    errors.push({index, message: String(error.message).slice(0, 400)});\n  }\n});\nprocess.stdout.write(JSON.stringify(errors));\n"

def document_formulas(document: BoardDocument) -> tuple[tuple[str, bool], ...]:
    result: list[tuple[str, bool]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get('type') in {'inlineMath', 'blockMath'}:
            result.append((str(node.get('attrs', {}).get('latex') or ''), node['type'] == 'blockMath'))
        for child in node.get('content', []):
            walk(child)
    walk(document.content_json)
    return tuple(result)

@lru_cache(maxsize=64)
def _check_formulas(formulas: tuple[tuple[str, bool], ...]) -> None:
    node = shutil.which('node')
    if not node:
        raise DocumentMathError('Formula syntax checking is unavailable. The document was not saved.')
    try:
        result = subprocess.run([node, '-e', _VALIDATOR], input=json.dumps(formulas), text=True, capture_output=True, timeout=10, cwd=ROOT_DIR, check=True)
        errors = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise DocumentMathError('Formula syntax checking failed. The document was not saved.') from exc
    if errors:
        first = errors[0]
        raise DocumentMathError(f"Formula {first['index'] + 1}: {first['message']}. The document was not saved.")

def validate_document_math(document: BoardDocument) -> None:
    formulas = document_formulas(document)
    if formulas:
        _check_formulas(formulas)
