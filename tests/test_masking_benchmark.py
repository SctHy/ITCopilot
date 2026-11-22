"""Guard against benchmark overclaiming: partial masks, overmasking and types."""
import pytest

from tests.support.masking_benchmark import emitted_spans, score


def test_partial_mask_is_not_full_protection():
    s = score("password ABCDEF", [(9, 15, "SECRET")], [(9, 12, "SECRET")])
    assert s["tp"] == 0 and s["fully_covered"] == 0
    assert s["covered_gold_chars"] == 3 and s["leaking_document"]


def test_overmask_covers_but_is_not_exact_and_counts_damage():
    s = score("abc XYZ def", [(4, 7, "PERSON")], [(0, 11, "PERSON")])
    assert s["fully_covered"] == 1 and s["tp"] == 0
    assert s["masked_non_gold_chars"] == 8


def test_wrong_type_is_separate_from_safety_coverage():
    s = score("123456", [(0, 6, "SECRET")], [(0, 6, "ACCOUNT")])
    assert s["tp"] == 0 and s["untyped_tp"] == 1 and s["fully_covered"] == 1


def test_union_coverage_and_duplicate_gold():
    s = score("ABCDEF", [(0, 6, "SECRET")], [(0, 3, "SECRET"), (3, 6, "SECRET")])
    assert s["fully_covered"] == 1 and s["tp"] == 0


def test_offset_reconstruction_tracks_occurrences_not_all_known_values():
    token = "{{PERSON_" + "a" * 24 + "}}"
    original = "张伟 / 张伟"
    assert emitted_spans(original, token + " / 张伟", {token: "张伟"}) == [(0, 2, "PERSON")]
    with pytest.raises(ValueError, match="round-trip"):
        emitted_spans(original, token + "/张伟", {token: "张伟"})


def test_unlabeled_document_does_not_inflate_recall():
    s = score("version 1.2", [], [])
    assert s["gold"] == 0 and s["fully_covered"] == 0 and not s["leaking_document"]
    assert s["negative_document"] and not s["negative_changed"]


def test_paired_coverage_does_not_hide_losses_behind_equal_net_gain():
    from tests.support.compare_masking_runs import coverage_changes
    gold = [(0, 5, "ACCOUNT"), (6, 11, "ACCOUNT"), (12, 17, "ID")]
    before = {1: {"gold_spans": gold, "full_flags": [True, False, False]}}
    after = {1: {"gold_spans": gold, "full_flags": [False, True, True]}}
    result = coverage_changes(before, after)
    assert result["ACCOUNT"] == {"gold": 2, "before_fully_covered": 1, "after_fully_covered": 1, "gained": 1, "lost": 1}
    assert result["ID"]["gained"] == 1 and result["ID"]["lost"] == 0
    with pytest.raises(AssertionError, match="Documents differ"):
        coverage_changes(before, {})
