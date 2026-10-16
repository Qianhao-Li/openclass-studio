from __future__ import annotations
OBJECT_REPLACEMENT_CHARACTER = '￼'

class CanonicalMarkupText:

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._offset = 0

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def text(self) -> str:
        return ''.join(self._parts)

    def append_text(self, value: str) -> str:
        normalized = ' '.join(value.split())
        if not normalized:
            return ''
        self._append(normalized)
        return normalized

    def append_visual_anchor(self) -> int:
        anchor_offset = self._offset
        self._append(OBJECT_REPLACEMENT_CHARACTER)
        return anchor_offset

    def _append(self, value: str) -> None:
        self._parts.extend((value, ' '))
        self._offset += len(value) + 1
