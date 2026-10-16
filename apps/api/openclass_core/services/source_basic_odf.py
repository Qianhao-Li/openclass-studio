"""Native ODF positions without conversion, rendering, or OCR."""
from openclass_core.services.source_archive import SafeSourceArchive
from openclass_core.services.source_xml import parse_untrusted_xml

def extract_odf(path, suffix):
    with SafeSourceArchive(path) as archive:
        root = parse_untrusted_xml(archive.read('content.xml'))
    local = lambda value: str(value).rsplit('}', 1)[-1]
    units, entries = ([], [])
    if suffix == '.odp':
        pages = [node for node in root.iter() if local(node.tag) == 'page']
        for index, page in enumerate(pages, 1):
            text = '\n'.join((''.join(node.itertext()) for node in page.iter() if local(node.tag) in {'p', 'h'}))
            locator = {'kind': 'ppt_slides', 'start': index, 'end': index}
            title = next((v for k, v in page.attrib.items() if local(k) == 'name'), f'Slide {index}')
            units.append((text, locator))
            entries.append({'title': title, 'level': 1, 'range': locator})
        return (units, entries, False)
    body = next((node for node in root.iter() if local(node.tag) == 'text'), None)
    if body is None:
        raise ValueError('The ODF document has no text body.')
    for index, node in enumerate((node for node in body.iter() if local(node.tag) in {'p', 'h'}), 1):
        text = ''.join(node.itertext())
        locator = {'kind': 'docx_paragraphs', 'start': index, 'end': index, 'metadata': {'native_format': 'odt', 'position_kind': 'paragraph'}}
        units.append((text, locator))
        if local(node.tag) == 'h':
            level = next((int(v) for k, v in node.attrib.items() if local(k) == 'outline-level'), 1)
            entries.append({'title': text, 'level': level, 'range': locator})
    return (units, entries, bool(entries))
