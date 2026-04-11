"""Tests for sort-mode feature (default / filename / date)."""

from unittest.mock import MagicMock, patch

from faststack.models import ImageFile


def _make_images(tmp_path, specs):
    """Create ImageFile objects from (name, timestamp) pairs.

    Files are touched on disk so that Path objects are valid, but sorting
    uses the ImageFile.timestamp field, not live stat() calls.
    """
    imgs = []
    for name, ts in specs:
        p = tmp_path / name
        p.write_bytes(b"\xff\xd8")  # minimal JPEG header
        imgs.append(ImageFile(path=p, timestamp=ts))
    return imgs


def _populate(ctrl, images):
    """Set up controller with a list of ImageFile objects."""
    ctrl._all_images = list(images)
    ctrl.image_files = list(images)
    ctrl._rebuild_path_to_index()
    ctrl.current_index = 0


# --- sort mode preserves current image ---


def test_sort_mode_preserves_current_image(app_controller, tmp_path):
    """Changing sort mode keeps the selected image when it remains present."""
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 1000), ("b.jpg", 3000), ("c.jpg", 2000)],
    )
    _populate(app_controller, imgs)
    # Select b.jpg (index 1 in default order)
    app_controller.current_index = 1

    app_controller.set_sort_mode("date")

    # b.jpg has the highest timestamp so it moves to index 0
    assert app_controller.image_files[app_controller.current_index].path.name == "b.jpg"


# --- empty image list does not crash ---


def test_sort_mode_empty_image_list(app_controller):
    """Switching sort mode with no images must not raise."""
    app_controller._all_images = []
    app_controller.image_files = []
    app_controller._rebuild_path_to_index()

    app_controller.set_sort_mode("filename")
    assert app_controller.image_files == []
    assert app_controller.current_index == 0


def test_sort_mode_empty_filtered_result(app_controller, tmp_path):
    """Sort mode change with active filter that matches nothing must not crash."""
    imgs = _make_images(tmp_path, [("photo.jpg", 1000)])
    _populate(app_controller, imgs)

    # Enable a filter that matches nothing
    app_controller._filter_enabled = True
    app_controller._filter_string = "zzz_no_match"

    app_controller.set_sort_mode("date")
    assert app_controller.image_files == []
    assert app_controller.current_index == 0


# --- date sort uses ImageFile.timestamp, not live stat() ---


def test_date_sort_uses_timestamp_field(app_controller, tmp_path):
    """Date sort must use ImageFile.timestamp, not filesystem stat()."""
    imgs = _make_images(
        tmp_path,
        [("old.jpg", 1000), ("mid.jpg", 2000), ("new.jpg", 3000)],
    )
    _populate(app_controller, imgs)

    app_controller.set_sort_mode("date")

    names = [img.path.name for img in app_controller.image_files]
    assert names == ["new.jpg", "mid.jpg", "old.jpg"]


def test_date_sort_oserror_does_not_crash(app_controller, tmp_path):
    """An image whose file was deleted between scan and sort must not crash.

    Since we use ImageFile.timestamp (captured at scan time), there's no
    filesystem call to fail.  Images with timestamp 0 sort last.
    """
    p_missing = tmp_path / "gone.jpg"
    p_missing.write_bytes(b"\xff\xd8")
    p_ok = tmp_path / "ok.jpg"
    p_ok.write_bytes(b"\xff\xd8")
    # Remove the file so it is truly missing on disk
    p_missing.unlink()
    imgs = [
        ImageFile(path=p_missing, timestamp=0.0),  # truly missing file
        ImageFile(path=p_ok, timestamp=5000),
    ]
    _populate(app_controller, imgs)

    app_controller.set_sort_mode("date")

    names = [img.path.name for img in app_controller.image_files]
    # ok.jpg (ts=5000) first, gone.jpg (ts=0) last
    assert names == ["ok.jpg", "gone.jpg"]


# --- date sort determinism with equal mtimes ---


def test_date_sort_deterministic_equal_mtimes(app_controller, tmp_path):
    """When timestamps are equal, sort must be deterministic by filename."""
    imgs = _make_images(
        tmp_path,
        [("charlie.jpg", 5000), ("alpha.jpg", 5000), ("bravo.jpg", 5000)],
    )
    _populate(app_controller, imgs)

    app_controller.set_sort_mode("date")

    names = [img.path.name for img in app_controller.image_files]
    # Same timestamp → alphabetical tiebreak
    assert names == ["alpha.jpg", "bravo.jpg", "charlie.jpg"]


# --- filename sort ---


def test_filename_sort(app_controller, tmp_path):
    imgs = _make_images(
        tmp_path,
        [("Zebra.jpg", 1000), ("apple.jpg", 2000), ("Mango.jpg", 3000)],
    )
    _populate(app_controller, imgs)

    app_controller.set_sort_mode("filename")

    names = [img.path.name for img in app_controller.image_files]
    assert names == ["apple.jpg", "Mango.jpg", "Zebra.jpg"]


# --- grid model refresh after sort ---


def test_grid_model_refreshed_when_grid_active(app_controller, tmp_path):
    """Grid model must be refreshed when sort mode changes in grid view."""
    imgs = _make_images(tmp_path, [("a.jpg", 1000), ("b.jpg", 2000)])
    _populate(app_controller, imgs)
    app_controller._is_grid_view_active = True

    app_controller.set_sort_mode("filename")

    app_controller._thumbnail_model.refresh_from_controller.assert_called_once()
    assert app_controller._grid_model_dirty is False


def test_grid_model_dirty_when_grid_inactive(app_controller, tmp_path):
    """Grid model dirty flag must be set when sort changes outside grid view."""
    imgs = _make_images(tmp_path, [("a.jpg", 1000), ("b.jpg", 2000)])
    _populate(app_controller, imgs)
    app_controller._is_grid_view_active = False

    app_controller.set_sort_mode("filename")

    app_controller._thumbnail_model.refresh_from_controller.assert_not_called()
    assert app_controller._grid_model_dirty is True


# --- no-op when mode unchanged ---


def test_set_sort_mode_noop_when_unchanged(app_controller, tmp_path):
    """Setting the same sort mode should be a no-op."""
    imgs = _make_images(tmp_path, [("a.jpg", 1000)])
    _populate(app_controller, imgs)

    prev = app_controller.sync_ui_state.call_count
    app_controller.set_sort_mode("default")  # already default

    # sync_ui_state should not be called again
    assert app_controller.sync_ui_state.call_count == prev


# --- invalid sort mode ignored ---


def test_set_sort_mode_invalid_ignored(app_controller):
    """Invalid sort mode string must be silently ignored."""
    app_controller.set_sort_mode("random")
    assert app_controller.sort_mode == "default"


# --- stack/batch preservation across sort ---


def test_sort_preserves_contiguous_stacks(app_controller, tmp_path):
    """Stacks that stay contiguous after sort must be preserved."""
    # a,b,c in default order; filename sort gives same order (a,b,c)
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 3000), ("b.jpg", 2000), ("c.jpg", 1000)],
    )
    _populate(app_controller, imgs)
    # Stack covers indices 0-1 (a.jpg, b.jpg) — stays contiguous under filename sort
    app_controller.stacks = [[0, 1]]
    app_controller.sidecar = MagicMock()
    app_controller.sidecar.data.stacks = [[0, 1]]

    app_controller.set_sort_mode("filename")

    assert app_controller.sort_mode == "filename"
    # a.jpg and b.jpg are still adjacent at positions 0-1
    assert app_controller.stacks == [[0, 1]]
    app_controller.sidecar.save.assert_called()


def test_sort_noncontiguous_stack_user_cancels(app_controller, tmp_path):
    """Non-contiguous stack + user Cancel ⇒ nothing changes."""
    # default order: a, b, c.  date sort: c(ts=3000), a(ts=2000), b(ts=1000)
    # Stack [0,1] = a,b. Under date sort a→idx1, b→idx2: still contiguous.
    # Stack [0,2] = a,b,c. Under date sort a→1,b→2,c→0: c=0,a=1,b=2 contiguous.
    # We need non-contiguous: stack [0,1]={a,b} where date order scatters them.
    # a(ts=1000), b(ts=3000), c(ts=2000). Date: b(3000), c(2000), a(1000).
    # Stack[0,1]={a,b} → date indices: a→2, b→0. Not contiguous!
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 1000), ("b.jpg", 3000), ("c.jpg", 2000)],
    )
    _populate(app_controller, imgs)
    app_controller.stacks = [[0, 1]]
    app_controller.sidecar = MagicMock()
    app_controller.sidecar.data.stacks = [[0, 1]]
    original_files = list(app_controller.image_files)

    with patch.object(
        app_controller, "_confirm_clear_stacks_for_sort", return_value=False
    ):
        app_controller.set_sort_mode("date")

    # Everything unchanged
    assert app_controller.sort_mode == "default"
    assert app_controller.stacks == [[0, 1]]
    assert app_controller.image_files == original_files
    app_controller.sidecar.save.assert_not_called()


def test_sort_noncontiguous_stack_user_clears(app_controller, tmp_path):
    """Non-contiguous stack + user confirms ⇒ stacks cleared, sort applied, sidecar saved."""
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 1000), ("b.jpg", 3000), ("c.jpg", 2000)],
    )
    _populate(app_controller, imgs)
    app_controller.stacks = [[0, 1]]
    app_controller.stack_start_index = 0
    app_controller.sidecar = MagicMock()
    app_controller.sidecar.data.stacks = [[0, 1]]
    app_controller.sidecar.get_metadata.return_value = None

    with patch.object(
        app_controller, "_confirm_clear_stacks_for_sort", return_value=True
    ):
        app_controller.set_sort_mode("date")

    assert app_controller.sort_mode == "date"
    assert app_controller.stacks == []
    assert app_controller.stack_start_index is None
    # Sidecar must persist the cleared stacks
    assert app_controller.sidecar.data.stacks == []
    app_controller.sidecar.save.assert_called()
    # Date order: b(3000), c(2000), a(1000)
    names = [img.path.name for img in app_controller.image_files]
    assert names == ["b.jpg", "c.jpg", "a.jpg"]


def test_batch_split_does_not_block_sort(app_controller, tmp_path):
    """Batches that become non-contiguous after sort must not block sorting."""
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 1000), ("b.jpg", 3000), ("c.jpg", 2000)],
    )
    _populate(app_controller, imgs)
    # Batch covers a,b (indices 0-1). No stacks defined.
    app_controller.batches = [[0, 1]]
    app_controller.stacks = []
    app_controller.sidecar = MagicMock()
    app_controller.sidecar.data.stacks = []
    app_controller.sidecar.get_metadata.return_value = None

    app_controller.set_sort_mode("date")

    assert app_controller.sort_mode == "date"
    # Date order: b(3000), c(2000), a(1000) → a is at idx 2, b at idx 0
    # Batch should now contain both, possibly split into [0,0] and [2,2]
    batch_indices = set()
    for start, end in app_controller.batches:
        for i in range(start, end + 1):
            batch_indices.add(i)
    # a.jpg → index 2, b.jpg → index 0 under date sort
    a_idx = next(
        i
        for i, img in enumerate(app_controller.image_files)
        if img.path.name == "a.jpg"
    )
    b_idx = next(
        i
        for i, img in enumerate(app_controller.image_files)
        if img.path.name == "b.jpg"
    )
    assert batch_indices == {a_idx, b_idx}


def test_pending_stack_start_remapped_without_completed_stacks(
    app_controller, tmp_path
):
    """A pending stack_start_index must follow its image through a sort,
    even when no completed stacks exist."""
    # Default order: a(idx0), b(idx1), c(idx2)
    # Date order: b(3000)→idx0, c(2000)→idx1, a(1000)→idx2
    imgs = _make_images(
        tmp_path,
        [("a.jpg", 1000), ("b.jpg", 3000), ("c.jpg", 2000)],
    )
    _populate(app_controller, imgs)
    app_controller.stacks = []  # no completed stacks
    app_controller.stack_start_index = 0  # pending start on a.jpg
    app_controller.sidecar = MagicMock()
    app_controller.sidecar.data.stacks = []
    app_controller.sidecar.get_metadata.return_value = None

    app_controller.set_sort_mode("date")

    assert app_controller.sort_mode == "date"
    # a.jpg moved to index 2 under date sort
    assert app_controller.stack_start_index == 2
    assert (
        app_controller.image_files[app_controller.stack_start_index].path.name
        == "a.jpg"
    )
