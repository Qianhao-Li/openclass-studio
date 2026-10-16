"""Reproduce Markdown heading boundaries through the existing rich-text parser."""
from openclass_core.services.board_segment_index import _node_text, compact_segment_text
from openclass_core.services.rich_document import text_to_tiptap_doc

def heading_block_span(markdown, segments, target):
    peers = [segment for segment in segments if segment.kind == 'heading' and segment.text == target.text]
    candidates, offset = ([], 0)
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip('\r\n')
        if content.startswith('#'):
            nodes = text_to_tiptap_doc(content).get('content', [])
            if len(nodes) == 1 and nodes[0].get('type') == 'heading':
                label = compact_segment_text(_node_text(nodes[0]), limit=800)
                if label == target.text:
                    candidates.append((offset, offset + len(content), offset, offset + len(content)))
        offset += len(line)
    if len(candidates) != len(peers):
        return None
    ordinal = next((i for i, segment in enumerate(peers) if segment.segment_id == target.segment_id), None)
    return candidates[ordinal] if ordinal is not None and ordinal < len(candidates) else None
