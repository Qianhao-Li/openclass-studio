import pytest

from openclass_core.models import BoardDocument
from openclass_core.services.latex_validation import DocumentMathError, _check_formulas, validate_document_math
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_v3 import RidocV3Error
from test_ridoc_learning_sequence import Course


def math_document(latex, *, display=False):
    node = {"type": "blockMath" if display else "inlineMath", "attrs": {"latex": latex}}
    return rebuild_document_from_content_json(BoardDocument(id="board", title="Board"), {
        "type": "doc", "content": [node if display else {"type": "paragraph", "content": [node]}],
    })


@pytest.mark.parametrize("latex", [r"\frac{\sigma}{\sqrt{m}}", r"\sqrt[3]{x}", r"\ce{H2O}"])
def test_valid_formulas_use_editor_parser(latex):
    validate_document_math(math_document(latex))


@pytest.mark.parametrize("latex", [r"\frac{1}{\sqrt} m", r"\(x^2\)", r"\unknowncommand{x}", r"\frac{a}{b"])
def test_invalid_syntax_is_rejected_with_formula_location(latex):
    with pytest.raises(DocumentMathError, match="Formula 1:"):
        validate_document_math(math_document(latex))


@pytest.mark.parametrize("method", ["commit_document", "commit_turn"])
def test_invalid_formula_never_commits_half_a_turn_or_changes_archive(tmp_path, method):
    course = Course(tmp_path)
    before = course.store.package_path("owner", course.package.id).read_bytes()
    kwargs = {"transaction_id": "invalid", "document": math_document(r"\frac{1}{\sqrt} m")}
    if method == "commit_turn":
        kwargs.update(user_message="update", assistant_message="done")
    with pytest.raises(RidocV3Error, match="Formula 1:"):
        course.write(method, **kwargs)
    assert course.store.package_path("owner", course.package.id).read_bytes() == before


def test_valid_formula_survives_save_and_store_reopen(tmp_path):
    course = Course(tmp_path)
    document = math_document(r"\frac{\sigma}{\sqrt{m}}")
    course.write("commit_document", transaction_id="correct", document=document)
    loaded = course.store.load_package("owner", package_id=course.package.id).lessons[0]
    assert loaded.board_document.content_json == document.content_json
    from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore
    reopened = RidocWorkspaceStore(course.store.root, tmp_path / "runtime.sqlite3")
    assert reopened.load_package("owner", package_id=course.package.id).lessons[0].board_document == loaded.board_document


def test_missing_parser_fails_closed(monkeypatch):
    _check_formulas.cache_clear()
    monkeypatch.setattr("openclass_core.services.latex_validation.shutil.which", lambda _name: None)
    with pytest.raises(DocumentMathError, match="unavailable"):
        validate_document_math(math_document(r"\sqrt{unique}"))
