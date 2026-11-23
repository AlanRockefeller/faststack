"""W: re-render the JPG from its RAW with RawTherapee auto white balance."""

import json
import os
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication

from faststack.app import AppController
from faststack.controller import raw_white_balance as rawb
from faststack.models import ImageFile
from faststack.ui.keystrokes import Keybinder

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake rawtherapee-cli is a POSIX script"
)


@pytest.fixture(scope="module")
def qt_app():
    return QCoreApplication.instance() or QApplication([])


def _write_jpg(path: Path, size, color, orientation=None):
    img = Image.new("RGB", size, color)
    if orientation is None:
        img.save(path, "JPEG", quality=90)
    else:
        exif = Image.Exif()
        exif[274] = orientation
        img.save(path, "JPEG", quality=90, exif=exif.tobytes())


def _fake_cli(
    tmp_path: Path,
    *,
    size=(60, 40),
    exit_code=0,
    write=True,
    source=None,
    clip_without_compression=False,
) -> Path:
    """A stand-in rawtherapee-cli: records argv + profile (the last run in
    cli-log.json, every profile in cli-runs.log), writes a JPEG (flat grey,
    a copy of ``source``, or blown white until highlight compression is
    asked for)."""
    log_path = tmp_path / "cli-log.json"
    script = tmp_path / "bin" / "rawtherapee-cli"
    script.parent.mkdir()
    script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path
            from PIL import Image
            argv = sys.argv[1:]
            out = Path(argv[argv.index("-o") + 1])
            # RawTherapee always rewrites the extension to the output type.
            out = out.with_suffix(".jpg")
            profile = Path(argv[argv.index("-p") + 1]).read_text()
            Path({str(log_path)!r}).write_text(
                json.dumps({{"argv": argv, "profile": profile}})
            )
            with open({str(tmp_path / "cli-runs.log")!r}, "a") as runs:
                runs.write(profile.replace("\\n", "|") + "\\n")
            if {clip_without_compression!r} and "HighlightCompr" not in profile:
                Image.new("RGB", {size!r}, (255, 255, 255)).save(out, "JPEG")
            elif {write!r} and {str(source) if source else None!r}:
                Image.open({str(source) if source else None!r}).save(out, "JPEG")
            elif {write!r}:
                Image.new("RGB", {size!r}, (128, 128, 128)).save(out, "JPEG")
            print("Processing " + argv[-1])
            sys.exit({exit_code!r})
            """))
    script.chmod(0o755)
    return script


def _controller(tmp_path: Path, cli: Path, *, image_size=(60, 40)):
    jpg = tmp_path / "P1010001.JPG"
    raw = tmp_path / "P1010001.ORF"
    _write_jpg(jpg, image_size, (200, 120, 40))
    raw.write_bytes(b"fake raw")

    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller._init_raw_white_balance()
    controller.image_files = [ImageFile(path=jpg, raw_pair=raw)]
    controller.current_index = 0
    controller.image_dir = tmp_path
    controller.sidecar = MagicMock()
    controller._shutting_down = False
    controller._saving_keys = {}
    controller._pending_edit_save_requests = {}
    controller._auto_adjust_save_pending_action = None
    controller.undo_history = []
    controller.ui_state = MagicMock(isCropping=False)
    controller.image_editor = MagicMock(current_filepath=None)
    controller.status_messages = []
    controller.update_status_message = (
        lambda msg, **_kw: controller.status_messages.append(msg)
    )
    controller._is_current_live_edit_session_dirty = MagicMock(return_value=False)
    controller._mark_image_edited_in_sidecar = MagicMock(return_value={"old": 1})
    controller._retire_pending_saves_for_photoshop = MagicMock(return_value=False)
    controller._suppress_watcher_paths = MagicMock()
    controller._refresh_after_raw_awb = MagicMock()
    controller._auto_add_edited_to_batch_if_enabled = MagicMock()
    controller._configured_rawtherapee_executable = lambda: str(cli)
    # Editor plumbing W touches when the editor holds the photo.
    controller._clear_active_auto_adjust_state = MagicMock()
    controller._clear_live_edit_session_state = MagicMock()
    controller._preview_lock = threading.Lock()
    controller._clear_last_rendered_preview_locked = MagicMock()
    controller.load_image_for_editing = MagicMock(return_value=True)
    controller.sync_ui_state = MagicMock()
    controller._persist_sidecar = MagicMock(return_value=True)
    controller._pending_save_recovery = {}
    return controller, jpg, raw


def _wait(controller, timeout_ms=10000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)

    def check():
        with controller._raw_awb_lock:
            if not controller._raw_awb_operations:
                loop.quit()

    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    # The queued completion slot runs after the worker emits.
    QCoreApplication.processEvents()
    with controller._raw_awb_lock:
        assert not controller._raw_awb_operations


def _cli_log(tmp_path):
    return json.loads((tmp_path / "cli-log.json").read_text())


def test_w_key_runs_raw_white_balance():
    controller = MagicMock(spec=["raw_white_balance_to_jpg"])
    controller.main_window = None
    binder = Keybinder(controller)
    assert binder._binding_for(Qt.Key_W, "w", Qt.NoModifier) == (
        "raw_white_balance_to_jpg"
    )
    assert binder._binding_for(Qt.Key_W, "W", Qt.ShiftModifier) is None


def test_profile_and_command_shape():
    profile = rawb.build_raw_awb_profile("Auto")
    assert profile == "[White Balance]\nEnabled=true\nSetting=Auto\n"
    cmd = rawb.build_raw_awb_command(
        "rt", Path("/r/a.ORF"), Path("/t/o.jpg"), Path("/t/p.pp3"), quality=95
    )
    # Default profile first, then the WB layer on top; -c last.
    assert cmd.index("-d") < cmd.index("-p")
    assert cmd[-2:] == ["-c", str(Path("/r/a.ORF"))]
    assert "-j95" in cmd and "-js3" in cmd and "-Y" in cmd


@pytest.mark.parametrize(
    "value, expected",
    [
        ("autold", "autold"),
        ("", "Auto"),
        ("Auto\nEnabled=false", "Auto"),
        ("a=b", "Auto"),
    ],
)
def test_awb_method_config_is_sanitized(value, expected):
    with patch.object(rawb.config, "get", return_value=value):
        assert rawb._raw_awb_method() == expected


def test_replaces_jpg_with_backup_and_undo(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, raw = _controller(tmp_path, cli)
    old_bytes = jpg.read_bytes()
    old_mtime = jpg.stat().st_mtime

    assert controller.raw_white_balance_to_jpg() is True
    _wait(controller)

    log = _cli_log(tmp_path)
    assert log["argv"][-1] == str(raw)
    assert "Setting=Auto" in log["profile"]
    backup = tmp_path / "P1010001-backup.JPG"
    assert backup.read_bytes() == old_bytes
    assert jpg.read_bytes() != old_bytes
    with Image.open(jpg) as img:
        assert img.getpixel((5, 5)) == pytest.approx((128, 128, 128), abs=3)
    assert jpg.stat().st_mtime == pytest.approx(old_mtime, abs=1e-3)
    # No staging or RawTherapee leftovers next to the photo.
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == sorted(
        [
            "P1010001.JPG",
            "P1010001-backup.JPG",
            "P1010001.ORF",
            "cli-log.json",
            "cli-runs.log",
        ]
    )

    action, data, _ts = controller.undo_history[-1]
    assert action == rawb.RAW_WHITE_BALANCE_ACTION
    assert data["saved_path"] == str(jpg)
    assert data["backup_path"] == str(backup)
    assert data["metadata_before"] == {"old": 1}
    controller._mark_image_edited_in_sidecar.assert_called_once()
    assert (
        controller._mark_image_edited_in_sidecar.call_args.kwargs[
            "clear_saved_edit_state"
        ]
        is True
    )
    controller._refresh_after_raw_awb.assert_called_once_with(
        jpg, on_current=True, keep_editor=False
    )
    assert "Ctrl+Z" in controller.status_messages[-1]


def test_undo_restores_camera_jpg(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    old_bytes = jpg.read_bytes()
    controller.raw_white_balance_to_jpg()
    _wait(controller)

    controller._flush_pending_auto_adjust_save = MagicMock()
    controller._block_if_saving = MagicMock(return_value=False)
    controller._clear_active_auto_adjust_state = MagicMock()
    controller._restore_metadata_snapshot = MagicMock(return_value=True)
    controller._post_undo_refresh_and_select = MagicMock()
    controller.undo_delete()

    assert jpg.read_bytes() == old_bytes
    assert not (tmp_path / "P1010001-backup.JPG").exists()
    assert controller.status_messages[-1] == "Undid RAW white balance"


def _wait_until(predicate, timeout_ms=10000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: predicate() and loop.quit())
    timer.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    timer.stop()
    QCoreApplication.processEvents()
    return predicate()


def test_unsaved_edits_are_saved_first(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._is_current_live_edit_session_dirty.return_value = True
    controller.image_editor.current_filepath = jpg
    key = controller._key(jpg)

    def flush():
        # The save goes off in the background and the session is clean.
        controller._saving_keys[key] = 1
        controller._is_current_live_edit_session_dirty.return_value = False
        return True

    controller._flush_current_live_edit_session_for_navigation = MagicMock(
        side_effect=flush
    )
    controller._pending_save_recovery = {}

    assert controller.raw_white_balance_to_jpg() is True
    controller._flush_current_live_edit_session_for_navigation.assert_called_once()
    assert "Saving your edits" in controller.status_messages[-1]
    assert key in controller._raw_awb_waiting
    assert not (tmp_path / "cli-log.json").exists()

    controller._saving_keys.pop(key)  # the save landed
    assert _wait_until(lambda: bool(controller.undo_history))
    assert controller.undo_history[-1][0] == rawb.RAW_WHITE_BALANCE_ACTION
    assert (tmp_path / "cli-log.json").exists()


def test_failed_save_cancels_w(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._is_current_live_edit_session_dirty.return_value = True
    controller.image_editor.current_filepath = jpg
    controller._flush_current_live_edit_session_for_navigation = MagicMock(
        return_value=True
    )
    controller._pending_save_recovery = {str(jpg): {"failed": True}}

    assert controller.raw_white_balance_to_jpg() is True
    assert _wait_until(lambda: not controller._raw_awb_waiting)
    assert "failed" in controller.status_messages[-1]
    assert not (tmp_path / "cli-log.json").exists()


def test_waits_while_saving(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    key = controller._key(jpg)
    controller._saving_keys[key] = 1
    controller._pending_save_recovery = {}

    assert controller.raw_white_balance_to_jpg() is True
    assert not (tmp_path / "cli-log.json").exists()
    controller._saving_keys.pop(key)
    assert _wait_until(lambda: bool(controller.undo_history))


def test_needs_a_raw_pair(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller.image_files = [ImageFile(path=jpg)]

    assert controller.raw_white_balance_to_jpg() is False
    assert "matching RAW" in controller.status_messages[-1]


def test_cli_failure_leaves_jpg_alone(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, exit_code=3, write=False)
    controller, jpg, _raw = _controller(tmp_path, cli)
    old_bytes = jpg.read_bytes()

    controller.raw_white_balance_to_jpg()
    _wait(controller)

    assert jpg.read_bytes() == old_bytes
    assert not (tmp_path / "P1010001-backup.JPG").exists()
    assert controller.undo_history == []
    assert "exit code 3" in controller.status_messages[-1]


def test_jpg_changed_during_develop_is_not_overwritten(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller.raw_white_balance_to_jpg()
    # Someone else (Photoshop, a save) rewrites the JPG meanwhile.
    _write_jpg(jpg, (60, 40), (0, 0, 255))
    os.utime(jpg, (1, 1))
    changed = jpg.read_bytes()
    _wait(controller)

    assert jpg.read_bytes() == changed
    assert controller.undo_history == []
    assert "changed while RawTherapee was running" in controller.status_messages[-1]
    assert not any(p.name.endswith(".raw-awb.tmp") for p in tmp_path.iterdir())


def _scene(size=(120, 80)) -> Image.Image:
    """A photo-like picture that only looks right one way up."""
    w, h = size
    img = Image.new("RGB", size, (30, 60, 30))
    img.paste((240, 200, 40), (w // 8, h // 6, w // 2, h // 2))  # flower, top left
    img.paste((90, 140, 90), (w // 2, h * 2 // 3, w, h))  # leaf, bottom right
    return img


@pytest.mark.parametrize(
    "angle, turned",
    [
        (90, Image.Transpose.ROTATE_270),
        (270, Image.Transpose.ROTATE_90),
        (180, Image.Transpose.ROTATE_180),
        (0, None),
    ],
)
def test_match_output_rotation_finds_the_turn(tmp_path, angle, turned):
    shown = tmp_path / "shown.jpg"
    _scene().save(shown, "JPEG", quality=95)
    out = tmp_path / "out.jpg"
    (_scene().transpose(turned) if turned else _scene()).save(out, "JPEG")
    assert rawb.match_output_rotation(shown, out) == angle


def test_match_uses_how_the_jpg_is_displayed(tmp_path):
    # Camera JPG: landscape pixels tagged "rotate 90 CW", shown portrait.
    # RawTherapee bakes the same turn in, so nothing more is needed.
    camera = tmp_path / "camera.jpg"
    exif = Image.Exif()
    exif[274] = 6
    _scene().save(camera, "JPEG", quality=95, exif=exif.tobytes())
    out = tmp_path / "out.jpg"
    _scene().transpose(Image.Transpose.ROTATE_270).save(out, "JPEG")
    assert rawb.match_output_rotation(camera, out) == 0


def test_featureless_photo_with_wrong_shape_is_refused(tmp_path):
    shown = tmp_path / "shown.jpg"
    Image.new("RGB", (120, 80), (90, 90, 90)).save(shown, "JPEG")
    out = tmp_path / "out.jpg"
    Image.new("RGB", (80, 120), (90, 90, 90)).save(out, "JPEG")
    with pytest.raises(RuntimeError, match="orientation"):
        rawb.match_output_rotation(shown, out)


def _textured_scene(size=(480, 320)) -> Image.Image:
    """A photo-like picture with detail everywhere, so a crop of it can be
    found again (and only one way up)."""
    import numpy as np

    w, h = size
    rng = np.random.default_rng(7)
    blobs = rng.integers(0, 256, (h // 10, w // 10, 3), dtype=np.uint8)
    img = Image.fromarray(blobs, "RGB").resize(size, Image.BICUBIC)
    img.paste((240, 200, 40), (w // 8, h // 6, w // 3, h // 3))  # flower
    return img


def test_crop_that_flips_the_shape_is_matched_inside_the_frame(tmp_path):
    # The user turned the photo (RawTherapee's output is still portrait),
    # then cropped a portrait strip out of the landscape picture.
    scene = _textured_scene()
    shown = tmp_path / "shown.jpg"
    scene.crop((150, 0, 330, 320)).save(shown, "JPEG", quality=95)
    out = tmp_path / "out.jpg"
    # RawTherapee's frame is a little bigger than the camera's.
    big = scene.resize((490, 327), Image.BICUBIC)
    big.transpose(Image.Transpose.ROTATE_270).save(out, "JPEG", quality=95)
    assert rawb.match_output_rotation(shown, out) == 90


@pytest.mark.parametrize(
    "angle, turned",
    [
        (180, Image.Transpose.ROTATE_180),
        (90, Image.Transpose.ROTATE_270),
        (0, None),
    ],
)
def test_off_centre_crop_of_a_turned_photo_finds_the_turn(tmp_path, angle, turned):
    scene = _textured_scene()
    shown = tmp_path / "shown.jpg"
    scene.crop((40, 30, 340, 230)).save(shown, "JPEG", quality=95)
    out = tmp_path / "out.jpg"
    (scene.transpose(turned) if turned else scene).save(out, "JPEG", quality=95)
    assert rawb.match_output_rotation(shown, out) == angle


def test_featureless_crop_with_wrong_shape_is_refused(tmp_path):
    shown = tmp_path / "shown.jpg"
    Image.new("RGB", (60, 80), (90, 90, 90)).save(shown, "JPEG")
    out = tmp_path / "out.jpg"
    flat = Image.new("RGB", (120, 80), (90, 90, 90))
    flat.transpose(Image.Transpose.ROTATE_90).save(out, "JPEG")
    # Portrait crop fits a portrait frame only, but nothing says which way up:
    # RawTherapee's own turn fits, so it is kept.
    assert rawb.match_output_rotation(shown, out) == 0


def test_rotate_jpeg_keeps_metadata_and_clears_tag(tmp_path):
    path = tmp_path / "out.jpg"
    exif = Image.Exif()
    exif[271] = "OM Digital Solutions"
    _scene().save(path, "JPEG", exif=exif.tobytes())
    rawb.rotate_jpeg(path, 90, quality=95)
    with Image.open(path) as img:
        assert img.size == (80, 120)
        assert img.getexif()[271] == "OM Digital Solutions"
        assert img.getexif()[274] == 1


def test_photo_turned_in_faststack_stays_turned(qt_app, tmp_path):
    # The camera tagged the shot portrait, the user turned it back to
    # landscape in FastStack (pixels landscape, no tag), and RawTherapee
    # follows the RAW's tag: portrait output. It must come out landscape.
    portrait = tmp_path / "rt-output.jpg"
    _scene().transpose(Image.Transpose.ROTATE_270).save(portrait, "JPEG")
    cli = _fake_cli(tmp_path, source=portrait)
    controller, jpg, _raw = _controller(tmp_path, cli)
    _scene().save(jpg, "JPEG", quality=95)

    controller.raw_white_balance_to_jpg()
    _wait(controller)

    assert controller.undo_history[-1][0] == rawb.RAW_WHITE_BALANCE_ACTION
    with Image.open(jpg) as img:
        assert img.size == (120, 80)
        # Top left is the flower again.
        r, g, _b = img.getpixel((30, 25))
        assert r > 180 and g > 150


def test_second_press_while_running_is_ignored(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    gate = threading.Event()
    real_worker = controller._raw_awb_worker

    def slow_worker(*args):
        gate.wait(5)
        real_worker(*args)

    controller._raw_awb_worker = slow_worker
    assert controller.raw_white_balance_to_jpg() is True
    assert controller.raw_white_balance_to_jpg() is False
    assert "already running" in controller.status_messages[-1]
    gate.set()
    _wait(controller)
    assert len(controller.undo_history) == 1


@pytest.mark.parametrize(
    "attr, value, expected",
    [
        ("_is_grid_view_active", True, "image view"),
        ("view_override_path", "/x/P1010001-backup.JPG", "main JPG"),
    ],
)
def test_refuses_when_photo_is_not_the_one_shown(
    qt_app, tmp_path, attr, value, expected
):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    setattr(controller, attr, value)

    assert controller.raw_white_balance_to_jpg() is False
    assert expected in controller.status_messages[-1]
    assert not (tmp_path / "cli-log.json").exists()


def _stub_refresh(controller, jpg):
    """Stub what the real _refresh_after_raw_awb calls around the editor."""
    controller._refresh_after_raw_awb = AppController._refresh_after_raw_awb.__get__(
        controller
    )
    controller._preview_lock = threading.Lock()
    controller._clear_last_rendered_preview_locked = MagicMock()
    controller._clear_live_edit_session_state = MagicMock()
    controller._clear_active_auto_adjust_state = MagicMock()
    controller._current_image_path = MagicMock(return_value=jpg)
    controller._begin_direct_image_transition = MagicMock()
    controller.refresh_image_list = MagicMock()
    controller._reindex_after_save = MagicMock()
    controller._invalidate_decoded_path = MagicMock()
    controller.prefetcher = MagicMock()
    controller.sync_ui_state = MagicMock()
    controller._do_prefetch = MagicMock()
    controller._restart_quality_decode_timer = MagicMock()
    controller.load_image_for_editing = MagicMock(return_value=True)
    controller.ui_state.isHistogramVisible = False
    controller.image_editor.current_filepath = jpg


def test_open_editor_stays_open_on_the_new_pixels(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    _stub_refresh(controller, jpg)
    controller.ui_state.isEditorOpen = True

    controller._refresh_after_raw_awb(jpg, on_current=True)

    assert controller.ui_state.isEditorOpen is True
    controller.image_editor.clear.assert_called_once()
    controller.load_image_for_editing.assert_called_once()


def test_editor_closes_when_the_reload_fails(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    _stub_refresh(controller, jpg)
    controller.ui_state.isEditorOpen = True
    controller.load_image_for_editing.return_value = False

    controller._refresh_after_raw_awb(jpg, on_current=True)

    assert controller.ui_state.isEditorOpen is False


def test_editor_on_a_photo_no_longer_shown_is_closed(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    _stub_refresh(controller, jpg)
    controller.ui_state.isEditorOpen = True

    controller._refresh_after_raw_awb(jpg, on_current=False)

    assert controller.ui_state.isEditorOpen is False
    controller.image_editor.clear.assert_called_once()
    controller.load_image_for_editing.assert_not_called()


# -- Highlights, framing, meter ----------------------------------------------


def test_profile_adds_highlight_compression():
    profile = rawb.build_raw_awb_profile("Auto", 250)
    assert "[Exposure]\nHighlightCompr=250\nHighlightComprThreshold=0\n" in profile
    assert rawb.build_raw_awb_profile("Auto", 0) == rawb.build_raw_awb_profile("Auto")


@pytest.mark.parametrize(
    "clipped, expected", [(0.0, 0), (0.004, 0), (0.01, 250), (0.2, 400)]
)
def test_highlight_compression_follows_the_render(clipped, expected):
    assert rawb.highlight_compression_for(clipped) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("auto", None),
        ("", None),
        ("off", 0),
        ("300", 300),
        ("inf", None),
        ("1e309", None),
        ("nan", None),
    ],
)
def test_highlight_mode_config(value, expected):
    with patch.object(rawb.config, "get", return_value=value):
        assert rawb._raw_awb_highlight_mode() == expected


def test_blown_render_gets_a_second_pass(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, clip_without_compression=True)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller.raw_white_balance_to_jpg()
    _wait(controller)

    runs = (tmp_path / "cli-runs.log").read_text().splitlines()
    assert len(runs) == 2
    assert "HighlightCompr" not in runs[0]
    assert "HighlightCompr=400" in runs[1]
    with Image.open(jpg) as img:
        assert img.getpixel((5, 5)) == pytest.approx((128, 128, 128), abs=3)
    assert "highlights pulled back" in controller.status_messages[-1]


def test_clean_render_is_developed_once(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    controller.raw_white_balance_to_jpg()
    _wait(controller)
    assert len((tmp_path / "cli-runs.log").read_text().splitlines()) == 1


def test_frame_crop_box_finds_an_off_centre_frame():
    rng = np.random.default_rng(3)
    big = cv2_blur(rng.integers(0, 256, (330, 490)).astype(np.float32))
    # The camera's frame sits 9 px right and 4 px up of centre.
    x0, y0 = (490 - 470) // 2 + 9, (330 - 314) // 2 - 4
    reference = big[y0 : y0 + 314, x0 : x0 + 470]
    assert rawb.frame_crop_box(reference, big) == (x0, y0, x0 + 470, y0 + 314)


def cv2_blur(arr):
    from faststack.imaging.optional_deps import get_cv2

    return get_cv2().GaussianBlur(arr, (0, 0), 2.0)


def test_render_is_trimmed_to_the_camera_frame(qt_app, tmp_path):
    scene = _textured_scene((490, 330))
    rt_output = tmp_path / "rt-output.jpg"
    scene.save(rt_output, "JPEG", quality=95)
    cli = _fake_cli(tmp_path, source=rt_output)
    controller, jpg, _raw = _controller(tmp_path, cli)
    scene.crop((10, 8, 480, 322)).save(jpg, "JPEG", quality=95)

    controller.raw_white_balance_to_jpg()
    _wait(controller)
    with Image.open(jpg) as img:
        assert img.size == (470, 314)


def test_narrow_crop_of_an_upright_frame_keeps_rawtherapee_turn(tmp_path):
    # Smooth picture: turned sideways it still correlates well with a
    # narrow strip, which used to win because of the strip's own shape.
    w, h = 480, 320
    ramp = np.linspace(0, 255, w, dtype=np.float32)[None, :].repeat(h, 0)
    ramp += np.linspace(0, 60, h, dtype=np.float32)[:, None]
    frame = Image.fromarray(np.clip(ramp, 0, 255).astype(np.uint8), "L")
    frame = frame.convert("RGB")
    shown = tmp_path / "shown.jpg"
    frame.crop((200, 20, 250, 300)).save(shown, "JPEG", quality=95)
    out = tmp_path / "out.jpg"
    frame.save(out, "JPEG", quality=95)
    assert rawb.match_output_rotation(shown, out) == 0


def test_meter_ends_done(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    seen = []
    controller.ui_state = MagicMock(isCropping=False)
    type(controller.ui_state).rawAwbProgress = property(
        lambda self: seen[-1] if seen else {},
        lambda self, value: seen.append(dict(value)),
    )
    controller.raw_white_balance_to_jpg()
    _wait(controller)
    stages = [m.get("stage") for m in seen if m.get("state") == "running"]
    assert stages[0] == "develop" and "finish" in stages
    assert seen[-1]["state"] == "done" and seen[-1]["active"] is True
    # Progress only reaches the end of its span when the run is done.
    assert all(m["to"] < 1.0 for m in seen if m.get("state") == "running")


# -- Review fixes -------------------------------------------------------------


def test_result_after_folder_change_is_not_applied(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    old = jpg.read_bytes()
    controller.raw_white_balance_to_jpg()
    controller.image_dir = tmp_path / "elsewhere"
    _wait(controller)
    assert jpg.read_bytes() == old
    assert controller.undo_history == []
    assert "another folder" in controller.status_messages[-1]


def test_result_during_crop_is_not_applied(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    old = jpg.read_bytes()
    controller.raw_white_balance_to_jpg()
    controller.ui_state.isCropping = True
    _wait(controller)
    assert jpg.read_bytes() == old
    assert "crop" in controller.status_messages[-1]


def test_worker_that_cannot_start_is_unregistered(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    with patch.object(
        rawb.threading.Thread, "start", side_effect=RuntimeError("no threads")
    ):
        assert controller.raw_white_balance_to_jpg() is False
    assert controller._raw_awb_operations == {}
    assert controller.raw_awb_pending_for_current() is False


def test_failed_staging_leaves_nothing_beside_the_jpg(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, _jpg, _raw = _controller(tmp_path, cli)
    real_copy = rawb.shutil.copyfile

    def broken_copy(src, dst):
        Path(dst).write_bytes(b"partial")
        raise OSError("disk full")

    with patch.object(rawb.shutil, "copyfile", side_effect=broken_copy):
        controller.raw_white_balance_to_jpg()
        _wait(controller)
    assert real_copy is not None
    assert not any(p.name.endswith(".raw-awb.tmp") for p in tmp_path.iterdir())
    assert "disk full" in controller.status_messages[-1]


# -- Edits made while W runs ---------------------------------------------------


def _save_request(jpg: Path, key: str) -> dict:
    source = np.full((40, 60, 3), 0.2, np.float32)
    return {
        "snapshot": {
            "source_arr": source,
            "source_shape": source.shape[:2],
            "edits": {"brightness": 0.3},
            "halo_results": {"layer": object()},
            "depth_map": object(),
        },
        "context": {
            "target": str(jpg),
            "save_image_key": key,
            "save_revision": 3,
            "edit_source_path": str(jpg.with_name("P1010001-backup.JPG")),
        },
    }


def test_save_made_while_w_runs_goes_onto_the_new_pixels(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, size=(60, 40))
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    submitted = []
    controller._submit_save_request_async = MagicMock(
        side_effect=lambda request, saving_status=None: submitted.append(request)
        or True
    )
    gate = threading.Event()
    real_worker = controller._raw_awb_worker
    controller._raw_awb_worker = lambda *a: (gate.wait(5), real_worker(*a))

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    # The user edits and moves on: the save is held, not written.
    assert controller._raw_awb_park_save(request, None) is True
    assert submitted == []
    gate.set()
    _wait(controller)

    assert submitted == [request]
    snapshot = request["snapshot"]
    # Rendered from the new JPG (flat grey), edits kept.
    assert snapshot["source_arr"].mean() == pytest.approx(128 / 255, abs=0.02)
    assert snapshot["edits"] == {"brightness": 0.3}
    # Same size as before, but halo and depth were worked out on the old
    # pixels: the save works them out again.
    assert snapshot["source_shape"] == (40, 60)
    assert snapshot["halo_results"] == {}
    assert snapshot["depth_map"] is None
    assert request["context"]["edit_source_path"] == str(jpg)
    assert "your edits moved onto it" in controller.status_messages[-1]


def test_held_save_is_written_unchanged_when_w_fails(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, exit_code=3, write=False)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    submitted = []
    controller._submit_save_request_async = MagicMock(
        side_effect=lambda request, saving_status=None: submitted.append(request)
        or True
    )
    gate = threading.Event()
    real_worker = controller._raw_awb_worker
    controller._raw_awb_worker = lambda *a: (gate.wait(5), real_worker(*a))

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    original = request["snapshot"]["source_arr"]
    controller._raw_awb_park_save(request, None)
    gate.set()
    _wait(controller)

    assert submitted == [request]
    assert request["snapshot"]["source_arr"] is original
    # The photo's saved recipe is not left hidden for the rest of the session.
    assert controller.raw_awb_recipe_hidden(controller._key(jpg)) is False


def test_live_edits_stay_live_on_the_new_pixels(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    gate = threading.Event()
    real_worker = controller._raw_awb_worker
    controller._raw_awb_worker = lambda *a: (gate.wait(5), real_worker(*a))
    controller._raw_awb_carry_live_session = MagicMock(return_value=True)

    controller.raw_white_balance_to_jpg()
    # The user opens the editor and edits while RawTherapee runs.
    controller.image_editor.current_filepath = jpg
    controller._current_live_session_has_meaningful_edits = MagicMock(return_value=True)
    gate.set()
    _wait(controller)

    controller._raw_awb_carry_live_session.assert_called_once_with(jpg, ANY)
    controller._refresh_after_raw_awb.assert_called_once_with(
        jpg, on_current=True, keep_editor=True
    )


def test_quit_writes_held_saves(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    controller._run_save_request_sync = MagicMock(return_value=True)
    gate = threading.Event()
    real_worker = controller._raw_awb_worker
    controller._raw_awb_worker = lambda *a: (gate.wait(5), real_worker(*a))

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(request, None)
    controller._flush_raw_awb_parked_saves()

    controller._run_save_request_sync.assert_called_once_with(
        request, saving_status=None
    )
    gate.set()
    controller._cancel_raw_white_balance_operations()


# -- Second review round -------------------------------------------------------


def _gated(controller):
    gate = threading.Event()
    real_worker = controller._raw_awb_worker
    controller._raw_awb_worker = lambda *a: (gate.wait(5), real_worker(*a))
    return gate


def _capture_submits(controller):
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    submitted = []
    controller._submit_save_request_async = MagicMock(
        side_effect=lambda request, saving_status=None: submitted.append(request)
        or True
    )
    return submitted


def test_saved_recipe_is_hidden_in_memory_only(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    key = controller._key(jpg)
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    # The editor does not see the old recipe, but the sidecar still has it,
    # so a crash while RawTherapee works loses nothing.
    assert controller.raw_awb_recipe_hidden(key) is True
    controller._persist_sidecar.assert_not_called()
    controller.sidecar.get_metadata.assert_not_called()
    gate.set()
    _wait(controller)
    assert controller.raw_awb_recipe_hidden(key) is False
    # The recipe is cleared only now, with the undo record taken first.
    controller._mark_image_edited_in_sidecar.assert_called_once()


def test_recipe_is_shown_again_when_w_fails(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, exit_code=3, write=False)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller.raw_white_balance_to_jpg()
    _wait(controller)
    assert controller.raw_awb_recipe_hidden(controller._key(jpg)) is False


def test_bad_highlight_setting_registers_nothing(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    with patch.object(
        rawb, "_raw_awb_highlight_mode", side_effect=OverflowError("inf")
    ):
        with pytest.raises(OverflowError):
            controller.raw_white_balance_to_jpg()
    assert controller._raw_awb_operations == {}
    assert controller.raw_awb_recipe_hidden(controller._key(jpg)) is False


def test_waiting_w_is_dropped_when_another_folder_opens(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    key = controller._key(jpg)
    controller._saving_keys[key] = 1

    assert controller.raw_white_balance_to_jpg() is True
    controller.image_dir = tmp_path / "elsewhere"
    controller._saving_keys.pop(key)
    assert _wait_until(lambda: not controller._raw_awb_waiting)
    assert not (tmp_path / "cli-log.json").exists()
    assert controller.undo_history == []
    assert "another folder" in controller.status_messages[-1]


def test_save_of_the_developed_variant_is_not_held(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    gate = _gated(controller)
    controller.raw_white_balance_to_jpg()
    developed = jpg.with_name("P1010001-developed.jpg")
    request = _save_request(jpg, controller._key(jpg))
    request["context"]["target"] = str(developed)
    assert controller._raw_awb_park_save(request, None) is False
    gate.set()
    _wait(controller)


def test_ctrl_s_with_the_editor_left_open_is_written(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    submitted = _capture_submits(controller)
    gate = _gated(controller)
    controller._raw_awb_carry_live_session = MagicMock(return_value=True)

    controller.raw_white_balance_to_jpg()
    # Ctrl+S, then the user keeps editing in the open editor.
    controller.image_editor.current_filepath = jpg
    controller._current_live_session_has_meaningful_edits = MagicMock(return_value=True)
    request = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(request, None)
    gate.set()
    _wait(controller)

    controller._raw_awb_carry_live_session.assert_called_once_with(jpg, ANY)
    assert submitted == [request]
    assert request["snapshot"]["source_arr"].mean() == pytest.approx(
        128 / 255, abs=0.02
    )
    controller._refresh_after_raw_awb.assert_called_once_with(
        jpg, on_current=True, keep_editor=True
    )


def test_unreadable_render_leaves_the_jpg_and_writes_held_edits(qt_app, tmp_path):
    # RawTherapee's JPG is decoded before the swap: if that fails, W is not
    # applied, and the held save goes onto the JPG it was made on.
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    before = jpg.read_bytes()
    submitted = _capture_submits(controller)
    gate = _gated(controller)
    controller._raw_awb_new_pixels = MagicMock(side_effect=RuntimeError("nope"))

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    original = request["snapshot"]["source_arr"]
    controller._raw_awb_park_save(request, None)
    gate.set()
    _wait(controller)

    assert jpg.read_bytes() == before
    assert submitted == [request]
    assert request["snapshot"]["source_arr"] is original
    assert "could not read RawTherapee's JPG" in controller.status_messages[-1]
    assert controller.raw_awb_recipe_hidden(controller._key(jpg)) is False


def test_held_save_points_at_the_jpg_not_the_staged_file(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, size=(60, 40))
    controller, jpg, _raw = _controller(tmp_path, cli)
    submitted = _capture_submits(controller)
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(request, None)
    gate.set()
    _wait(controller)

    assert submitted == [request]
    snapshot = request["snapshot"]
    assert Path(snapshot["filepath_snapshot"]) == jpg
    assert Path(snapshot["source_filepath"]) == jpg
    assert snapshot["current_mtime"] == jpg.stat().st_mtime
    assert not list(tmp_path.glob(".*.raw-awb.tmp"))


def test_held_save_is_queued_for_quit_when_it_cannot_be_submitted(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, size=(60, 40))
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    controller._submit_save_request_async = MagicMock(return_value=False)
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(request, None)
    gate.set()
    _wait(controller)

    # Rebased onto the new pixels, and written by the shutdown recovery.
    assert controller._pending_save_recovery[str(jpg)] is request
    assert request["snapshot"]["source_arr"].mean() == pytest.approx(
        128 / 255, abs=0.02
    )


def _touch_externally(jpg):
    _write_jpg(jpg, (60, 40), (10, 200, 10))
    st = jpg.stat()
    os.utime(jpg, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_held_save_is_not_written_over_an_outside_change(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    submitted = _capture_submits(controller)
    controller._mark_current_live_edit_session_save_failed = MagicMock()
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    request = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(request, None)
    controller._pending_edit_save_requests[controller._key(jpg)] = request
    _touch_externally(jpg)
    outside = jpg.read_bytes()
    gate.set()
    _wait(controller)

    assert submitted == []
    assert jpg.read_bytes() == outside
    # Not restored on return to the photo either: it renders the old pixels.
    assert controller._key(jpg) not in controller._pending_edit_save_requests
    assert "were not written over it" in controller.status_messages[-1]


def test_cli_failure_with_an_outside_change_drops_the_held_save(qt_app, tmp_path):
    cli = _fake_cli(tmp_path, exit_code=3, write=False)
    controller, jpg, _raw = _controller(tmp_path, cli)
    submitted = _capture_submits(controller)
    controller._mark_current_live_edit_session_save_failed = MagicMock()
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    controller._raw_awb_park_save(_save_request(jpg, controller._key(jpg)), None)
    _touch_externally(jpg)
    gate.set()
    _wait(controller)

    assert submitted == []
    assert "were not written over it" in controller.status_messages[-1]


def test_quit_does_not_write_a_held_save_over_an_outside_change(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    controller._mark_current_live_edit_session_save_failed = MagicMock()
    controller._run_save_request_sync = MagicMock(return_value=True)
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    controller._raw_awb_park_save(_save_request(jpg, controller._key(jpg)), None)
    _touch_externally(jpg)
    controller._flush_raw_awb_parked_saves()

    controller._run_save_request_sync.assert_not_called()
    gate.set()
    controller._cancel_raw_white_balance_operations()


def test_save_on_quit_is_not_overwritten_by_an_older_held_save(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    written = []
    controller._run_save_request_sync = MagicMock(
        side_effect=lambda request, saving_status=None: written.append(request) or True
    )
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    held = _save_request(jpg, controller._key(jpg))  # revision 3, Ctrl+S
    controller._raw_awb_park_save(held, None)
    newer = _save_request(jpg, controller._key(jpg))  # revision 4, on quit
    newer["context"]["save_revision"] = 4
    controller._close_discard_token = None
    controller._prepare_current_session_save_request = MagicMock(return_value=newer)
    controller._last_save_prepare_error = None

    assert controller.prepare_for_app_close() is True
    controller._flush_raw_awb_parked_saves()
    assert written == [newer]
    gate.set()
    controller._cancel_raw_white_balance_operations()


def test_held_save_of_another_photo_is_still_written_on_quit(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._mark_current_live_edit_session_submitted = MagicMock()
    controller._remember_pending_edit_save_request = MagicMock()
    controller._run_save_request_sync = MagicMock(return_value=True)
    gate = _gated(controller)

    controller.raw_white_balance_to_jpg()
    held = _save_request(jpg, controller._key(jpg))
    controller._raw_awb_park_save(held, None)
    controller._raw_awb_drop_parked_for(str(tmp_path / "other.jpg"))
    controller._flush_raw_awb_parked_saves()
    controller._run_save_request_sync.assert_called_once_with(held, saving_status=None)
    gate.set()
    controller._cancel_raw_white_balance_operations()


def test_carried_session_drops_halo_and_depth_of_the_old_pixels(qt_app, tmp_path):
    from faststack.imaging.editor import ImageEditor

    jpg = tmp_path / "a.jpg"
    _write_jpg(jpg, (60, 40), (200, 100, 50))
    controller = AppController.__new__(AppController)
    QObject.__init__(controller)
    controller.image_editor = ImageEditor()
    assert controller.image_editor.load_image(str(jpg))
    controller.image_editor._ensure_float_image()
    controller.image_editor._halo_results = {"layer": object()}
    controller.image_editor._depth_map = object()
    controller._halo_token = 0
    controller._halo_jobs = {"layer": ("sig",)}
    controller._halo_errors = {}
    controller._depth_token = 0
    controller._depth_job_session = "session"
    controller._depth_error = None
    controller._set_halo_state = MagicMock()
    controller._set_depth_state = MagicMock()
    controller._schedule_halo_jobs = MagicMock()
    controller._schedule_depth_job = MagicMock()
    # The new JPG has the same size as the old one.
    staged = tmp_path / ".a.jpg.0000.raw-awb.tmp"
    _write_jpg(staged, (60, 40), (128, 128, 128))
    fresh, _ = controller._raw_awb_new_pixels(staged, jpg, snapshot=False)

    assert controller._raw_awb_carry_live_session(jpg, fresh) is True
    editor = controller.image_editor
    assert editor.float_image.mean() == pytest.approx(128 / 255, abs=0.02)
    assert editor._halo_results == {} and editor._depth_map is None
    # Work in flight for the old pixels is superseded and started again.
    assert controller._halo_token == 1 and controller._depth_token == 1
    controller._schedule_halo_jobs.assert_called_once()
    controller._schedule_depth_job.assert_called_once()


# -- Third review round --------------------------------------------------------


def test_safe_replace_checks_before_every_rename_attempt(tmp_path, monkeypatch):
    from faststack.imaging import editor as editor_mod

    target = tmp_path / "a.jpg"
    target.write_bytes(b"old")
    tmp = tmp_path / "a.tmp"
    tmp.write_bytes(b"new")
    monkeypatch.setattr(editor_mod, "_REPLACE_RETRY_DELAY", 0)
    real_replace = os.replace
    attempts = []

    def flaky_replace(src, dst):
        attempts.append("rename")
        if len(attempts) == 1:
            raise PermissionError("locked")
        real_replace(src, dst)

    calls = []

    def check():
        calls.append(len(attempts))
        if len(calls) == 2:
            raise rawb._JpgChanged

    monkeypatch.setattr(editor_mod.os, "replace", flaky_replace)
    with pytest.raises(rawb._JpgChanged):
        editor_mod._safe_replace(tmp, target, check=check)
    # Checked before the first attempt and again before the retry, which
    # was then abandoned with the target untouched.
    assert calls == [0, 1]
    assert target.read_bytes() == b"old"


def test_write_during_the_backup_is_not_replaced(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    real_backup = rawb.create_backup_file

    def backup_then_outside_write(path):
        backup = real_backup(path)
        _touch_externally(path)
        return backup

    with patch.object(rawb, "create_backup_file", backup_then_outside_write):
        controller.raw_white_balance_to_jpg()
        _wait(controller)

    outside = Image.open(jpg).convert("RGB").getpixel((5, 5))
    assert outside[1] > 150  # still the outside write's green
    assert "changed while RawTherapee was running" in controller.status_messages[-1]
    assert not list(tmp_path.glob("*backup*"))
    assert not list(tmp_path.glob(".*.raw-awb.tmp"))
