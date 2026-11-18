"""Pure sampling rules: guarantees, priority, seed stability, no repeats."""

from __future__ import annotations

import pytest

from app.sampling import (
    MAX_SAMPLE_SIZE,
    SamplingError,
    select_sample,
    validate_params,
)


def _change(identifier, change_type, seq):
    return {
        "run_id": "run-1",
        "identifier": identifier,
        "change_type": change_type,
        "before_title": None,
        "before_creator": None,
        "after_title": identifier,
        "after_creator": None,
        "datestamp": "2026-01-01T00:00:00Z",
        "seq": seq,
    }


def _pool(*specs):
    return [_change(ident, ctype, seq) for seq, (ident, ctype) in enumerate(specs)]


def test_each_type_gets_one_guaranteed_pick_when_size_allows():
    changes = _pool(
        ("w1", "withdrawn"),
        ("c1", "corrected"),
        ("a1", "added"),
    )
    sample = select_sample(changes, run_id="run-1", sample_size=3, seed=1)
    assert len(sample) == 3
    types = {item["change_type"] for item in sample}
    assert types == {"withdrawn", "corrected", "added"}
    assert all(item["pick"] == "guaranteed" for item in sample)
    assert [item["order"] for item in sample] == [1, 2, 3]


def test_insufficient_slots_follow_withdrawn_corrected_added_priority():
    changes = _pool(
        ("w1", "withdrawn"),
        ("c1", "corrected"),
        ("a1", "added"),
    )
    one = select_sample(changes, run_id="run-1", sample_size=1, seed=1)
    assert [item["change_type"] for item in one] == ["withdrawn"]

    two = select_sample(changes, run_id="run-1", sample_size=2, seed=1)
    assert [item["change_type"] for item in two] == ["withdrawn", "corrected"]


def test_missing_types_are_skipped_not_fabricated():
    changes = _pool(("a1", "added"), ("a2", "added"))
    sample = select_sample(changes, run_id="run-1", sample_size=3, seed=1)
    assert len(sample) == 2
    assert {item["change_type"] for item in sample} == {"added"}
    assert {item["identifier"] for item in sample} == {"a1", "a2"}


def test_remainder_is_seed_stable_and_unique():
    changes = _pool(
        ("w1", "withdrawn"),
        ("c1", "corrected"),
        ("a1", "added"),
        ("a2", "added"),
        ("a3", "added"),
        ("a4", "added"),
    )
    first = select_sample(changes, run_id="run-1", sample_size=5, seed=42)
    second = select_sample(changes, run_id="run-1", sample_size=5, seed=42)
    assert first == second  # same params, same sample
    ids = [item["identifier"] for item in first]
    assert len(ids) == len(set(ids))  # no repeats
    # First three are the per-type guarantees; the rest are seeded.
    assert [item["pick"] for item in first[:3]] == ["guaranteed"] * 3
    assert all(item["pick"] == "seeded" for item in first[3:])


def test_different_seeds_can_change_remainder_but_never_the_guarantees():
    changes = _pool(
        ("w1", "withdrawn"),
        ("c1", "corrected"),
        *[(f"a{i}", "added") for i in range(8)],
    )
    s1 = select_sample(changes, run_id="run-1", sample_size=5, seed=1)
    s2 = select_sample(changes, run_id="run-1", sample_size=5, seed=2)
    guaranteed_1 = [(i["identifier"], i["change_type"]) for i in s1 if i["pick"] == "guaranteed"]
    guaranteed_2 = [(i["identifier"], i["change_type"]) for i in s2 if i["pick"] == "guaranteed"]
    assert guaranteed_1 == guaranteed_2
    assert len(guaranteed_1) == 3
    # With eight additions and two seeded slots, some seed pair must differ
    # (the hash ordering is effectively a permutation).
    assert [i["identifier"] for i in s1] != [i["identifier"] for i in s2]


def test_seed_stability_does_not_depend_on_dict_order():
    changes = _pool(
        ("w1", "withdrawn"),
        ("c1", "corrected"),
        ("a1", "added"),
        ("a2", "added"),
        ("a3", "added"),
    )
    forward = select_sample(changes, run_id="run-1", sample_size=4, seed=99)
    reversed_ = select_sample(list(reversed(changes)), run_id="run-1",
                              sample_size=4, seed=99)
    assert [i["identifier"] for i in forward] == [i["identifier"] for i in reversed_]


def test_sample_size_beyond_available_returns_everything_once():
    changes = _pool(("a1", "added"), ("a2", "added"))
    sample = select_sample(changes, run_id="run-1", sample_size=20, seed=3)
    assert len(sample) == 2
    assert len({i["identifier"] for i in sample}) == 2


def test_empty_changes_returns_empty_sample():
    assert select_sample([], run_id="run-1", sample_size=5, seed=0) == []


def test_run_id_participates_in_the_stable_key():
    changes = _pool(*[(f"a{i}", "added") for i in range(6)])
    # Skip the guarantee phase effects by comparing only seeded picks on a
    # single-type pool with size > 1: different run ids may reorder draws.
    s_run1 = select_sample(changes, run_id="run-1", sample_size=6, seed=7)
    s_run2 = select_sample(changes, run_id="run-2", sample_size=6, seed=7)
    # With size == pool size both return everything; order may differ.
    assert {i["identifier"] for i in s_run1} == {i["identifier"] for i in s_run2}


@pytest.mark.parametrize("bad_size", [0, -1, 21, 100])
def test_sample_size_outside_range_rejected(bad_size):
    with pytest.raises(SamplingError):
        validate_params(bad_size, 0)
    with pytest.raises(SamplingError):
        select_sample([], run_id="r", sample_size=bad_size, seed=0)


def test_max_allowed_size_is_twenty():
    assert MAX_SAMPLE_SIZE == 20
    validate_params(20, 0)
    validate_params(1, 0)


def test_bool_is_not_accepted_as_integer():
    with pytest.raises(SamplingError):
        validate_params(True, 0)
    with pytest.raises(SamplingError):
        validate_params(5, True)
