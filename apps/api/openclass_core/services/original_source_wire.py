"""Verify the actual original bytes at the last boundary before model transport."""
from __future__ import annotations
import base64
import hashlib
import json
import mimetypes
from pathlib import Path
from openclass_core.services.original_source_delivery import current_original_source_batch, validate_original_source_call

def validate_staged_original_images(*, system_prompt, user_prompt, image_paths):
    batch = current_original_source_batch()
    if batch is None:
        return
    images = ['data:' + (mimetypes.guess_type(str(path))[0] or 'image/png') + ';base64,' + base64.b64encode(path.read_bytes()).decode() for path in image_paths]
    validate_original_source_call(system_prompt=system_prompt, user_prompt=user_prompt, image_inputs=images, source_delivery=batch)

def validate_original_responses_payload(payload, *, document_path=None):
    batch = current_original_source_batch()
    if batch is None:
        return
    wire_input = payload.get('input')
    if isinstance(wire_input, str):
        texts, images, files = ([wire_input], [], [])
    else:
        texts, images, files = ([], [], [])
        for message in wire_input or []:
            if not isinstance(message, dict):
                continue
            for item in message.get('content', []):
                if item.get('type') == 'input_text' and message.get('role') == 'user':
                    texts.append(item.get('text', ''))
                elif item.get('type') == 'input_image':
                    images.append(item.get('image_url'))
                elif item.get('type') == 'input_file':
                    files.append(item)
    if document_path is None and files:
        raise ValueError('The model request added an unauthorized original file.')
    document_bytes = None
    if document_path is not None:
        if len(files) != 1:
            raise ValueError('The model request omitted or duplicated the authorized original file.')
        data = files[0].get('file_data', '')
        if not isinstance(data, str) or not data.startswith('data:') or ';base64,' not in data:
            raise ValueError('An original file must be transmitted as its verified bytes.')
        document_bytes = base64.b64decode(data.partition(',')[2], validate=True)
        digest = hashlib.sha256(document_bytes).hexdigest()
        with Path(document_path).open('rb') as original:
            expected_digest = hashlib.file_digest(original, 'sha256').hexdigest()
        if digest != expected_digest:
            raise ValueError('The serialized file differs from the selected original file.')
        if digest not in {item.metadata.get('source_content_hash') for item in batch.evidence_items}:
            raise ValueError('The transmitted file bytes differ from the authorized original.')
    prompt = texts[0] if len(texts) == 1 else json.dumps(texts, ensure_ascii=False)
    validate_original_source_call(system_prompt=payload.get('instructions', ''), user_prompt=prompt, image_inputs=images, document_bytes=document_bytes, source_delivery=batch)

def validate_codex_original_input(turn_input, *, system_prompt='', document_path=None):
    """Check the actual app-server turn vector and optional staged original file."""
    batch = current_original_source_batch()
    if batch is None:
        return
    texts, images = ([], [])
    for item in turn_input:
        if item.get('type') == 'text':
            texts.append(item.get('text', ''))
        elif item.get('type') == 'image':
            images.append(item.get('url'))
        else:
            raise ValueError('An unsupported carrier was added to the original-source model input.')
    prompt = texts[0] if len(texts) == 1 else json.dumps(texts, ensure_ascii=False)
    validate_original_source_call(system_prompt=system_prompt, user_prompt=prompt, image_inputs=images, document_path=document_path, source_delivery=batch)

def validate_original_chat_messages(messages):
    """Check a chat-completions request immediately before its SDK call."""
    if current_original_source_batch() is None:
        return
    response_input = []
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            items = [{'type': 'input_text', 'text': content}]
        else:
            items = []
            for item in content:
                if item.get('type') == 'text':
                    items.append({'type': 'input_text', 'text': item.get('text', '')})
                elif item.get('type') == 'image_url':
                    image = item.get('image_url', {})
                    items.append({'type': 'input_image', 'image_url': image.get('url') if isinstance(image, dict) else image})
                else:
                    raise ValueError('An unsupported carrier was added to the original-source chat request.')
        response_input.append({'role': message.get('role'), 'content': items})
    validate_original_responses_payload({'input': response_input})
