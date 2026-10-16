"""Cache only visible native PDF glyphs; preserve visual pages as originals."""

def extract_pdf_units(path, is_cancelled):
    from openclass_core.services import pdf_backend as fitz
    from openclass_core.services.source_basic_formats import BasicContentError
    units, entries = ([], [])
    with fitz.open(path) as pdf:
        for index, page in enumerate(pdf, 1):
            if is_cancelled():
                raise BasicContentError('Source parsing was cancelled.')
            graphical = bool(page.get_images() or page.get_drawings())
            traces = page.get_texttrace()
            covers = [(order, fitz.Rect(box)) for order, (kind, box) in enumerate(page.get_bboxlog()) if kind == 'fill-image']
            visible = bool(traces) and all((span.get('type') in (0, 1) and span.get('opacity', 1) >= 0.99 and all((char[0] != 65533 and char[0] >= 0 and (char[1] >= 0) for char in span.get('chars', ()))) and (not any((order > span['seqno'] and box.intersects(fitz.Rect(span['bbox'])) for order, box in covers))) for span in traces))
            blocks = page.get_text('blocks', sort=True, flags=fitz.TEXTFLAGS_BLOCKS & ~fitz.TEXT_PRESERVE_IMAGES)
            text_blocks = [block for block in blocks if len(block) > 6 and block[6] == 0]
            if not text_blocks:
                units.append(('', {'kind': 'pdf_pages', 'start': index, 'end': index, 'metadata': {'requires_original_image': graphical}}))
                continue
            for block in text_blocks:
                units.append((block[4], {'kind': 'pdf_pages', 'start': index, 'end': index, 'metadata': {'native_bbox': list(block[:4]), 'native_visibility_verified': visible, 'requires_original_image': graphical or not visible}}))
        outline = pdf.get_toc()
        for index, (level, title, start) in enumerate(outline):
            if not 1 <= start <= len(pdf):
                continue
            end = next((p - 1 for l, _, p in outline[index + 1:] if l <= level and p > start), len(pdf))
            entries.append({'title': title, 'level': level, 'range': {'kind': 'pdf_pages', 'start': start, 'end': end}})
    return (units, entries, bool(entries))
