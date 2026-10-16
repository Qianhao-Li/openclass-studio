from __future__ import annotations

from copy import deepcopy

import pytest

from openclass_core.models import BoardDocument
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked
from openclass_core.services.ridoc_range_actions import compile_range_action, same_document_content, validate_compiled_action
from openclass_core.services.ridoc_range_history import build_range_history, replay_range_proof, validate_range_occurrence, validate_range_trace


def doc(*texts, ids=True, marks=None):
    return rebuild_document_from_content_json(BoardDocument(id="board", title="Board"), {
        "type": "doc", "content": [{"type": "paragraph", **({"attrs": {"blockId": str(index)}} if ids else {}),
                                     "content": [{"type": "text", "text": text, **({"marks": marks} if marks else {})}] if text else []}
                                    for index, text in enumerate(texts)]})


def records(*documents):
    return [{"node_id": f"C{index}", "before": before, "after": after}
            for index, (before, after) in enumerate(zip(documents, documents[1:]), 1)]


@pytest.mark.parametrize("ids", [True, False])
def test_c5_then_c3_keeps_c4_including_new_bottom_paragraph(ids):
    states = [doc("甲", "乙", ids=ids), doc("壹", "乙", ids=ids), doc("壹", "贰", ids=ids),
              doc("壹", "贰", "丙", ids=ids), doc("一", "二", "丙", ids=ids), doc("一", "两", "参", ids=ids)]
    actions = records(*states)
    full = build_range_history(states[0], actions)
    assert full["entries"][4]["dependencies"] == ["C3", "C4"]
    assert not full["entries"][2]["can_undo"]
    after_c5 = build_range_history(states[0], actions, ["C5"])
    assert after_c5["entries"][2]["can_undo"]
    result = build_range_history(states[0], actions, ["C5", "C3"])
    assert same_document_content(result["document"], doc("一", "二", ids=ids))
    assert result["entries"][3]["status"] == "active"
    assert len(result["occurrences"]) == 3
    for occurrence in result["occurrences"]:
        source = actions[int(occurrence["node_id"][1:]) - 1]
        validate_range_occurrence(occurrence["proof"], source["before"], source["after"],
                                  occurrence["before"], occurrence["after"], occurrence["input_trace"], occurrence["output_trace"])


def test_same_paragraph_nonoverlapping_edit_survives_length_change():
    states = [doc("甲乙丙丁戊己庚辛"), doc("壹丙丁戊己庚辛"), doc("壹丙丁戊陆辛")]
    result = build_range_history(states[0], records(*states), ["C1"])
    assert same_document_content(result["document"], doc("甲乙丙丁戊陆辛"))
    assert result["entries"][1]["dependencies"] == []


def test_actual_cover_is_blocked_even_when_a_later_action_restores_same_text():
    states = [doc("abc"), doc("azc"), doc("abc"), doc("aQc")]
    result = build_range_history(states[0], records(*states))
    assert result["entries"][1]["dependencies"] == ["C1"]
    assert result["entries"][2]["dependencies"] == ["C2"]
    with pytest.raises(DocumentActionBlocked, match="still depends"):
        build_range_history(states[0], records(*states), ["C2"])


def test_pure_delete_produces_gap_dependency_and_outside_edit_is_independent():
    initial, deleted = doc("abXYZcd"), doc("abcd")
    inserted = doc("ab!cd")
    actions = records(initial, deleted, inserted)
    result = build_range_history(initial, actions)
    assert result["entries"][1]["dependencies"] == ["C1"]
    gaps = next(iter(result["occurrences"][0]["output_trace"]["blocks"].values()))["gaps"]
    assert any(gap["kind"] == "deletion_join" and gap["producer"] == "C1" and gap["deleted_token_ids"] for gap in gaps)
    with pytest.raises(DocumentActionBlocked):
        build_range_history(initial, actions, ["C1"])
    outside = build_range_history(initial, records(initial, deleted, doc("abcD")), ["C1"])
    assert same_document_content(outside["document"], doc("abXYZcD"))


def test_new_content_insertion_inside_produced_slice_depends_on_it():
    states = [doc("甲乙"), doc("甲ABC乙"), doc("甲AB!C乙")]
    result = build_range_history(states[0], records(*states))
    assert result["entries"][1]["dependencies"] == ["C1"]


def test_multi_range_action_is_atomic_and_keeps_input_on_failed_replay():
    states = [doc("甲乙丙丁戊己"), doc("壹乙丙丁戊陆")]
    result = build_range_history(states[0], records(*states))
    occurrence = result["occurrences"][0]
    assert len(occurrence["proof"]["action"]["operations"]) == 2
    trace = deepcopy(occurrence["input_trace"])
    key = trace["block_order"][0]
    trace["blocks"][key]["tokens"][-1]["producer"] = "overwriter"
    before = deepcopy(trace)
    with pytest.raises(DocumentActionBlocked, match="source version"):
        replay_range_proof(occurrence["proof"], states[0], states[1], states[0], trace)
    assert trace == before


def test_unicode_scalar_positions_and_marks_are_typed_changes():
    before = doc("甲😀乙丙")
    after = doc("甲🧠乙丙")
    marked = after.model_copy(deep=True)
    marked.content_json["content"][0]["content"] = [
        {"type": "text", "text": "甲🧠乙"}, {"type": "text", "text": "丙", "marks": [{"type": "bold"}]}]
    marked = rebuild_document_from_content_json(marked, marked.content_json)
    actions = records(before, after, marked)
    assert compile_range_action(before, after)["operations"][0]["start"] == 1
    result = build_range_history(before, actions, ["C1"])
    tokens = next(iter(result["state_trace"]["blocks"].values()))["tokens"]
    assert tokens[1]["value"]["char"] == "😀"
    assert tokens[-1]["value"]["shell"]["marks"] == [{"type": "bold"}]


def test_repeated_alignment_expands_only_ambiguous_region_and_round_trips():
    before, after = doc("甲aaaa乙"), doc("甲aaa乙")
    action = compile_range_action(before, after)
    validate_compiled_action(before, after, action)
    assert action["ambiguities"]
    operation = action["operations"][0]
    assert (operation["start"], operation["end"]) == (1, 5)
    following = doc("甲aaa贰")
    result = build_range_history(before, records(before, after, following), ["C1"])
    assert same_document_content(result["document"], doc("甲aaaa贰"))


def test_compiled_proof_tamper_and_trace_document_mismatch_rejected():
    before, after = doc("abc"), doc("aZc")
    action = compile_range_action(before, after)
    action["operations"][0]["start"] = 0
    with pytest.raises(DocumentActionBlocked, match="original before and after"):
        validate_compiled_action(before, after, action)
    result = build_range_history(before, records(before, after))
    with pytest.raises(DocumentActionBlocked, match="lineage"):
        validate_range_trace(before, result["state_trace"])


def test_saved_state_checkpoint_cannot_hide_effect_of_an_excluded_earlier_action():
    first, after = doc("甲"), doc("乙")
    checkpoint, final = doc("乙", "保留快照"), doc("丙", "保留快照")
    actions = [{"node_id": "C1", "before": first, "after": after},
               {"node_id": "C2", "before": checkpoint, "after": final}]
    result = build_range_history(first, actions)
    assert result["entries"][1]["checkpoint"]
    assert result["entries"][1]["dependencies"] == ["C1"]
    with pytest.raises(DocumentActionBlocked, match="still depends"):
        build_range_history(first, actions, ["C1"])


def test_new_top_paragraph_moves_unchanged_identity_and_unrelated_edit_survives():
    initial = doc("甲", "乙", ids=False)
    inserted = doc("新增", "甲", "乙", ids=False)
    edited = doc("新增", "甲", "贰", ids=False)
    result = build_range_history(initial, records(initial, inserted, edited), ["C1"])
    assert same_document_content(result["document"], doc("甲", "贰", ids=False))
    assert result["entries"][1]["dependencies"] == []


def test_pure_paragraph_deletion_gap_blocks_same_gap_insert_but_not_distant_text():
    initial = doc("甲", "将删", "乙", ids=False)
    deleted = doc("甲", "乙", ids=False)
    inserted = doc("甲", "新段", "乙", ids=False)
    result = build_range_history(initial, records(initial, deleted, inserted))
    assert result["entries"][1]["dependencies"] == ["C1"]
    with pytest.raises(DocumentActionBlocked, match="still depends"):
        build_range_history(initial, records(initial, deleted, inserted), ["C1"])
    unrelated = build_range_history(initial, records(initial, deleted, doc("壹", "乙", ids=False)), ["C1"])
    assert same_document_content(unrelated["document"], doc("壹", "将删", "乙", ids=False))


def test_unchanged_duplicate_paragraphs_do_not_expand_an_unambiguous_edit():
    before, after = doc("甲", "重复", "重复", "乙", ids=False), doc("壹", "重复", "重复", "乙", ids=False)
    action = compile_range_action(before, after)
    assert not action["block_operations"]
    assert len(action["operations"]) == 1 and action["operations"][0]["block_id"] == "path:0"
