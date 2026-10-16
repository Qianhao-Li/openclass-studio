"""Validate host-issued PDF detail evidence for independent heading observations.

This reads catalog runtime checkpoints only; it grants no course or history writes.
"""
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_image_detail import validate_region
from openclass_core.services.source_catalog_refinement_evidence import range_was_read, read_units
MAX_REGION_AREA = 0.35

def detail_reads(task, page, read_ids, headings, *, merged_revision, fresh=False):
    from openclass_core.services.source_catalog_fragments import _reads, _owns
    regions, identities = ([], [])
    expected = {'epoch': task.get('refinement_epoch') or task['request_id'], 'content_hash': task['content_hash'], 'source_version': task['source_version'], 'plan_revision': page['plan_revision'], 'merged_revision': merged_revision, 'complete': True}
    for identity in read_ids:
        entry = task.get('reads', {}).get(identity, {})
        context = entry.get('catalog_fragment_context', {})
        if any((context.get(key) != value for key, value in expected.items())):
            continue
        if not range_was_read(SourceRange.model_validate(page['source_range']), read_units(task, [identity])):
            continue
        for detail in entry.get('image_details', []):
            if detail.get('detail_source') != 'original_pdf' or detail.get('source_content_hash') != task['content_hash'] or detail.get('source_version') != task['source_version'] or (detail.get('physical_page') != page['source_range']['start']) or (type(detail.get('render_dpi')) is not int) or (detail['render_dpi'] < 300):
                continue
            try:
                region = validate_region(detail.get('region'))
                if region is None or (region[2] - region[0]) * (region[3] - region[1]) > MAX_REGION_AREA:
                    continue
                if fresh:
                    _reads(task, page['source_range'], [identity], page=page, region=region, latest=True)
            except GoogleSourceError:
                continue
            regions.append(region)
            identities.append(identity)
    if any((not any((_owns(region, heading['ownership_point']) for region in regions)) for heading in headings)):
        raise GoogleSourceError('Every heading needs a fresh high-resolution original PDF detail read covering its position. Use pdf_page_image with explicit regions of at most 35% of the page; include those read IDs with the whole-page read.')
    return list(dict.fromkeys(identities))
