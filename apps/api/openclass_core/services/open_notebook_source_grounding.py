from __future__ import annotations
import hashlib
from dataclasses import dataclass
from openclass_core.models import EvidenceBundle, LearningClarificationStatus, LearningRequirementAuxiliaryFactor, LearningRequirementChecklistItem, LearningRequirementKeyFact, LearningRequirementSheet, LearningSourceGrounding, LearningSourceReference, Lesson, RetrievalEvidence, SourceIngestionRecord, now_iso
from openclass_core.services.source_evidence_store import source_evidence_store

@dataclass(frozen=True)
class OpenNotebookSourcePlan:
    requirement: LearningRequirementSheet
    clarification: LearningClarificationStatus
    teaching_plan: str

class OpenNotebookSourceGroundingError(RuntimeError):
    pass

def resolve_open_notebook_source_plan(*, owner_user_id: str, package_id: str, lesson: Lesson, source: SourceIngestionRecord, query: str) -> OpenNotebookSourcePlan:
    raise OpenNotebookSourceGroundingError('OpenNotebook search results are navigation metadata, not original source input. Add the original file to Sources and select its verified original range before generating or answering.')

def _open_notebook_evidence(*, source: SourceIngestionRecord, raw_results: list[dict[str, object]]) -> list[RetrievalEvidence]:
    evidence: list[RetrievalEvidence] = []
    used_tokens = 0
    seen: set[str] = set()
    for raw in raw_results:
        expanded_text = _result_text(raw, 'expanded_text', 'context', 'content', 'text', 'snippet', 'chunk')[:3600]
        if not expanded_text:
            continue
        chunk_id = _result_text(raw, 'chunk_id', 'id', 'embedding_id')
        dedupe_key = f'{chunk_id}:{expanded_text[:160]}'
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        token_count = max(1, (len(expanded_text) + 3) // 4)
        if used_tokens and used_tokens + token_count > 6000:
            break
        used_tokens += token_count
        evidence.append(RetrievalEvidence(source_ingestion_id=source.id, open_notebook_source_id=source.open_notebook_source_id, source_title=source.title, source_uri=source.source_uri, section_path=_result_path(raw), page_range=_result_page_range(raw), chunk_ids=[chunk_id] if chunk_id else [], excerpt=expanded_text[:360], expanded_text=expanded_text, relevance_score=_result_score(raw), reason='OpenNotebook search result from the explicitly selected source.', token_count=token_count, metadata={**raw, 'retrieval_mode': 'open_notebook_search_only'}))
        if len(evidence) >= 8:
            break
    return evidence

def _evidence_context_text(evidence: list[RetrievalEvidence]) -> str:
    return '\n\n'.join(('\n'.join((part for part in [item.source_title, ' > '.join(item.section_path), item.page_range, item.expanded_text] if part)) for item in evidence))

def _result_text(raw: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    nested = raw.get('source')
    return _result_text(nested, *keys) if isinstance(nested, dict) else ''

def _result_path(raw: dict[str, object]) -> list[str]:
    value = raw.get('section_path') or raw.get('heading_path') or raw.get('path')
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.replace('>', '/').split('/') if part.strip()]
    return []

def _result_page_range(raw: dict[str, object]) -> str:
    page_range = _result_text(raw, 'page_range', 'pages')
    if page_range:
        return page_range
    page = _result_text(raw, 'page', 'page_no', 'page_number')
    return f'p. {page}' if page else ''

def _result_score(raw: dict[str, object]) -> float:
    for key in ('score', 'relevance_score', 'similarity'):
        value = raw.get(key)
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            continue
    return 0.0

def _dedupe_chunk_ids(evidence: list[RetrievalEvidence]) -> list[str]:
    return list(dict.fromkeys((chunk_id for item in evidence for chunk_id in item.chunk_ids if chunk_id)))

def _evidence_hash(evidence: list[RetrievalEvidence]) -> str:
    content = '\n'.join((item.expanded_text for item in evidence if item.expanded_text))
    return hashlib.sha256(content.encode('utf-8')).hexdigest()
