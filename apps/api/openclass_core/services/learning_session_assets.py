"""Keep referenced board images portable without reading an author's external library."""
from __future__ import annotations
import re
import json
from html.parser import HTMLParser
from openclass_core.models import BoardDocument
from openclass_core.services.board_asset_store import BoardAssetStore
from openclass_core.services.board_asset_store import board_asset_id_from_url
from openclass_core.services.board_asset_identity import rewrite_board_asset_html, rewrite_board_asset_json, rewrite_board_asset_markdown
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocMessageNode, RidocV3Error, content_hash
from openclass_core.services.learning_session_ridoc import remap_state_objects
from openclass_core.services.ridoc_document_actions import build_document_action
_ASSET_ID = re.compile('basset_[a-f0-9]{12}_([a-f0-9]{32})')

def document_asset_ids(document: BoardDocument) -> set[str]:
    """Use structured references, not asset-like strings quoted in learning text."""
    ids: set[str] = set()

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == 'assetId' and isinstance(item, str) and _ASSET_ID.fullmatch(item):
                    ids.add(item)
                elif key in {'src', 'originalSrc', 'asset_url'} and isinstance(item, str):
                    asset_id = board_asset_id_from_url(item)
                    if asset_id:
                        ids.add(asset_id)
                elif isinstance(item, (dict, list)):
                    visit(item)

    class References(HTMLParser):

        def handle_starttag(self, tag, attributes):
            for key, value in attributes:
                if key == 'data-board-asset-id' and value and _ASSET_ID.fullmatch(value):
                    ids.add(value)
                elif tag == 'img' and key == 'src' and value:
                    asset_id = board_asset_id_from_url(value)
                    if asset_id:
                        ids.add(asset_id)
    visit(document.content_json)
    References().feed(document.content_html)
    return ids

def hydrate_board_assets(archive: RidocArchiveV3, owner_user_id: str, store: BoardAssetStore) -> None:
    replacements = {}
    document_resources = {}
    for reference, payload in list(archive.documents.items()):
        document = BoardDocument.model_validate(payload)
        ids = document_asset_ids(document)
        resources = set()
        nodes = [node for node in archive.nodes.values() if reference in ((node.document_ref,) if isinstance(node, RidocMessageNode) else (node.before_document_ref, node.after_document_ref))]
        lessons = {node.lesson_id for node in nodes} | {state.lesson_id for state in archive.document_states.values() if state.document_ref == reference}
        for asset_id in ids:
            digest_prefix = asset_id.rsplit('_', 1)[1]
            digest = next((key for key in archive.assets if key.startswith(digest_prefix)), None)
            if digest is None:
                raise RidocV3Error('Published course is missing a referenced image; ask the author to publish it again')
            resources.add(digest)
            if not lessons:
                raise RidocV3Error('Published image has no authorized lesson reference')
            for lesson_id in lessons:
                record = store.put_bytes(owner_user_id=owner_user_id, lesson_id=lesson_id, document_id=document.id, content=archive.assets[digest], mime_type='application/octet-stream')
            for node in nodes:
                if digest not in node.resource_refs:
                    node.resource_refs.append(digest)
            document.content_json = json.loads(rewrite_board_asset_json(json.dumps(document.content_json), old_asset_id=asset_id, new_asset_id=record.id))
            document.content_html = rewrite_board_asset_html(document.content_html, old_asset_id=asset_id, new_asset_id=record.id)
            document.content_text = rewrite_board_asset_markdown(document.content_text, old_asset_id=asset_id, new_asset_id=record.id)
        updated = document.model_dump(mode='json')
        replacements[reference] = content_hash(updated)
        archive.documents.pop(reference)
        archive.documents[replacements[reference]] = updated
        document_resources[replacements[reference]] = resources
    for node in archive.nodes.values():
        if isinstance(node, RidocMessageNode):
            node.document_ref = replacements[node.document_ref]
        else:
            if node.before_document_ref:
                node.before_document_ref = replacements[node.before_document_ref]
            node.after_document_ref = replacements[node.after_document_ref]
            if node.document_action:
                action = node.document_action
                node.document_action = build_document_action(BoardDocument.model_validate(archive.documents[node.before_document_ref]), BoardDocument.model_validate(archive.documents[node.after_document_ref]), versions={key: value['version'] for key, value in action.get('inputs', {}).items()}, document_version=action['input_document_version'], structure_version=action.get('input_structure_version', ''), allow_local=action['scope'] == 'blocks')
    remap_state_objects(archive, document_refs=replacements, document_resources=document_resources)

def capture_board_assets(archive: RidocArchiveV3, owner_user_id: str, store: BoardAssetStore) -> None:
    """Embed raw bytes referenced by newly saved documents; never OCR or extract file bodies."""
    for node in archive.nodes.values():
        refs = (node.document_ref,) if isinstance(node, RidocMessageNode) else (node.before_document_ref, node.after_document_ref)
        for ref in refs:
            if ref is None:
                continue
            payload = archive.documents[ref]
            for asset_id in document_asset_ids(BoardDocument.model_validate(payload)):
                stored = store.read_bytes(asset_id, owner_user_id)
                if stored is None:
                    from openclass_core.services.ridoc_board_assets import read_embedded_board_asset
                    stored = read_embedded_board_asset(archive, asset_id, owner_user_id)
                if stored is None:
                    raise RidocV3Error('A referenced board image is unavailable')
                record, content = stored
                archive.assets[record.content_hash] = content
                if record.content_hash not in node.resource_refs:
                    node.resource_refs.append(record.content_hash)
    if archive.document_states:
        document_resources = {}
        for state in archive.document_states.values():
            resources = set()
            for asset_id in document_asset_ids(BoardDocument.model_validate(archive.documents[state.document_ref])):
                stored = store.read_bytes(asset_id, owner_user_id)
                if stored is None:
                    from openclass_core.services.ridoc_board_assets import read_embedded_board_asset
                    stored = read_embedded_board_asset(archive, asset_id, owner_user_id)
                if stored is None:
                    raise RidocV3Error('A saved-state image is unavailable')
                record, content = stored
                archive.assets[record.content_hash] = content
                resources.add(record.content_hash)
            document_resources[state.document_ref] = resources
        remap_state_objects(archive, document_resources=document_resources)
