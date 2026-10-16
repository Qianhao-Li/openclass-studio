from __future__ import annotations
import base64
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
import zipfile
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from .config import GoogleSourceError
MAX_RESOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024

def public_addresses(url, resolver=socket.getaddrinfo):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or (parsed.port not in {None, 80, 443}) or any((ord(char) < 32 for char in url)):
        raise GoogleSourceError('The webpage must use a public HTTP or HTTPS URL without embedded credentials.')
    try:
        addresses = list(dict.fromkeys((item[4][0] for item in resolver(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM))))
    except OSError as exc:
        raise GoogleSourceError('The webpage hostname could not be resolved.') from exc
    if not addresses or any((not ipaddress.ip_address(address).is_global for address in addresses)):
        raise GoogleSourceError('Access to private, local, or cloud metadata addresses is not allowed.')
    return (parsed, addresses)

def fetch_public(url, *, resolver=socket.getaddrinfo):
    """Pin connection to the validated IP; DNS cannot change between check and connect."""
    for _ in range(6):
        parsed, addresses = public_addresses(url, resolver)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=25)
        try:
            sock = socket.create_connection((addresses[0], port), timeout=25)
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'Host': parsed.netloc, 'User-Agent': 'OpenClassSourceSnapshot/1.0', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                destination = response.getheader('Location')
                if not destination:
                    raise GoogleSourceError('The webpage redirect has no destination URL.')
                url = urljoin(url, destination)
                continue
            if response.status != 200:
                raise GoogleSourceError(f'Webpage download failed (HTTP {response.status}).')
            content = response.read(MAX_RESOURCE_BYTES + 1)
            if len(content) > MAX_RESOURCE_BYTES:
                raise GoogleSourceError('The webpage resource exceeds the 32 MB limit.')
            return (content, response.getheader('Content-Type', 'application/octet-stream').split(';', 1)[0], url)
        except (OSError, http.client.HTTPException) as exc:
            raise GoogleSourceError('The webpage resource could not be downloaded.') from exc
        finally:
            connection.close()
    raise GoogleSourceError('The webpage exceeded the redirect limit.')

class SnapshotHTML(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.styles = []
        self.inline_styles = []
        self.skip = []
        self.style_depth = 0
        self.title = []
        self.title_depth = 0
        self.svg = []
        self.svg_depth = 0
        self.inline_svgs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.svg_depth or tag == 'svg':
            self.svg_depth += 1
            self.svg.append(self.get_starttag_text())
            return
        if tag in {'canvas', 'iframe', 'object', 'embed', 'video'}:
            raise GoogleSourceError('The webpage contains a dynamic canvas or embedded media. Export a complete PDF and upload it instead.')
        if tag in {'script', 'noscript'}:
            self.skip.append(tag)
            return
        if tag == 'style':
            self.style_depth += 1
            return
        if self.skip:
            return
        if values.get('style'):
            self.inline_styles.append(values['style'])
        if tag == 'link' and 'stylesheet' in values.get('rel', '').split():
            if values.get('href'):
                self.styles.append(values['href'])
            return
        if tag in {'img', 'source'}:
            if values.get('src'):
                self.images.append(values['src'])
            if values.get('srcset'):
                self.images.extend((item.strip().split()[0] for item in values['srcset'].split(',') if item.strip()))
            if tag == 'img':
                self.parts.append('<p>' + escape(values.get('alt', 'Image')) + '</p>')
            return
        if tag == 'title':
            self.title_depth += 1
        safe_attrs = ' '.join((f'''{key}="{escape(value or '', quote=True)}"''' for key, value in attrs if key in {'id', 'title', 'colspan', 'rowspan'}))
        self.parts.append('<' + tag + (' ' + safe_attrs if safe_attrs else '') + '>')

    def handle_startendtag(self, tag, attrs):
        if self.svg_depth:
            self.svg.append(self.get_starttag_text())
        elif tag == 'svg':
            self.inline_svgs.append(self.get_starttag_text().encode())
        else:
            self.handle_starttag(tag, attrs)
            if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
                self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.svg_depth:
            self.svg.append('</' + tag + '>')
            self.svg_depth -= 1
            if not self.svg_depth:
                self.inline_svgs.append(''.join(self.svg).encode())
                self.svg = []
            return
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == 'style':
            self.style_depth = max(0, self.style_depth - 1)
            return
        if tag == 'title':
            self.title_depth = max(0, self.title_depth - 1)
        self.parts.append('</' + tag + '>')

    def handle_data(self, data):
        if self.svg_depth:
            self.svg.append(escape(data))
        elif self.style_depth:
            self.inline_styles.append(data)
        elif not self.skip:
            self.parts.append(escape(data))
            if self.title_depth:
                self.title.append(data)

def make_web_snapshot(url, *, fetch=fetch_public):
    content, mime, final_url = fetch(url)
    if mime not in {'text/html', 'application/xhtml+xml', 'text/plain'}:
        raise GoogleSourceError('This URL does not contain a webpage or text. Import it by uploading the file.')
    try:
        text = content.decode('utf-8-sig')
    except UnicodeError as exc:
        raise GoogleSourceError('The webpage is not encoded as UTF-8. Export it to a file and upload it instead.') from exc
    parser = SnapshotHTML()
    parser.feed(text)
    images = list(parser.images)
    assets = []
    total = len(content)
    visited = set()

    def fetch_asset(relative, base):
        nonlocal total
        if relative.startswith('data:'):
            header, encoded = relative.split(',', 1)
            if ';base64' not in header:
                raise GoogleSourceError('The embedded image encoding is unsupported.')
            data = base64.b64decode(encoded, validate=True)
            media = header[5:].split(';', 1)[0]
            resolved = 'inline:' + hashlib.sha256(data).hexdigest()
        else:
            data, media, resolved = fetch(urljoin(base, relative))
        total += len(data)
        if len(data) > MAX_RESOURCE_BYTES or total > MAX_TOTAL_BYTES:
            raise GoogleSourceError('The webpage snapshot exceeds the total resource size limit.')
        return (data, media, resolved)
    css_queue = [(item, final_url) for item in parser.styles]
    css_texts = [(item, final_url) for item in parser.inline_styles]
    while css_queue:
        relative, base = css_queue.pop(0)
        key = urljoin(base, relative)
        if key in visited:
            continue
        visited.add(key)
        if len(visited) > 100:
            raise GoogleSourceError('The webpage exceeds the stylesheet resource limit.')
        data, media, resolved = fetch_asset(relative, base)
        css_texts.append((data.decode('utf-8'), resolved))
        assets.append({'content': data, 'mime': media, 'url': resolved, 'kind': 'style'})
        for imported in re.findall('@import\\s+(?:url\\()?\\s*[\\"\\\']([^\\"\\\']+)', data.decode('utf-8'), re.I):
            css_queue.append((imported, resolved))
    for css, base in css_texts:
        for relative in re.findall('url\\(\\s*[\\"\\\']?([^\\)\\"\\\']+)', css, re.I):
            if not relative.startswith('#'):
                images.append(urljoin(base, relative))
    for relative in dict.fromkeys(images):
        if len(assets) > 300:
            raise GoogleSourceError('The webpage exceeds the resource count limit.')
        data, media, resolved = fetch_asset(relative, final_url)
        if media.startswith('image/'):
            assets.append({'content': data, 'mime': media, 'url': resolved, 'kind': 'image'})
        elif media.startswith('font/') or media in {'application/font-woff', 'application/vnd.ms-fontobject', 'text/css'}:
            assets.append({'content': data, 'mime': media, 'url': resolved, 'kind': 'style'})
        else:
            raise GoogleSourceError('A webpage visual resource returned an unsupported content type.')
    for index, svg in enumerate(parser.inline_svgs):
        assets.append({'content': svg, 'mime': 'image/svg+xml', 'url': f'{final_url}#svg-{index}', 'kind': 'image'})
    output = io.BytesIO()
    manifest = {'version': 1, 'url': final_url, 'title': ''.join(parser.title).strip(), 'assets': []}
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('original.html', content)
        archive.writestr('content.html', ''.join(parser.parts))
        for index, asset in enumerate(assets):
            name = f'assets/{index}'
            archive.writestr(name, asset['content'])
            manifest['assets'].append({'name': name, 'mime': asset['mime'], 'url': asset['url'], 'kind': asset['kind'], 'sha256': hashlib.sha256(asset['content']).hexdigest()})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    return (output.getvalue(), manifest)
