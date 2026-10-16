from __future__ import annotations
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from openclass_core.models import BoardDocument, BranchRef, CommitRecord, LearningRequirementSheet, Lesson, LessonHistoryGraph, TeachingGuide, new_id
from openclass_core.services.renderer import build_document_for_topic_render
from openclass_core.services.rich_document import build_document
UNTITLED_LESSON_TITLE = '无标题'

def default_lesson_title(*, timezone_name: str | None=None) -> str:
    title_timezone = timezone.utc
    if timezone_name:
        try:
            title_timezone = ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return f'New course {datetime.now(title_timezone):%Y-%m-%d %H:%M:%S}'

def slugify(value: str) -> str:
    lowered = re.sub('\\s+', '-', value.strip().lower())
    lowered = re.sub('[^a-z0-9\\-\\u4e00-\\u9fff]+', '-', lowered)
    return lowered.strip('-') or new_id('lesson')

def _clean_topic(topic: str) -> str:
    return re.sub('\\s+', ' ', topic or '').strip() or '新学习主题'

def build_requirements(topic: str) -> LearningRequirementSheet:
    normalized_topic = _clean_topic(topic)
    return LearningRequirementSheet(theme=normalized_topic, learning_goal='', level='', known_background='', current_questions=[], learning_need_checklist=[], target_depth='', output_preference='', boundary='', board_scope=[], success_criteria='', board_workflow='unknown')

def build_teaching_guide(lesson_id: str, lesson_title: str, document: BoardDocument, requirements: LearningRequirementSheet | None=None) -> TeachingGuide:
    return TeachingGuide(lesson_id=lesson_id, summary='', structure_note='', pacing='', mappings=[], strategy='')

def _initial_history(document: BoardDocument, *, auto_title_pending: bool=False, auto_title_timezone: str | None=None) -> LessonHistoryGraph:
    commit = CommitRecord(label='Initial document', message=f'Generated starter rich document for {document.title}', branch_name='main', snapshot=document, metadata={'kind': 'initial_document', 'history_node_kind': 'system', 'history_node_title': 'Initial document', 'history_node_summary': f'Generated starter rich document for {document.title}', 'active_requirement_sheet_after': None, 'active_board_task_sheet_after': None, 'auto_title_pending': auto_title_pending, 'auto_title_default': document.title if auto_title_pending else None, 'auto_title_timezone': auto_title_timezone})
    return LessonHistoryGraph(branches={'main': BranchRef(name='main', head_commit_id=commit.id, base_commit_id=commit.id, document_cursor_commit_id=commit.id)}, commits=[commit], current_branch='main')

def create_lesson(title: str, *, requirements: LearningRequirementSheet | None=None) -> Lesson:
    clean_title = _clean_topic(title)
    document = build_document_for_topic_render(clean_title)
    lesson_id = new_id('lesson')
    guide = build_teaching_guide(lesson_id, clean_title, document)
    return Lesson(id=lesson_id, title=clean_title, slug=slugify(clean_title), summary='', tags=[], board_document=document, learning_requirements=None, teaching_guide=guide, history_graph=_initial_history(document))

def create_empty_lesson(title: str, *, auto_title_pending: bool=False, auto_title_timezone: str | None=None) -> Lesson:
    clean_title = _clean_topic(title)
    document = build_document(title=clean_title)
    lesson_id = new_id('lesson')
    guide = build_teaching_guide(lesson_id, clean_title, document)
    return Lesson(id=lesson_id, title=clean_title, slug=slugify(clean_title), summary='', tags=[], board_document=document, learning_requirements=None, teaching_guide=guide, history_graph=_initial_history(document, auto_title_pending=auto_title_pending, auto_title_timezone=auto_title_timezone))

def create_untitled_lesson(*, timezone_name: str | None=None) -> Lesson:
    return create_empty_lesson(default_lesson_title(timezone_name=timezone_name), auto_title_pending=True, auto_title_timezone=timezone_name)
