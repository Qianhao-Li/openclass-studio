from __future__ import annotations
import re
from xml.etree import ElementTree

class SourceXmlError(ValueError):
    pass

def parse_untrusted_xml(content: bytes | str) -> ElementTree.Element:
    raw = content if isinstance(content, bytes) else content.encode('utf-8', errors='replace')
    if re.search(b'<!\\s*(?:DOCTYPE|ENTITY)\\b', raw, flags=re.I):
        raise SourceXmlError('Source XML contains unsafe or invalid markup.')
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, LookupError, UnicodeError, ValueError) as exc:
        raise SourceXmlError('Source XML contains unsafe or invalid markup.') from exc
