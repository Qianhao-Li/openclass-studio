"""Choose the complete original directory as model input; keep body reading native."""
from __future__ import annotations
from dataclasses import dataclass
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_attachment_locations import heading_label_key
from openclass_core.services.source_basic_toc import PrintedDirectory, title_key

@dataclass(frozen=True)
class AttachmentDirectoryScope:
    units: list
    mode: str

def directory_input_scope(data, printed: PrintedDirectory | None):
    """A TOC is a complete native block, never a selection of search matches."""
    full = AttachmentDirectoryScope(data['units'], 'full_original')
    if not printed or not printed.pages or (not printed.rows):
        return full
    pages = set(printed.pages)
    units = [unit for unit in data['units'] if unit['locator']['kind'] == 'pdf_pages' and unit['locator']['start'] in pages]
    if {unit['id'] for unit in units} != set(printed.unit_ids):
        return full
    labels = {heading_label_key(row.title) for row in printed.rows}
    original = heading_label_key('\n'.join(printed.page_text.values()))
    for entry in data.get('native_entries', []):
        locator = entry['range']
        if locator.get('kind') != 'pdf_pages' or locator.get('start', 0) <= max(pages):
            continue
        key = heading_label_key(entry['title'])
        if key not in labels and key not in original:
            return full
    return AttachmentDirectoryScope(units, 'original_directory')

def validate_directory_rows(printed: PrintedDirectory | None, rows):
    """Check every independently parsed declaration, including repeated titles."""
    if not printed:
        return
    remaining = list(rows)
    for declaration in printed.rows:
        key = heading_label_key(declaration.title)
        matched = next((index for index, row in enumerate(remaining) if heading_label_key(row.title) == key or _same_numeric_line(declaration, row)), None)
        if matched is None:
            raise GoogleSourceError('The model omitted an entry from the complete original directory.')
        remaining.pop(matched)

def _same_numeric_line(declaration, row):
    """A PDF may glue a title's terminal numeral to the printed page label.

    Compare the entire declared title+page, rather than guessing either number.
    Both model fields must reconstruct exactly the independently observed line.
    """
    page = title_key(row.printed_page_label)
    return declaration.printed_page is not None and page.isdigit() and (heading_label_key(declaration.title) + str(declaration.printed_page) == heading_label_key(row.title) + page)

def verify_native_directory_objects(service, source, data):
    """An authored cover/image bookmark is an original object, not OCR text.

    Reading the text TOC must not require a vision model to transcribe unrelated
    covers. The original native label and an actually readable original image are
    sufficient for these objects, as in the existing native-navigation path.
    Undeclared image headings are never inferred by this verification.
    """
    destinations = {entry['range'].get('start') for entry in data.get('native_entries', []) if entry['range'].get('kind') == 'pdf_pages'}
    text_pages = {unit['locator']['start'] for unit in data['units'] if unit['text'].strip()}
    pending = [unit for unit in data['units'] if not unit['text'].strip() and unit['locator']['kind'] == 'pdf_pages' and (unit['locator']['start'] not in text_pages) and (unit['locator']['start'] in destinations)]
    if not pending:
        return []
    if len(pending) > 60:
        raise GoogleSourceError('The original-object catalog validation budget was reached.')
    import hashlib
    from openclass_core.services.google_sources.original_cache import original_path
    from openclass_core.services.source_content_reader import _pdf_parts
    service._check(source)
    path = original_path(source)
    if hashlib.sha256(path.read_bytes()).hexdigest() != data['content_hash']:
        raise GoogleSourceError('The original PDF changed during native-object verification.')
    verified = []
    for unit in pending:
        service._check(source)
        page = unit['locator']['start']
        output = _pdf_parts(path, source, {}, {'kind': 'pdf_pages', 'start': page, 'end': page}, 0, 1, 40 * 1024 * 1024)
        if len(output['parts']) != 1 or output['parts'][0].get('kind') != 'image':
            raise GoogleSourceError('An original native directory object has no readable image.')
        verified.append(unit['id'])
    service._check(source)
    return verified
