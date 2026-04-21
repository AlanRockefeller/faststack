"""Tests for the RAW-JPG pairing logic."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from faststack.io.indexer import _find_raw_pair, find_images


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
    (tmp_path / "IMG_0001.CR3").touch()  # Perfect match
    # Match for 0002, but with a slight time diff
    two_cr3 = tmp_path / "IMG_0002.CR3"
    two_cr3.touch()
    # Change timestamp slightly
    os.utime(two_cr3, (two_cr3.stat().st_atime, two_cr3.stat().st_mtime + 0.5))

    # A raw with no JPG
    (tmp_path / "IMG_0004.CR3").touch()

    return tmp_path


def test_find_images(mock_image_dir: Path):
    """Tests the main find_images function."""
    images = find_images(mock_image_dir)

    assert len(images) == 4
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
    jpg_stat = MagicMock()
    jpg_stat.st_mtime = 1000.0

    # Case 1: Single same-stem candidate always pairs, even with a large mtime gap
    raw1_path = Path("IMG_01.NEF")
    raw1_stat = MagicMock()
    raw1_stat.st_mtime = 1010.0
    potentials = [(raw1_path, raw1_stat)]
    assert _find_raw_pair(jpg_path, jpg_stat, potentials) == raw1_path

    # Case 2: With multiple same-stem candidates, closest mtime wins
    raw2_path = Path("IMG_01.DNG")
    raw2_stat = MagicMock()
    raw2_stat.st_mtime = 1004.0
    raw3_path = Path("IMG_01.CR3")
    raw3_stat = MagicMock()
    raw3_stat.st_mtime = 1001.0
    raw4_path = Path("IMG_01.ARW")
    raw4_stat = MagicMock()
    raw4_stat.st_mtime = 1008.0
    potentials = [
        (raw2_path, raw2_stat),
        (raw3_path, raw3_stat),
        (raw4_path, raw4_stat),
    ]
    assert _find_raw_pair(jpg_path, jpg_stat, potentials) == raw3_path

    # Case 4: No potential RAWs
    assert _find_raw_pair(jpg_path, jpg_stat, []) is None


def test_find_images_pairs_same_stem_nef_beyond_two_seconds(tmp_path: Path):
    """A single same-stem NEF should pair even when mtimes are far apart."""
    jpg_path = tmp_path / "DSC_0001.JPG"
    raw_path = tmp_path / "DSC_0001.NEF"

    jpg_path.touch()
    raw_path.touch()
    os.utime(jpg_path, (1000, 1000))
    os.utime(raw_path, (1010, 1010))

    images = find_images(tmp_path)

    assert len(images) == 1
    assert images[0].path.name == "DSC_0001.JPG"
    assert images[0].raw_pair == raw_path


def test_find_images_multi_candidate_same_stem_uses_closest_mtime(tmp_path: Path):
    """Multiple same-stem RAW candidates should be resolved by closest mtime."""
    jpg_path = tmp_path / "DSC_0002.JPG"
    nef_path = tmp_path / "DSC_0002.NEF"
    dng_path = tmp_path / "DSC_0002.DNG"

    jpg_path.touch()
    nef_path.touch()
    dng_path.touch()
    os.utime(jpg_path, (1000, 1000))
    os.utime(nef_path, (1007, 1007))
    os.utime(dng_path, (1002, 1002))

    images = find_images(tmp_path)

    assert len(images) == 2

    jpg_image = next(im for im in images if im.path == jpg_path)
    raw_only_image = next(im for im in images if im.path == nef_path)

    assert jpg_image.raw_pair == dng_path
    assert raw_only_image.raw_pair == nef_path


def test_find_images_developed_artifact_behavior_is_preserved(tmp_path: Path):
    """Developed JPGs stay unpaired while the base JPG keeps the RAW pair."""
    jpg_path = tmp_path / "A.jpg"
    raw_path = tmp_path / "A.NEF"
    developed_path = tmp_path / "A-developed.jpg"

    jpg_path.touch()
    raw_path.touch()
    developed_path.touch()
    os.utime(jpg_path, (1000, 1000))
    os.utime(raw_path, (1010, 1010))
    os.utime(developed_path, (3000, 3000))

    images = find_images(tmp_path)

    assert len(images) == 2

    img_a = next(im for im in images if im.path == jpg_path)
    img_dev = next(im for im in images if im.path == developed_path)

    assert img_a.raw_pair == raw_path
    assert img_dev.raw_pair is None
    assert [im.path.name for im in images] == ["A.jpg", "A-developed.jpg"]
