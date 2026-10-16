"""Adapt a Sources runtime task to the chapter-boundary evidence contract."""
from openclass_core.services.source_catalog_refinement_evidence import RefinementReport

def parameters(task, source=None):
    metadata = source.metadata if source is not None else {}
    count = metadata.get('catalog_pdf_page_count') or metadata.get('page_count') or task.get('scope_pdf_page_count', 0)
    pages = {page for unit in task.get('directory_transcription', {}).get('units', []) if unit.get('complete') and unit.get('source_range', {}).get('kind') == 'pdf_pages' for page in range(unit['source_range']['start'], unit['source_range']['end'] + 1)}
    return (RefinementReport.model_validate(task.get('report') or {}).nodes, {'page_count': int(count), 'directory_pages': pages})

def current_work(source, task):
    if not task.get('scope_target_key'):
        return None
    from openclass_core.services.source_catalog_scope_boundaries import work
    nodes, options = parameters(task, source)
    return work(task, nodes, **options)

def read_context(source, task):
    step = task.get('execution', {}).get('current_step', {})
    if not task.get('scope_target_key') or step.get('stage') != 'verify_chapter_scope' or step.get('finished_at'):
        return {}
    from openclass_core.services.source_catalog_scope_boundaries import scope_read_context
    nodes, options = parameters(task, source)
    context = scope_read_context(task, nodes, **options)
    if not context or any((step.get(key) != context.get(key) for key in ('plan_digest', 'physical_page', 'pass_index'))):
        return {}
    return context

def progress(task):
    if not task.get('scope_target_key'):
        return None
    from openclass_core.services.source_catalog_scope_boundaries import progress as scope_progress
    nodes, options = parameters(task)
    return scope_progress(task, nodes, **options)
