"""Restore rich visual nodes untouched by authorized Markdown patches."""
from __future__ import annotations
from copy import deepcopy
from openclass_core.services.rich_document import document_to_markdown, rebuild_document_from_content_json

def _node_spans(document):
    text = document_to_markdown(document)
    cursor, result = (0, [])
    for index, node in enumerate(document.content_json.get('content', [])):
        fragment = document_to_markdown(document.model_copy(update={'content_json': {'type': 'doc', 'content': [node]}, 'content_html': '', 'content_text': ''}))
        if not fragment:
            continue
        start = text.find(fragment, cursor)
        if start < 0:
            raise ValueError('An existing visual cannot be located reliably in the document serialization.')
        result.append((index, start, start + len(fragment), node))
        cursor = start + len(fragment)
    return result

def restore_untouched_visual_nodes(before, after, patches):
    """Use validated patch coordinates; never guess visual identity from labels."""
    if not any((node.get('type') in {'resourceVisualBlock', 'image'} for node in before.content_json.get('content', []))):
        return after
    old_spans = _node_spans(before)
    new_spans = _node_spans(after)
    after_text = document_to_markdown(after)
    before_text = document_to_markdown(before)
    replacements = []
    for _index, start, end, node in old_spans:
        if node.get('type') not in {'resourceVisualBlock', 'image'}:
            continue
        if any((patch.start < end and patch.end > start or (patch.start == patch.end and start < patch.start < end) for patch in patches)):
            continue
        shift = sum((len(patch.replacement) - (patch.end - patch.start) for patch in patches if patch.end <= start))
        mapped_start, mapped_end = (start + shift, end + shift)
        if after_text[mapped_start:mapped_end] != before_text[start:end]:
            raise ValueError('An untouched original visual changed outside the authorized mutation.')
        nodes = [span for span in new_spans if mapped_start <= span[1] and span[2] <= mapped_end]
        if not nodes or nodes[0][1] != mapped_start or nodes[-1][2] != mapped_end:
            raise ValueError('An untouched original visual has no exact reconstruction boundary.')
        replacements.append((nodes[0][0], nodes[-1][0] + 1, deepcopy(node)))
    content = deepcopy(after.content_json)
    for start, end, node in reversed(replacements):
        content['content'][start:end] = [node]
    return rebuild_document_from_content_json(after, content)
