"""Ordinary content stays ready while catalog/enhancement runtime jobs recover."""

def ordinary_task_active(source):
    return ordinary_metadata_active(source.metadata)

def ordinary_metadata_active(metadata):
    return metadata.get('processing_mode') == 'ordinary' and (metadata.get('enhancement_status') in {'queued', 'processing'} or metadata.get('catalog_status') in {'pending', 'running'})

def interrupted_ordinary_source(source, message):
    enhanced = source.metadata.get('enhancement_status') in {'queued', 'processing'}
    changes = {'enhancement_status': 'interrupted', 'enhancement_error': message, 'enhancement_requested': False, 'enhancement_progress': 100} if enhanced else {'catalog_status': 'ready' if source.metadata.get('catalog_ready') else 'interrupted', 'catalog_error': message, 'catalog_warning': message, 'catalog_progress': 100}
    task = source.metadata.get('catalog_refinement', {})
    if not enhanced and task.get('status') in {'queued', 'running'}:
        changes.update(catalog_refinement={**task, 'status': 'interrupted', 'requested': False}, catalog_refinement_status='interrupted')
    return source.model_copy(update={'status': 'ready' if source.metadata.get('original_read_ready') else 'interrupted', 'error': '' if source.metadata.get('original_read_ready') else message, 'metadata': {**source.metadata, **changes}})
