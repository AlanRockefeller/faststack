import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import shutil
import subprocess
import numpy as np
from PIL import Image
from dataclasses import dataclass
import logging

from faststack.app import AppController
from faststack.imaging.editor import ImageEditor

# Ensure logs are visible
logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


@dataclass
class DummyImageFile:
    """
    Minimal stand-in for faststack.models.ImageFile.

    This avoids test-order issues where other tests may monkeypatch
    sys.modules["faststack.models"] and turn ImageFile into a MagicMock.
    """

    path: Path
    raw_pair: Path | None = None

    @property
    def has_raw(self) -> bool:
        return self.raw_pair is not None and self.raw_pair.exists()

    @property
    def raw_path(self) -> Path:
        if self.raw_pair is None:
            raise AttributeError("No RAW pair")
        return self.raw_pair

    @property
    def working_tif_path(self) -> Path:
        # match existing expectations: "<stem>-working.tif"
        return self.path.with_name(f"{self.path.stem}-working.tif")

    @property
    def developed_jpg_path(self) -> Path:
        return self.path.with_name(f"{self.path.stem}-developed.jpg")


class TestRawPipeline(unittest.TestCase):
    @patch("faststack.app.os.path.exists")
    @patch("faststack.app.subprocess.run")
    @patch("faststack.config.config.get")
    @patch("faststack.app.threading.Thread")
    def test_develop_raw_empty_output_cleanup(
        self, mock_thread, mock_config_get, mock_run, mock_exists
    ):
        """Test garbage collection if RT exits 0 but produces 0-byte file."""
        mock_config_get.return_value = "c:\\path\\to\\rawtherapee-cli.exe"
        mock_exists.return_value = True  # exe exists

        # Make Thread().start() run the target immediately (synchronous for testing)
        def side_effect_start(*args, **kwargs):
            _, thread_kwargs = mock_thread.call_args
            target = thread_kwargs.get("target")
            if target:
                target()

        mock_thread.return_value.start.side_effect = side_effect_start

        # Mock subprocess.run to return success (returncode=0)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        app = MagicMock()
        app.image_files = [self.image_file]
        app.current_index = 0
        app.update_status_message = MagicMock()

        # Bind the real _develop_raw_backend method to our mock
        app._develop_raw_backend = AppController._develop_raw_backend.__get__(
            app, AppController
        )

        # Create 0-byte zombie file BEFORE calling develop
        tif_path = self.image_file.working_tif_path
        tif_path.touch()
        self.assertTrue(tif_path.exists())
        self.assertEqual(tif_path.stat().st_size, 0)

        app._develop_raw_backend()

        # Expect file to be DELETED because it was 0 bytes
        self.assertFalse(tif_path.exists(), "Zombie 0-byte file should be cleaned up")

    @patch("faststack.app.QTimer.singleShot")
    @patch("faststack.app.os.path.exists")
    @patch("faststack.app.subprocess.run")
    @patch("faststack.config.config.get")
    @patch("faststack.app.threading.Thread")
    def test_develop_raw_timeout(
        self, mock_thread, mock_config_get, mock_run, mock_exists, mock_single_shot
    ):
        mock_config_get.return_value = "c:\\path\\to\\rawtherapee-cli.exe"
        mock_exists.return_value = True

        def side_effect_start(*args, **kwargs):
            _, thread_kwargs = mock_thread.call_args
            target = thread_kwargs.get("target")
            if target:
                target()

        mock_thread.return_value.start.side_effect = side_effect_start

        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="rawtherapee-cli", timeout=60
        )

        app = MagicMock()
        app.image_files = [self.image_file]
        app.current_index = 0
        app._develop_raw_backend = AppController._develop_raw_backend.__get__(
            app, AppController
        )

        app._develop_raw_backend()

        # Check QTimer call
        mock_single_shot.assert_called()
        # call_args[0] is (0, partial_obj)
        _, callback = mock_single_shot.call_args[0]
        # callback is functools.partial(self._on_develop_finished, False, err_msg)
        # For a bound method, callback.func is the method
        self.assertTrue(hasattr(callback, "func"))
        self.assertTrue("_on_develop_finished" in str(callback.func))
        self.assertEqual(callback.args[0], False)  # Success = False
        self.assertIn("timed out", callback.args[1])  # Msg

    @patch("faststack.app.os.path.exists")
    @patch("faststack.app.subprocess.run")
    @patch("faststack.config.config.get")
    @patch("faststack.app.threading.Thread")
    def test_develop_raw_with_custom_args(
        self, mock_thread, mock_config_get, mock_run, mock_exists
    ):
        """Test that custom RawTherapee args are correctly passed to the command."""

        # Setup mock behavior for config.get
        def mock_config_side_effect(section, option):
            if section == "rawtherapee" and option == "exe":
                return "c:\\path\\to\\rawtherapee-cli.exe"
            if section == "rawtherapee" and option == "args":
                return "-p my_profile.pp3 -s"
            return None

        mock_config_get.side_effect = mock_config_side_effect
        mock_exists.return_value = True

        # Run target in thread immediately
        def side_effect_start(*args, **kwargs):
            _, thread_kwargs = mock_thread.call_args
            target = thread_kwargs.get("target")
            if target:
                target()

        mock_thread.return_value.start.side_effect = side_effect_start

        # Mock subprocess.run
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )

        app = MagicMock()
        app.image_files = [self.image_file]
        app.current_index = 0
        app._develop_raw_backend = AppController._develop_raw_backend.__get__(
            app, AppController
        )

        app._develop_raw_backend()

        # Verify command
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]

        # Check base command structure
        self.assertEqual(cmd[0], "c:\\path\\to\\rawtherapee-cli.exe")
        self.assertIn("-t", cmd)
        self.assertIn("-b16", cmd)
        self.assertIn("-Y", cmd)

        # Check custom args
        self.assertIn("-p", cmd)
        self.assertIn("my_profile.pp3", cmd)
        self.assertIn("-s", cmd)

        # Check input/output order (input -c should be after args)
        self.assertEqual(cmd[-2], "-c")
        self.assertEqual(cmd[-1], str(self.image_file.raw_path))

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)

        # Setup dummy RAW file
        self.raw_path = self.tmp_path / "test_image.CR2"
        self.raw_path.touch()

        # Setup dummy JPG for indexer (FastStack usually finds JPGs first)
        self.jpg_path = self.tmp_path / "test_image.jpg"
        # Create a real small JPG
        img = Image.new("RGB", (100, 100), color="red")
        img.save(self.jpg_path)

        self.image_file = DummyImageFile(path=self.jpg_path, raw_pair=self.raw_path)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_image_file_properties(self):
        """Test computed properties for RAW pipeline."""
        self.assertTrue(self.image_file.has_raw)
        self.assertEqual(self.image_file.raw_path, self.tmp_path / "test_image.CR2")
        self.assertEqual(
            self.image_file.working_tif_path, self.tmp_path / "test_image-working.tif"
        )
        self.assertEqual(
            self.image_file.developed_jpg_path,
            self.tmp_path / "test_image-developed.jpg",
        )

        # Rename raw to break pairing
        shutil.move(self.raw_path, self.tmp_path / "other.CR2")
        img2 = DummyImageFile(path=self.jpg_path, raw_pair=self.raw_path)
        self.assertFalse(img2.has_raw)

    @patch("faststack.app.os.path.exists")
    @patch("faststack.app.subprocess.run")
    @patch("faststack.config.config.get")
    def test_develop_raw_slot(self, mock_config_get, mock_run, mock_exists):
        """Test the develop_raw_for_current_image slot."""
        # Mock Config
        mock_config_get.return_value = "c:\\path\\to\\rawtherapee-cli.exe"
        mock_exists.return_value = True  # Pretend exe exists

        # Mock AppController partial environment
        app = MagicMock()
        app.image_files = [self.image_file]
        app.current_index = 0
        app.update_status_message = MagicMock()
        app.load_image_for_editing = MagicMock()
        app.enable_raw_editing = MagicMock()

        # Bind real method
        func = AppController.develop_raw_for_current_image.__get__(app, AppController)
        func()

        # Verify delegation
        app.enable_raw_editing.assert_called_once()

    def test_editor_float_pipeline_io(self):
        """
        Test that editor save path + developed sidecar logic runs.

        IMPORTANT:
        - Production code requires OpenCV for writing 16-bit TIFF.
        - Test env may not have cv2 installed.
        - So we stub ImageEditor._write_tiff_16bit to make this test deterministic
          and independent of OpenCV availability.
        """
        editor = ImageEditor()

        tif_path = self.tmp_path / "working-working.tif"
        tif_path.touch()  # Ensure it exists for backup logic

        editor.load_image(str(self.jpg_path))
        editor.current_filepath = tif_path  # Trick it into "saving a TIFF"

        editor.current_edits["exposure"] = 1.0  # +1 EV -> 2x gain

        def fake_write_tiff_16bit(path: Path, arr_float: np.ndarray):
            # Write a minimal TIFF header so downstream checks are meaningful.
            # Little-endian TIFF: "II*\x00"
            with open(path, "wb") as f:
                f.write(b"II\x2a\x00")

        with patch.object(
            editor, "_write_tiff_16bit", side_effect=fake_write_tiff_16bit
        ):
            res = editor.save_image(write_developed_jpg=True)

        self.assertIsNotNone(res)
        saved_path, backup_path = res

        self.assertEqual(saved_path, tif_path)
        self.assertTrue(tif_path.exists())

        # Developed JPG naming: strip one "-working" suffix if present
        expected_dev_path = self.tmp_path / "working-developed.jpg"
        self.assertTrue(
            expected_dev_path.exists(), f"Expected {expected_dev_path} to exist"
        )

        # Verify TIFF Content (Basic)
        with open(tif_path, "rb") as f:
            header = f.read(4)
            self.assertEqual(header, b"II\x2a\x00")  # Little endian TIFF

        # Backup should exist
        self.assertTrue(backup_path.exists(), "Expected backup file to exist")

    def test_editor_edit_float_logic(self):
        """Test float math."""
        editor = ImageEditor()
        arr = np.ones((10, 10, 3), dtype=np.float32) * 0.5  # Mid gray

        # Exposure +1 (2x gain in linear space)
        # 0.5 sRGB is ~0.214 linear. 2x -> 0.428 linear. 0.428 linear is ~0.6858 sRGB.
        edits = {"exposure": 1.0}
        res = editor._apply_edits(arr.copy(), edits, for_export=True)
        np.testing.assert_allclose(res, 0.6858, atol=0.01)

        # Exposure -1 (0.5x gain in linear space)
        # 0.5 sRGB is ~0.214 linear. 0.5x -> 0.107 linear. 0.107 linear is ~0.3617 sRGB.
        edits = {"exposure": -1.0}
        res = editor._apply_edits(arr.copy(), edits, for_export=True)
        np.testing.assert_allclose(res, 0.3617, atol=0.01)

        # Brightness (sRGB Multiplication)
        # Brightness 0.5 -> 1.5x gain on sRGB
        # 0.5 sRGB * 1.5 = 0.75 sRGB.
        edits = {"brightness": 0.5}
        res = editor._apply_edits(arr.copy(), edits, for_export=True)
        np.testing.assert_allclose(res, 0.75, atol=0.01)
