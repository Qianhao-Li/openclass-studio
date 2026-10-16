from copy import deepcopy

import pytest

from openclass_core.services.ridoc_range_actions import compile_range_action, validate_compiled_action
from openclass_core.services.ridoc_range_edit import range_actions_view
from openclass_core.services.ridoc_v3 import content_hash
from test_ridoc_learning_sequence import Course, document
from test_ridoc_range_history import doc


@pytest.mark.parametrize("kind,expected", [
    ("repeated", "44678df6d7d81f9c1f29af2f523ab0349543305d2c488c10b5d6409d4f911092"),
    ("document", "3acb42db6585f6e30b4f502fc22de1c0cf103522b5ab32aac43de0bb1040e032"),
])
def test_english_ui_preserves_pre_release_compiled_proof_hashes(kind, expected):
    # Hashes captured from the production range compiler before UI translation.
    before = doc("aaaa") if kind == "repeated" else doc("abc")
    after = doc("aaa") if kind == "repeated" else before.model_copy(update={"title": "Changed title"})
    action = compile_range_action(before, after)
    assert content_hash(action) == expected
    validate_compiled_action(before, after, action)


def test_range_history_translates_display_without_rewriting_proof_or_document(tmp_path):
    course = Course(tmp_path)
    course.write("commit_document", transaction_id="repeated-before", document=document("aaaa"))
    course.write("commit_document", transaction_id="repeated-after", document=document("aaa"))
    archive = course.archive()
    original = deepcopy(archive)
    rows = range_actions_view(archive, course.lesson.id, "main")
    assert any("Repeated content has multiple possible alignments" in row["summary"] for row in rows)
    blocked = range_actions_view(archive, course.lesson.id, "main", blocked_reason="文档结构无法可靠定位文本范围")
    assert any(row["blocked_reason"] == "The document structure cannot reliably locate the text range" for row in blocked)
    assert all(not row["blocked_reason"] or row["blocked_reason"].isascii() for row in blocked)
    assert archive == original
