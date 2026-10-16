"""Create a blank teaching destination inside an already locked RIDOC mutation."""
from __future__ import annotations
from openclass_core.models import CoursePackage, Lesson
from openclass_core.services.rich_document import build_document
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, new_ridoc_v3
from openclass_core.services.ridoc_v34_states import is_v34

def append_empty_destination(archive: RidocArchiveV3, lesson: Lesson) -> None:
    if lesson.id in archive.package.lessons:
        raise RidocV3ConflictError('The destination lesson already exists')
    initial = lesson.model_copy(deep=True)
    initial.board_document = build_document(title=lesson.title, document_id=lesson.board_document.id, content_text='', page_settings=lesson.board_document.page_settings)
    initial.history_graph.commits = []
    wrapper = CoursePackage(id=archive.package.id, title=archive.package.title, summary=archive.package.summary, lessons=[initial], package_kind='course')
    addition = new_ridoc_v3(wrapper, package_kind='course')
    if is_v34(archive):
        from openclass_core.services.ridoc_v34 import upgrade_archive
        upgrade_archive(addition)
    archive.package.kind = 'course'
    archive.package.lessons[lesson.id] = addition.package.lessons[lesson.id]
    archive.package.lesson_order.append(lesson.id)
    archive.nodes.update(addition.nodes)
    archive.documents.update(addition.documents)
    archive.document_states.update(addition.document_states)
    archive.node_refs.update(addition.node_refs)
    archive.range_proofs.update(addition.range_proofs)
    archive.range_traces.update(addition.range_traces)
