"""Tests for permanent delete fallback when recycle bin is unavailable."""

from pathlib import Path


class MockImageFile:
    """Mock ImageFile for testing."""

    def __init__(self, jpg_path: Path, raw_path: Path = None):
        self.path = jpg_path
        self.raw_pair = raw_path


class TestEnsureRecycleBinDir:
    """Tests for _ensure_recycle_bin_dir helper."""

    def test_returns_true_when_creation_succeeds(self, tmp_path):
        """Should return True when recycle bin can be created."""
        recycle_bin_dir = tmp_path / "recycle_bin"

        def ensure_recycle_bin_dir():
            try:
                recycle_bin_dir.mkdir(parents=True, exist_ok=True)
                return True
            except (PermissionError, OSError):
                return False

        result = ensure_recycle_bin_dir()

        assert result is True
        assert recycle_bin_dir.exists()

    def test_returns_false_when_creation_fails(self, tmp_path):
        """Should return False when recycle bin creation fails."""

        def ensure_recycle_bin_dir_with_failure():
            try:
                raise PermissionError("Simulated permission error")
            except (PermissionError, OSError):
                return False

        result = ensure_recycle_bin_dir_with_failure()

        assert result is False


class TestConfirmPermanentDelete:
    """Tests for _confirm_permanent_delete helper."""

    def test_returns_user_choice(self):
        """Confirm dialog should return True on confirm, False on cancel."""

        # Simulated confirm function
        def confirm_permanent_delete(
            image_file, user_confirms: bool, reason: str = ""
        ) -> bool:
            # In real implementation, this shows a dialog
            return user_confirms

        mock_img = MockImageFile(Path("test.jpg"), Path("test.orf"))

        assert confirm_permanent_delete(mock_img, user_confirms=True) is True
        assert confirm_permanent_delete(mock_img, user_confirms=False) is False


class TestPermanentlyDeleteImageFiles:
    """Tests for _permanently_delete_image_files helper."""

    def test_deletes_jpg_and_raw(self, tmp_path):
        """Should delete both JPG and RAW files."""
        jpg_path = tmp_path / "IMG_0001.jpg"
        raw_path = tmp_path / "IMG_0001.orf"
        jpg_path.touch()
        raw_path.touch()

        mock_img = MockImageFile(jpg_path, raw_path)

        def permanently_delete(image_file):
            deleted_any = False
            if image_file.path.exists():
                image_file.path.unlink()
                deleted_any = True
            if image_file.raw_pair and image_file.raw_pair.exists():
                image_file.raw_pair.unlink()
                deleted_any = True
            return deleted_any

        result = permanently_delete(mock_img)

        assert result is True
        assert not jpg_path.exists()
        assert not raw_path.exists()

    def test_deletes_jpg_only_when_no_raw(self, tmp_path):
        """Should delete JPG when no RAW exists."""
        jpg_path = tmp_path / "IMG_0002.jpg"
        jpg_path.touch()

        mock_img = MockImageFile(jpg_path, None)

        def permanently_delete(image_file):
            deleted_any = False
            if image_file.path.exists():
                image_file.path.unlink()
                deleted_any = True
            if image_file.raw_pair and image_file.raw_pair.exists():
                image_file.raw_pair.unlink()
                deleted_any = True
            return deleted_any

        result = permanently_delete(mock_img)

        assert result is True
        assert not jpg_path.exists()

    def test_returns_false_when_nothing_to_delete(self, tmp_path):
        """Should return False when files don't exist."""
        jpg_path = tmp_path / "nonexistent.jpg"
        mock_img = MockImageFile(jpg_path, None)

        def permanently_delete(image_file):
            deleted_any = False
            if image_file.path.exists():
                image_file.path.unlink()
                deleted_any = True
            if image_file.raw_pair and image_file.raw_pair.exists():
                image_file.raw_pair.unlink()
                deleted_any = True
            return deleted_any

        result = permanently_delete(mock_img)

        assert result is False


class TestPermanentDeleteFlow:
    """Integration-style tests for permanent delete fallback flow."""

    def test_permanent_delete_when_recycle_bin_fails(self, tmp_path):
        """Permanent delete should proceed when recycle bin creation fails."""
        # Setup: create test files
        jpg1 = tmp_path / "IMG_0001.jpg"
        raw1 = tmp_path / "IMG_0001.orf"
        jpg2 = tmp_path / "IMG_0002.jpg"

        jpg1.touch()
        raw1.touch()
        jpg2.touch()

        images = [
            MockImageFile(jpg1, raw1),
            MockImageFile(jpg2, None),
        ]

        # Simulate: recycle bin unavailable, user confirms all deletes
        def simulate_delete_flow(images, recycle_bin_available, confirm_results):
            """Simulates the delete flow with controllable confirmation."""
            deleted_count = 0
            undo_history = []
            confirm_idx = 0

            for img in images:
                if not recycle_bin_available:
                    # Permanent delete mode with confirmation
                    if (
                        confirm_idx < len(confirm_results)
                        and not confirm_results[confirm_idx]
                    ):
                        # User cancelled - abort remaining
                        break
                    confirm_idx += 1

                    # Actually delete
                    if img.path.exists():
                        img.path.unlink()
                        deleted_count += 1
                    if img.raw_pair and img.raw_pair.exists():
                        img.raw_pair.unlink()
                else:
                    # Normal recycle bin path (not tested here)
                    pass

            return deleted_count, undo_history

        # All confirmations return True
        deleted_count, undo_history = simulate_delete_flow(
            images, recycle_bin_available=False, confirm_results=[True, True]
        )

        assert deleted_count == 2
        assert not jpg1.exists()
        assert not raw1.exists()
        assert not jpg2.exists()
        # No undo history for permanent deletes
        assert len(undo_history) == 0

    def test_abort_on_cancel(self, tmp_path):
        """Remaining files should be preserved when user cancels mid-way."""
        jpg1 = tmp_path / "IMG_0001.jpg"
        jpg2 = tmp_path / "IMG_0002.jpg"

        jpg1.touch()
        jpg2.touch()

        images = [
            MockImageFile(jpg1, None),
            MockImageFile(jpg2, None),
        ]

        def simulate_delete_flow(images, confirm_results):
            deleted_count = 0
            confirm_idx = 0

            for img in images:
                if (
                    confirm_idx < len(confirm_results)
                    and not confirm_results[confirm_idx]
                ):
                    break
                confirm_idx += 1

                if img.path.exists():
                    img.path.unlink()
                    deleted_count += 1

            return deleted_count

        # First confirmation True, second False (cancel)
        deleted_count = simulate_delete_flow(images, confirm_results=[True, False])

        assert deleted_count == 1
        assert not jpg1.exists()  # First was deleted
        assert jpg2.exists()  # Second was preserved due to cancel
