"""Strict, atomic document actions. No conflict resolution or best-effort skips.

Top-level rich-document objects are opaque. An object includes its entire nested
subtree. Structural changes conservatively depend on the complete sibling order.
Documents without trustworthy unique object IDs use a whole-document condition.
"""
from __future__ import annotations
from copy import deepcopy
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_v3 import RidocV3Error, content_hash

class DocumentActionBlocked(RidocV3Error):

    def __init__(self, reason: str, object_id: str='') -> None:
        super().__init__(reason)
        self.object_id = object_id

def objects(document: BoardDocument) -> tuple[list[str], dict[str, dict[str, Any]]] | None:
    raw = document.content_json.get('content')
    if not isinstance(raw, list):
        return None
    result: dict[str, dict[str, Any]] = {}
    for node in raw:
        attrs = node.get('attrs') if isinstance(node, dict) else None
        identity = attrs.get('blockId') if isinstance(attrs, dict) else None
        if not isinstance(identity, str) or not identity.strip() or identity in result:
            return None
        result[identity] = node
    return (list(result), result)

def initial_versions(document: BoardDocument, node_id: str) -> dict[str, str]:
    found = objects(document)
    return {key: node_id for key in found[0]} if found is not None else {}

def build_document_action(before: BoardDocument, after: BoardDocument, *, versions: dict[str, str], document_version: str, structure_version: str, allow_local: bool=True) -> dict[str, Any]:
    """Capture the action at its original write boundary, never during merge."""
    common: dict[str, Any] = {'version': 1, 'before_hash': content_hash(before), 'after_hash': content_hash(after), 'input_document_version': document_version}
    old, new = (objects(before), objects(after))
    if not allow_local or old is None or new is None or (before.title != after.title) or (before.page_settings != after.page_settings) or (before.id != after.id):
        return {**common, 'scope': 'document'}
    old_order, old_objects = old
    new_order, new_objects = new
    changed = [key for key in dict.fromkeys([*old_order, *new_order]) if old_objects.get(key) != new_objects.get(key)]
    required_ids = old_order if old_order != new_order else [key for key in changed if key in old_objects]
    if any((key not in versions for key in required_ids)):
        return {**common, 'scope': 'document'}
    inputs = {key: {'version': versions[key], 'hash': content_hash(old_objects[key])} for key in required_ids}
    return {**common, 'scope': 'blocks', 'inputs': inputs, 'changes': {key: deepcopy(new_objects.get(key)) for key in changed}, 'inverse_changes': {key: deepcopy(old_objects.get(key)) for key in changed}, 'order_before': old_order, 'order_after': new_order, 'input_structure_version': structure_version}

def apply_document_action(current: BoardDocument, before: BoardDocument, after: BoardDocument, action: dict[str, Any], *, node_id: str, versions: dict[str, str], document_version: str, structure_version: str, legacy_input_version: str) -> tuple[BoardDocument, dict[str, str], str]:
    """Return a complete new state or raise without mutating the input state."""
    if action and (action.get('version') != 1 or action.get('before_hash') != content_hash(before) or action.get('after_hash') != content_hash(after)):
        raise DocumentActionBlocked('The document change record does not match the original before and after snapshots')
    scope = action.get('scope', 'document')
    if scope == 'document':
        if action and set(action) != {'version', 'before_hash', 'after_hash', 'input_document_version', 'scope'}:
            raise DocumentActionBlocked('The whole-document change contains fields that cannot be verified')
        expected = action.get('input_document_version', legacy_input_version)
        if current != before or document_version != expected:
            reason = 'The original document input version has changed. This change cannot be applied' if action else 'The legacy change has no verifiable local object version, and the current document differs from its original input'
            raise DocumentActionBlocked(reason, 'document')
        old_objects, new_objects = (objects(before), objects(after))
        if old_objects is not None and new_objects is not None:
            next_versions = {key: versions.get(key, node_id) if old_objects[1].get(key) == value else node_id for key, value in new_objects[1].items()}
            next_structure = structure_version if old_objects[0] == new_objects[0] else node_id
        else:
            next_versions, next_structure = (initial_versions(after, node_id), node_id)
        return (after.model_copy(deep=True), next_versions, next_structure)
    if scope != 'blocks':
        raise DocumentActionBlocked('Unsupported document change scope')
    old, new, present = (objects(before), objects(after), objects(current))
    if old is None or new is None or present is None:
        raise DocumentActionBlocked('The document object is missing a stable, unique ID')
    expected = build_document_action(before, after, versions={key: item['version'] for key, item in action.get('inputs', {}).items()}, document_version=action['input_document_version'], structure_version=action.get('input_structure_version', ''))
    if expected != action:
        raise DocumentActionBlocked('The document change operations do not match the original snapshots')
    order, present_objects = present
    for key, requirement in action['inputs'].items():
        if key not in present_objects:
            raise DocumentActionBlocked('The object originally targeted by the change no longer exists', key)
        if versions.get(key) != requirement['version'] or content_hash(present_objects[key]) != requirement['hash']:
            raise DocumentActionBlocked('The object version originally targeted by the change no longer exists', key)
    for key, replacement in action['changes'].items():
        if key not in old[1] and key in present_objects:
            raise DocumentActionBlocked('The ID of the object to insert already exists', key)
    structural = action['order_before'] != action['order_after']
    if structural and (order != action['order_before'] or structure_version != action['input_structure_version']):
        raise DocumentActionBlocked("The change's original insertion position or structure version no longer exists", 'document/order')
    changed_objects = deepcopy(present_objects)
    updated_versions = dict(versions)
    for key, replacement in action['changes'].items():
        if replacement is None:
            changed_objects.pop(key, None)
            updated_versions.pop(key, None)
        else:
            changed_objects[key] = deepcopy(replacement)
            updated_versions[key] = node_id
    next_order = action['order_after'] if structural else order
    result = after.model_copy(deep=True) if current == before else rebuild_document_from_content_json(current, {'type': 'doc', 'content': [changed_objects[key] for key in next_order]})
    return (result, updated_versions, node_id if structural else structure_version)
