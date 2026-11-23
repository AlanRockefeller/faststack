"""W: re-render the JPG from its RAW with RawTherapee auto white balance."""

import json
import os
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    tmp_path: Path, *, size=(60, 40), exit_code=0, write=True, source=None
) -> Path:
    """A stand-in rawtherapee-cli: records argv + profile, writes a JPEG
    (flat grey, or a copy of ``source``)."""
    log_path = tmp_path / "cli-log.json"
    script = tmp_path / "bin" / "rawtherapee-cli"
    script.parent.mkdir()
    script.write_text(
        textwrap.dedent(
            f"""\
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
            if {write!r} and {str(source) if source else None!r}:
                Image.open({str(source) if source else None!r}).save(out, "JPEG")
            elif {write!r}:
                Image.new("RGB", {size!r}, (128, 128, 128)).save(out, "JPEG")
            print("Processing " + argv[-1])
            sys.exit({exit_code!r})
            """
        )
    )
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
        ["P1010001.JPG", "P1010001-backup.JPG", "P1010001.ORF", "cli-log.json"]
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
    controller._refresh_after_raw_awb.assert_called_once_with(jpg, on_current=True)
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


def test_refuses_with_unsaved_edits(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._is_current_live_edit_session_dirty.return_value = True
    controller.image_editor.current_filepath = jpg

    # In the loupe Ctrl+S toggles Stacked, so the hint must not suggest it.
    controller.ui_state.isEditorOpen = False
    assert controller.raw_white_balance_to_jpg() is False
    assert "unsaved edits" in controller.status_messages[-1]
    assert "Ctrl+S" not in controller.status_messages[-1]

    controller.ui_state.isEditorOpen = True
    assert controller.raw_white_balance_to_jpg() is False
    assert "Ctrl+S" in controller.status_messages[-1]
    assert not (tmp_path / "cli-log.json").exists()


def test_refuses_while_saving(qt_app, tmp_path):
    cli = _fake_cli(tmp_path)
    controller, jpg, _raw = _controller(tmp_path, cli)
    controller._saving_keys[controller._key(jpg)] = 1

    assert controller.raw_white_balance_to_jpg() is False
    assert "saving" in controller.status_messages[-1]


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
