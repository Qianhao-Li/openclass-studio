"""Finite source scopes support complete reading through bounded continuations."""
from openclass_core.services.source_content_contract import SourceReadBudget

def task_read_budget(event, task):
    if 'source_read_budget' in event:
        return SourceReadBudget(**event['source_read_budget'])
    if task.get('source_access_mode') == 'explicit' and task.get('source_grant_id') and task.get('authorized_refs'):
        from openclass_core.services.source_evidence_store import source_evidence_store
        sources = [source_evidence_store.get_source(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=ref.get('source_ingestion_id')) for ref in task['authorized_refs'] if ref.get('source_ingestion_id')]
        if sources and all((source and source.metadata.get('source_processor') == 'anythingllm' for source in sources)):
            return SourceReadBudget.continuation()
    return SourceReadBudget.continuation()
