"""Regression tests for FS-P1-002: shutdown must not persist a stale save marker.

Failure sequence being locked out
--------------------------------
1. Async save R1 starts and persists ``edit_state = {pending_save, R1}``.
2. The user edits again, producing revision R2 for the same target.
3. Quitting captures R2, but ``_run_save_request_sync`` cannot run it because
   R1 still owns the target, so R2 is parked in ``_pending_save_recovery``.
4. Shutdown sets ``_shutting_down`` before draining R1, so R1's completion
   bookkeeping never runs.
5. Shutdown then saves R2's pixels -- but ``_mark_image_edited_in_sidecar``
   sees the stored R1 marker, whose ``request_id`` differs from R2's, and
   "preserves it as newer".

Result: R2 pixels on disk next to R1 recovery metadata. Reopening the image
would replay the older edit over the newer pixels.

The fix publishes the deferred request through the same pending-save
bookkeeping an accepted async save uses, so the persisted marker always names
the newest published request.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from faststack.io.sidecar import SidecarManager

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def save_env(app_controller, tmp_path):
    """AppController with a real sidecar and a controllable save worker."""
    controller = app_controller
    image_dir = tmp_path / "images"
    image_dir.mkdir(exist_ok=True)

    target = image_dir / "shot.jpg"
    target.write_bytes(b"original")
    # A durable source distinct from the save target, so edit state is
    # considered replayable and therefore actually gets persisted.
    source = image_dir / "shot-backup.jpg"
    source.write_bytes(b"source")

    controller.image_dir = image_dir
    controller.sidecar = SidecarManager(image_dir, watcher=None)

    state = {
        "saved": [],  # request ids in the order their pixels hit disk
        "gate": None,  # (request_id, started_event, release_event)
    }

    def fake_save_from_snapshot(snapshot):
        request_id = snapshot["request_id"]
        gate = state["gate"]
        if gate and gate[0] == request_id:
            gate[1].set()
            assert gate[2].wait(timeout=10), "save gate never released"
        target.write_bytes(f"pixels:{request_id}".encode())
        state["saved"].append(request_id)
        return (target, source)

    controller.image_editor.save_from_snapshot = fake_save_from_snapshot
    controller.image_editor.current_filepath = str(target)

    state["controller"] = controller
    state["target"] = target
    state["source"] = source
    yield state

    gate = state["gate"]
    if gate:
        gate[2].set()


def _make_request(env, *, revision: int):
    """A save request shaped exactly like _prepare_current_session_save_request."""
    controller = env["controller"]
    target = env["target"]
    source = env["source"]
    request_id = f"req{revision}-{uuid.uuid4().hex[:8]}"
    image_key = controller._key(target)
    session_token = (image_key, None, "session-1", revision)
    return {
        "snapshot": {
            "request_id": request_id,
            "filepath_snapshot": str(target),
            "source_filepath": str(source),
            "edits": {},
            "mask_assets": {},
        },
        "context": {
            "target": str(target.resolve()),
            "editor_was_open": False,
            "save_action_type": "save_edit",
            "save_image_key": image_key,
            "save_revision": revision,
            "save_request_id": request_id,
            "session_token": session_token,
            "edit_source_path": str(source),
            "auto_adjust_state": None,
            "save_directory_key": controller._key(env["target"].parent),
            "save_metadata_path": str(target),
            "started_from_restore_override": False,
            "save_sidecar": controller.sidecar,
            "success_message": None,
        },
    }


def _stored_edit_state(env):
    meta = env["controller"].sidecar.get_metadata(env["target"], create=False)
    return getattr(meta, "edit_state", None) if meta is not None else None


# --------------------------------------------------------------------------
# The full deterministic sequence
# --------------------------------------------------------------------------


def test_deferred_shutdown_save_is_published_as_the_newest_pending_request(save_env):
    env = save_env
    controller = env["controller"]

    r1 = _make_request(env, revision=1)
    r2 = _make_request(env, revision=2)
    r1_id = r1["context"]["save_request_id"]
    r2_id = r2["context"]["save_request_id"]

    r1_started = threading.Event()
    r1_release = threading.Event()
    env["gate"] = (r1_id, r1_started, r1_release)

    try:
        # 1-2. R1 starts and blocks inside the save worker.
        assert controller._submit_save_request_async(r1) is True
        assert r1_started.wait(timeout=10), "R1 worker never started"

        # 3. R1's pending marker is persisted.
        pending = _stored_edit_state(env)
        assert pending is not None
        assert pending["status"] == "pending_save"
        assert pending["request_id"] == r1_id

        # 4. The live session advances to R2 and the user quits.
        controller._prepare_current_session_save_request = (
            lambda **kwargs: r2  # noqa: ARG005
        )
        assert controller.prepare_for_app_close() is True

        # 5-6. R2 is the shutdown recovery request *and* the newest published
        # pending request -- both in memory and on disk.
        assert controller._pending_save_recovery[r2["context"]["target"]] is r2
        assert (
            controller._pending_edit_save_requests[r2["context"]["save_image_key"]]
            is r2
        )
        published = _stored_edit_state(env)
        assert published["status"] == "pending_save"
        assert published["request_id"] == r2_id, (
            "the deferred request must be published as the newest pending "
            "request; leaving R1's marker is what lets shutdown preserve it"
        )
        assert published["revision"] == 2

        # 7-8. Shutdown begins, then R1 is allowed to finish.
        controller._shutting_down = True
        r1_release.set()
    finally:
        r1_release.set()

    # 9-10. Drain the save executor and run shutdown recovery for R2.
    controller.shutdown_nonqt()

    # R1's pixels were written first, R2's last -- R2 wins on disk.
    assert env["saved"] == [r1_id, r2_id]
    assert env["target"].read_bytes() == f"pixels:{r2_id}".encode()

    # 11. Sidecar reflects R2, saved -- not a stale R1 pending_save.
    final = _stored_edit_state(env)
    assert final is not None
    assert final["status"] == "saved", "shutdown preserved a stale pending marker"
    assert final["request_id"] == r2_id
    assert final["revision"] == 2

    meta = controller.sidecar.get_metadata(env["target"], create=False)
    assert meta.edited is True

    # 12. The recovery loader must not offer R1 over the newer saved pixels.
    controller._pending_edit_save_requests.clear()
    controller.image_files = []
    controller.current_index = 0
    restored = controller._get_pending_edit_state_for_loaded_path(env["target"])
    assert restored is not None
    assert restored["request_id"] == r2_id
    assert restored["revision"] == 2


# --------------------------------------------------------------------------
# The ordering invariant itself
# --------------------------------------------------------------------------


def test_older_pending_marker_is_not_mistaken_for_a_newer_one(save_env):
    """A same-lineage marker at an older revision must never be preserved."""
    env = save_env
    controller = env["controller"]

    r1 = _make_request(env, revision=1)
    r2 = _make_request(env, revision=2)

    controller._write_pending_edit_state_for_request(r1)
    assert _stored_edit_state(env)["request_id"] == r1["context"]["save_request_id"]

    saved_state = controller._build_pending_edit_state(r2)
    saved_state["status"] = "saved"
    controller._mark_image_edited_in_sidecar(
        controller.sidecar,
        env["target"],
        completed_edit_state_request_id=r2["context"]["save_request_id"],
        saved_edit_state=saved_state,
    )

    final = _stored_edit_state(env)
    assert final["status"] == "saved"
    assert final["request_id"] == r2["context"]["save_request_id"]


def test_genuinely_newer_pending_marker_is_still_preserved(save_env):
    """The original protection must survive: a newer revision is not clobbered."""
    env = save_env
    controller = env["controller"]

    r1 = _make_request(env, revision=1)
    r2 = _make_request(env, revision=2)

    # R2 (newer) is the stored pending marker; R1 (older) completes late.
    controller._write_pending_edit_state_for_request(r2)

    saved_state = controller._build_pending_edit_state(r1)
    saved_state["status"] = "saved"
    controller._mark_image_edited_in_sidecar(
        controller.sidecar,
        env["target"],
        completed_edit_state_request_id=r1["context"]["save_request_id"],
        saved_edit_state=saved_state,
    )

    final = _stored_edit_state(env)
    assert final["status"] == "pending_save"
    assert final["request_id"] == r2["context"]["save_request_id"]


def test_unorderable_lineages_keep_the_conservative_behaviour(save_env):
    """Different sessions cannot be ordered, so the pending marker is kept."""
    env = save_env
    controller = env["controller"]

    pending = controller._build_pending_edit_state(_make_request(env, revision=5))
    pending["session_token"] = ["other-image", None, "session-2", 5]
    saved = controller._build_pending_edit_state(_make_request(env, revision=9))
    saved["status"] = "saved"

    assert controller._pending_edit_state_is_superseded(pending, saved) is False


@pytest.mark.parametrize(
    "pending_rev,saved_rev,expected",
    [(1, 2, True), (2, 2, True), (3, 2, False), (None, 2, False), (1, None, False)],
)
def test_supersession_revision_comparison(save_env, pending_rev, saved_rev, expected):
    controller = save_env["controller"]
    lineage = ["key", None, "session-1"]
    pending = {"session_token": lineage + [pending_rev], "revision": pending_rev}
    saved = {"session_token": lineage + [saved_rev], "revision": saved_rev}
    assert (
        controller._pending_edit_state_is_superseded(pending, saved) is expected
    )
