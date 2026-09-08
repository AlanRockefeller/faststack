from pathlib import Path
from types import SimpleNamespace

from faststack.app import AppController
from faststack.io.sidecar import _merge_sidecar_payloads
from faststack.models import ImageFile

ORDER = ["a", "b", "c", "d", "e", "f"]


def payload(groups, order=ORDER):
    ranges = []
    for group in groups:
        indices = sorted(order.index(key) for key in group)
        if indices == list(range(indices[0], indices[-1] + 1)):
            ranges.append([indices[0], indices[-1]])
    return {
        "version": 2,
        "entries": {},
        "stacks": ranges,
        "stack_paths": groups,
        "stack_order": list(order),
    }


def merge(base, ours, theirs):
    return _merge_sidecar_payloads(base, ours, theirs)


def test_independent_additions_both_survive():
    result = merge(payload([]), payload([["a", "b"]]), payload([["e", "f"]]))
    assert result["stack_paths"] == [["a", "b"], ["e", "f"]]


def test_identical_addition_does_not_duplicate():
    result = merge(payload([]), payload([["b", "c"]]), payload([["b", "c"]]))
    assert result["stack_paths"] == [["b", "c"]]


def test_removal_and_unrelated_addition_are_both_respected():
    base = payload([["a", "b"]])
    result = merge(base, payload([]), payload([["a", "b"], ["e", "f"]]))
    assert result["stack_paths"] == [["e", "f"]]


def test_conflicting_edits_to_same_stack_use_lock_holder_group():
    base = payload([["b", "c"]])
    ours = payload([["a", "b", "c"]])
    theirs = payload([["b", "c", "d"]])
    result = merge(base, ours, theirs)
    assert result["stack_paths"] == ours["stack_paths"]
    assert result["stacks"] == ours["stacks"]


def test_changed_order_is_resolved_by_identity():
    reversed_order = list(reversed(ORDER))
    result = merge(
        payload([]),
        payload([["a", "b"]], reversed_order),
        payload([["e", "f"]]),
    )
    assert result["stack_order"] == reversed_order
    assert result["stack_paths"] == [["f", "e"], ["b", "a"]]


def test_reordered_stack_is_not_silently_split():
    reordered = ["a", "c", "b", "d", "e", "f"]
    ours = payload([["a", "b"]], reordered)
    result = merge(payload([["a", "b"]]), ours, payload([["a", "b"]]))
    assert result["stack_paths"] == [["a", "b"]]
    assert result["stacks"] == []


def test_distinct_groups_that_become_adjacent_are_not_merged():
    reordered = ["a", "b", "d", "e", "c", "f"]
    groups = [["a", "b"], ["d", "e"]]
    ours = payload(groups, reordered)
    result = merge(payload(groups), ours, payload(groups))
    assert result["stack_paths"] == groups
    assert result["stacks"] == []


def test_incompatible_image_identity_uses_deterministic_fail_safe():
    ours = payload([["a", "b"]], ["a", "b", "c"])
    theirs = payload([["x", "y"]], ["x", "y", "z"])
    result = merge(payload([]), ours, theirs)
    assert result["stack_paths"] == ours["stack_paths"]
    assert result["stack_order"] == ours["stack_order"]


def test_legacy_index_only_payload_remains_backward_compatible():
    base = {"version": 2, "entries": {}, "stacks": [[0, 1]]}
    ours = {"version": 2, "entries": {}, "stacks": [[0, 1]]}
    theirs = {"version": 2, "entries": {}, "stacks": [[3, 4]]}
    result = merge(base, ours, theirs)
    assert result["stacks"] == [[3, 4]]
    assert "stack_paths" not in result
    assert "stack_order" not in result


def _restore(groups, order):
    controller = AppController.__new__(AppController)
    controller.image_files = [ImageFile(Path(key)) for key in order]
    controller.sidecar = SimpleNamespace(
        data=SimpleNamespace(stack_paths=groups, stacks=[[99, 100]]),
        metadata_key_for_path=lambda path: path.name,
    )
    return controller._restore_stacks_from_sidecar_identity()


def test_startup_restoration_preserves_representable_groups():
    assert _restore([["a", "b"], ["e", "f"]], ORDER) == [[0, 1], [4, 5]]


def test_startup_restoration_does_not_split_noncontiguous_group():
    assert _restore([["a", "b"]], ["a", "c", "b", "d"]) == []


def test_startup_restoration_does_not_merge_adjacent_groups():
    assert _restore([["a", "b"], ["d", "e"]], ["a", "b", "d", "e"]) == []


def test_unprojectable_merge_does_not_resurrect_a_removed_image():
    """A concurrently removed image must not come back through the fail-safe.

    Ours (the lock holder) edits stack membership; theirs removes image "c",
    which leaves the two surviving groups adjacent and therefore impossible to
    express as runtime ranges.
    """
    base = payload([["a", "b"], ["d", "e"]])
    ours = payload([["a", "b", "c"], ["d", "e"]])
    without_c = ["a", "b", "d", "e", "f"]
    theirs = payload([["a", "b"], ["d", "e"]], without_c)

    result = merge(base, ours, theirs)

    assert result["stack_order"] == without_c
    assert "c" not in result["stack_order"]
    assert all("c" not in group for group in result["stack_paths"])
    # Surviving identities are retained even though no range can express them.
    assert result["stack_paths"] == [["a", "b"], ["d", "e"]]
    assert result["stacks"] == []


def test_unprojectable_merge_keeps_reordered_identities_without_removed_image():
    """Reordering by the other writer must not revive its removed image."""
    base = payload([["a", "b"]], ["a", "b", "c", "d"])
    ours = payload([["a", "b", "c"]], ["a", "b", "c", "d"])
    reordered = ["a", "d", "b"]
    theirs = payload([["a", "b"]], reordered)

    result = merge(base, ours, theirs)

    assert result["stack_order"] == reordered
    assert result["stack_paths"] == [["a", "b"]]
    assert result["stacks"] == []


def test_overlapping_conflict_fallback_still_drops_a_removed_image():
    """The lock holder wins an overlap, but only over surviving images."""
    without_f = ["a", "b", "c", "d", "e"]
    base = payload([["b", "c"]])
    ours = payload([["a", "b", "c"]])
    theirs = payload([["b", "c", "d"]], without_f)

    result = merge(base, ours, theirs)

    assert result["stack_order"] == without_f
    assert result["stack_paths"] == [["a", "b", "c"]]
    assert result["stacks"] == [[0, 2]]
