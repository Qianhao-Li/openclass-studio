"""Read immutable web/EPUB/notebook native objects and their embedded images."""
from __future__ import annotations
import base64
import hashlib
import json
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from .source_archive import SafeSourceArchive
from .source_range_reader import _epub_spine_items
from .source_content_formats import SourceContentReadError, decode, interval, text_part
from .source_content_media import image_part
from .source_content_assets import asset_reference, decode_inline_image, decode_image_bundle, replace_inline_images, structured_asset_provenance

class _DOM(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {'tag': 'document', 'attributes': {}, 'children': []}
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attributes': dict(attrs), 'children': []}
        self.stack[-1]['children'].append(node)
        self.nodes.append(node)
        if tag not in {'img', 'br', 'hr', 'meta', 'link', 'input', 'source', 'area', 'base', 'embed', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.stack[-1]['children'].append(text)

def _nodes(node):
    yield node
    for child in node['children']:
        if isinstance(child, dict):
            yield from _nodes(child)

def _dom_parts(text, *, locator, scope, get_image, provenance=None):
    dom = _DOM()
    dom.feed(text)
    selected = dom.root
    if locator.get('kind') == 'epub_spine':
        bodies = [node for node in dom.nodes if node['tag'] == 'body']
        if len(bodies) == 1:
            selected = bodies[0]
    anchor = (scope or {}).get('start_anchor')
    end_anchor = (scope or {}).get('end_anchor')

    def find_anchor(value):
        candidates = [node for node in dom.nodes if node['attributes'].get('id') == value or node['attributes'].get('name') == value]
        if len(candidates) != 1:
            raise SourceContentReadError('The original DOM anchor is missing or ambiguous.')
        return candidates[0]
    mode = (scope or {}).get('metadata', {}).get('epub_boundary_mode', '') if locator.get('kind') == 'epub_spine' else ''
    if mode == 'prefix':
        if not end_anchor or anchor:
            raise SourceContentReadError('The EPUB prefix requires only its verified ending anchor.')
        selected = _dom_interval(dom.root, selected, find_anchor(end_anchor), exclusive=bool((scope or {}).get('metadata', {}).get('end_anchor_exclusive', True)))
        locator = {**locator, 'end_anchor': end_anchor, 'metadata': dict(scope.get('metadata', {}))}
    elif mode == 'suffix':
        if not anchor or end_anchor:
            raise SourceContentReadError('The EPUB suffix requires only its verified starting anchor.')
        selected = _dom_interval(dom.root, find_anchor(anchor), dom.root)
        locator = {**locator, 'start_anchor': anchor, 'metadata': dict(scope.get('metadata', {}))}
    elif mode not in {'', 'interval'}:
        raise SourceContentReadError('The EPUB boundary fragment is invalid.')
    elif end_anchor:
        if not anchor:
            raise SourceContentReadError('A DOM ending anchor requires an explicit starting anchor.')
        first, last = (find_anchor(anchor), find_anchor(end_anchor))
        selected = _dom_interval(dom.root, first, last, exclusive=bool((scope or {}).get('metadata', {}).get('end_anchor_exclusive')))
        locator = {**locator, 'start_anchor': anchor, 'end_anchor': end_anchor, 'metadata': {'end_anchor_exclusive': bool((scope or {}).get('metadata', {}).get('end_anchor_exclusive'))}}
    elif anchor:
        selected = find_anchor(anchor)
        locator = {**locator, 'start_anchor': anchor}
    image_units, references = ({}, {})

    def register(uri):
        mime, raw = decode_inline_image(uri)
        reference = asset_reference(raw, mime)
        digest = reference['sha256']
        if digest not in references:
            unit = get_image(uri, locator)
            unit['locator'] = {**unit['locator'], 'object': 'embedded_asset:' + digest}
            unit['provenance']['asset_sha256'] = digest
            image_units[digest], references[digest] = (unit, reference)
        return reference
    external_images = {}
    for node in _nodes(selected):
        tag, attrs = (node['tag'], node['attributes'])
        if tag in {'canvas', 'svg', 'object', 'iframe', 'video', 'audio', 'embed'}:
            raise SourceContentReadError('This original DOM object requires a separately verified visual or media reader.')
        if 'url(' in attrs.get('style', '') or (tag == 'link' and 'stylesheet' in attrs.get('rel', '')):
            raise SourceContentReadError('This DOM range depends on styles whose visual resources require an explicit mapped snapshot.')
        if tag == 'img':
            if attrs.get('srcset'):
                raise SourceContentReadError('Select a snapshot with a verified image source instead of ambiguous srcset candidates.')
            uri = attrs.get('src', '')
            if uri.startswith('data:image/'):
                register(uri)
            else:
                original = get_image(uri, locator)
                identity = json.dumps(original['locator'], sort_keys=True, ensure_ascii=False)
                external_images.setdefault(identity, original)
    selected = replace_inline_images(selected, register)
    representation = structured_asset_provenance(references.values()) if references else {}
    return [text_part(json.dumps(selected, ensure_ascii=False), {**locator, 'object': 'native_dom'}, **{**(provenance or {}), **representation}), *external_images.values(), *image_units.values()]

def _dom_interval(root, first, last, *, exclusive=False):
    """Clip original DOM order, retaining wrappers without out-of-range text."""
    spans, position = ({}, 0)

    def index(node):
        nonlocal position
        begin = position
        position += 1
        for child in node['children']:
            if isinstance(child, dict):
                index(child)
            else:
                position += 1
        position += 1
        spans[id(node)] = (begin, position)
    index(root)
    start = spans[id(first)][0]
    end = spans[id(last)][0 if exclusive else 1]
    if start >= end or (last is not root and spans[id(last)][0] < start):
        raise SourceContentReadError('The original DOM anchors are reversed or empty.')

    def clip(node):
        lo, hi = spans[id(node)]
        if hi <= start or lo >= end:
            return None
        if start <= lo and hi <= end:
            return node
        children, cursor = ([], lo + 1)
        for child in node['children']:
            if isinstance(child, dict):
                included = clip(child)
                if included is not None:
                    children.append(included)
                cursor = spans[id(child)][1]
            else:
                if start <= cursor < end:
                    children.append(child)
                cursor += 1
        return {'tag': node['tag'], 'attributes': {}, 'children': children}
    return clip(root)

def _archive_image(archive, parent, uri):
    parsed = urlsplit(uri)
    if parsed.scheme or parsed.netloc or parsed.path.startswith('/'):
        raise SourceContentReadError('The original image is not an embedded archive object.')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(parent), unquote(parsed.path)))
    if target.startswith('../') or target not in archive.namelist():
        raise SourceContentReadError('The embedded original image is missing or outside its archive.')
    return (target, archive.read(target))

def read_web_units(source, path, artifact, scope):
    if path.suffix.lower() == '.epub':
        with SafeSourceArchive(path) as archive:
            spine = _epub_spine_items(archive)
            effective = dict(scope or {})
            metadata = dict(effective.get('metadata', {}))
            if metadata.get('index_base', 1) == 0:
                if type(effective.get('start')) is not int or type(effective.get('end')) is not int:
                    raise SourceContentReadError('The legacy EPUB spine coordinates are invalid.')
                effective.update(start=effective['start'] + 1, end=effective['end'] + 1)
                metadata['index_base'] = 1
                effective['metadata'] = metadata
            start, end = interval(effective or None, 'epub_spine', len(spine))
            if scope and scope.get('container') and (scope['container'] != spine[start - 1]):
                raise SourceContentReadError('The EPUB spine container does not match its one-based index.')
            result = []
            for index in range(start, end + 1):
                container = spine[index - 1]
                local = {**effective, 'container': container, 'start': index, 'end': index, 'start_anchor': effective.get('start_anchor', '') if index == start else '', 'end_anchor': effective.get('end_anchor', '') if index == end else '', 'metadata': dict(metadata)}
                mode = metadata.get('epub_boundary_mode', '')
                if start != end:
                    mode = 'suffix' if index == start and local['start_anchor'] else 'prefix' if index == end and local['end_anchor'] else ''
                elif local['end_anchor'] and (not local['start_anchor']):
                    mode = 'prefix'
                if mode:
                    local['metadata']['epub_boundary_mode'] = mode
                else:
                    local['metadata'].pop('epub_boundary_mode', None)
                if local['end_anchor']:
                    local['metadata'].setdefault('end_anchor_exclusive', True)
                locator = {'kind': 'epub_spine', 'container': container, 'start': index, 'end': index}
                if local['metadata']:
                    locator['metadata'] = dict(local['metadata'])

                def image(uri, at):
                    if uri.startswith('data:image/'):
                        header, encoded = uri.split(',', 1)
                        if not header.endswith(';base64'):
                            raise SourceContentReadError('The embedded image must use verifiable base64 encoding.')
                        raw = base64.b64decode(encoded, validate=True)
                        return image_part(raw, locator={**at, 'object': 'inline_image'}, artifact=artifact, original_name=container)
                    target, raw = _archive_image(archive, container, uri)
                    return image_part(raw, locator={**at, 'object': target}, artifact=artifact, original_name=target)
                result.extend(_dom_parts(decode(archive.read(container)), locator=locator, scope=local, get_image=image))
            return result
    if scope and scope.get('kind') not in {'dom_anchor', 'structured_path', 'web_snapshot'}:
        raise SourceContentReadError('This source requires original DOM snapshot coordinates.')
    if scope and (scope.get('start') is not None or scope.get('end') is not None or scope.get('path')):
        raise SourceContentReadError('This web range has no verifiable DOM anchor.')
    if source.file_name.endswith('.webbundle.zip'):
        with SafeSourceArchive(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            url = manifest['url']
            if scope and scope.get('container') not in {None, '', url}:
                raise SourceContentReadError('The range belongs to a different web snapshot.')

            def image(uri, at):
                candidates = [asset for asset in manifest.get('assets', []) if uri in {asset.get('url'), asset.get('name')}]
                if len(candidates) != 1:
                    raise SourceContentReadError('The snapshot image has no unique resource mapping.')
                asset = candidates[0]
                raw = archive.read(asset['name'])
                if hashlib.sha256(raw).hexdigest() != asset.get('sha256'):
                    raise SourceContentReadError('The snapshot image hash changed.')
                return image_part(raw, locator={**at, 'object': asset['name']}, artifact=artifact, original_name=asset['url'])
            return _dom_parts(decode(archive.read('content.html')), locator={'kind': 'web_snapshot', 'container': url}, scope=scope, get_image=image, provenance={'input_method': 'original_web_snapshot'})

    def unavailable(_uri, _at):
        raise SourceContentReadError('The original HTML has no verified embedded image snapshot.')
    return _dom_parts(decode(path.read_bytes()), locator={'kind': 'dom_anchor'}, scope=scope, get_image=unavailable)

def read_notebook_units(source, path, artifact, scope):
    notebook = json.loads(path.read_bytes())
    cells = notebook.get('cells', [])
    start, end = interval(scope, 'notebook_cells', len(cells))
    result = []
    for index in range(start, end + 1):
        cell = cells[index - 1]
        locator = {'kind': 'notebook_cells', 'start': index, 'end': index}
        references, images = ({}, {})

        def register(raw, mime):
            reference = asset_reference(raw, mime)
            digest = reference['sha256']
            if digest not in references:
                at = {**locator, 'object': 'embedded_asset:' + digest}
                unit = image_part(raw, locator=at, artifact=artifact, original_name=str(index))
                unit['provenance']['asset_sha256'] = digest
                images[digest], references[digest] = (unit, reference)
            return reference

        def bundle(data):
            if not isinstance(data, dict):
                raise SourceContentReadError('The original notebook MIME bundle is invalid.')
            return {mime: register(decode_image_bundle(mime, value), mime) if mime.startswith('image/') else value for mime, value in data.items()}
        text_cell = dict(cell)
        text_cell['outputs'] = [{**output, 'data': bundle(output.get('data', {}))} for output in cell.get('outputs', [])]
        if cell.get('attachments'):
            text_cell['attachments'] = {name: bundle(data) for name, data in cell['attachments'].items()}

        def inline(uri):
            mime, raw = decode_inline_image(uri)
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in references and artifact.get('_original_read_exception') != 'explicit_switch_off':
                mapped = any((part.get('locator', {}).get('original_asset_sha256') == digest and str(part.get('locator', {}).get('container')) == str(index) for part in artifact.get('parts', [])))
                if not mapped:
                    raise SourceContentReadError('This inline notebook image has no original-asset Layout mapping.')
            return register(raw, mime)
        if cell.get('cell_type') == 'markdown':
            body = ''.join(cell.get('source', []))
            links = re.findall('!\\[[^\\]]*\\]\\(([^)]+)\\)', body)
            if re.search('<(?:svg|canvas|object|iframe)\\b', body, re.I) or any((not uri.startswith('data:image/') and (not uri.startswith('attachment:') or uri.removeprefix('attachment:') not in cell.get('attachments', {})) for uri in links)):
                raise SourceContentReadError('The original notebook references unmapped visual resources.')
            html = _DOM()
            html.feed(body)
            if any((node['tag'] == 'img' and (not node['attributes'].get('src', '').startswith(('data:image/', 'attachment:'))) for node in html.nodes)):
                raise SourceContentReadError('The original notebook references an external image.')
        text_cell = replace_inline_images(text_cell, inline)
        representation = structured_asset_provenance(references.values()) if references else {}
        result.append(text_part(json.dumps(text_cell, ensure_ascii=False), locator, **representation))
        result.extend(images.values())
    return result
