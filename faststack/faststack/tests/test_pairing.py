"""Tests for the RAW-JPG pairing logic."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faststack.io.indexer import find_images, _find_raw_pair

@pytest.fixture
def mock_image_dir(tmp_path: Path):
    """Creates a temporary directory with mock image files."""
    # JPGs
    (tmp_path / "IMG_0001.JPG").touch()
    time.sleep(0.01)
    (tmp_path / "IMG_0002.jpg").touch()
    time.sleep(0.01)
    (tmp_path / "IMG_0003.jpeg").touch()
    time.sleep(0.01)

    # Raws (CR3)
    (tmp_path / "IMG_0001.CR3").touch() # Perfect match
    # Match for 0002, but with a slight time diff
    two_cr3 = (tmp_path / "IMG_0002.CR3")
    two_cr3.touch()
    # Change timestamp slightly
    os.utime(two_cr3, (two_cr3.stat().st_atime, two_cr3.stat().st_mtime + 0.5))

    # A raw with no JPG
    (tmp_path / "IMG_0004.CR3").touch()

    return tmp_path

def test_find_images(mock_image_dir: Path):
    """Tests the main find_images function."""
    images = find_images(mock_image_dir)

    assert len(images) == 3
    assert images[0].path.name == "IMG_0001.JPG"
    assert images[0].raw_pair is not None
    assert images[0].raw_pair.name == "IMG_0001.CR3"

    assert images[1].path.name == "IMG_0002.jpg"
    assert images[1].raw_pair is not None
    assert images[1].raw_pair.name == "IMG_0002.CR3"

    assert images[2].path.name == "IMG_0003.jpeg"
    assert images[2].raw_pair is None

def test_raw_pairing_logic():
    """Unit tests the _find_raw_pair function specifically."""
    jpg_path = Path("IMG_01.JPG")
    jpg_stat = MagicMock(); jpg_stat.st_mtime = 1000.0

    # Case 1: Perfect match
    raw1_path = Path("IMG_01.CR3"); raw1_stat = MagicMock(); raw1_stat.st_mtime = 1000.1
    potentials = [(raw1_path, raw1_stat)]
    assert _find_raw_pair(jpg_path, jpg_stat, potentials) == raw1_path

    # Case 2: No match (time delta too large)
    raw2_path = Path("IMG_01.CR3"); raw2_stat = MagicMock(); raw2_stat.st_mtime = 1003.0
    potentials = [(raw2_path, raw2_stat)]
    assert _find_raw_pair(jpg_path, jpg_stat, potentials) is None

    # Case 3: Closest match is chosen
    raw3_path = Path("IMG_01_A.CR3"); raw3_stat = MagicMock(); raw3_stat.st_mtime = 1000.5
    raw4_path = Path("IMG_01_B.CR3"); raw4_stat = MagicMock(); raw4_stat.st_mtime = 1001.8
    potentials = [(raw3_path, raw3_stat), (raw4_path, raw4_stat)]
    assert _find_raw_pair(jpg_path, jpg_stat, potentials) == raw3_path

    # Case 4: No potential RAWs
    assert _find_raw_pair(jpg_path, jpg_stat, []) is None
