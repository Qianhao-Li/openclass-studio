"""Render source QA citation labels from verified identities, never model numbering."""
from __future__ import annotations
import re

def render_source_qa_citations(message, cited_ids, numbered_evidence, citations):
    available = {item.id for item in numbered_evidence}
    if any((identity not in available for identity in cited_ids)):
        raise ValueError('The source QA role cited evidence outside the frozen bundle')
    numbers = {item.id: item.metadata.get('citation_numbers', []) for item in numbered_evidence}
    if any(numbers.values()):
        if re.search('\\[\\^\\d+\\]', message):
            raise ValueError('Source QA must cite verified evidence IDs; citation numbering is assigned by the backend')
        for identity in cited_ids:
            if numbers.get(identity):
                message = message.replace(f'[{identity}]', ''.join((f'[^{number}]' for number in numbers[identity])))
    by_id = {item.evidence_id: item for item in citations}
    selected = [by_id[identity].model_copy(update={'citation_number': numbers[identity][0] if len(numbers.get(identity, [])) == 1 else by_id[identity].citation_number}) for identity in cited_ids if identity in by_id]
    return (message, selected)
