import os
from faststack.io.indexer import find_images


def test_developed_sorting_adjacency(tmp_path):
    """
    Test that developed images appear immediately after their base images,
    regardless of their filesystem modification time.
    """
    # Setup files:
    # A.jpg (old)
    # B.jpg (mid)
    # A-developed.jpg (new)

    a_path = tmp_path / "A.jpg"
    b_path = tmp_path / "B.jpg"
    a_dev_path = tmp_path / "A-developed.jpg"

    a_path.touch()
    os.utime(a_path, (1000, 1000))

    b_path.touch()
    os.utime(b_path, (2000, 2000))

    a_dev_path.touch()
    os.utime(a_dev_path, (3000, 3000))

    images = find_images(tmp_path)

    # Expected order: A.jpg, A-developed.jpg, B.jpg
    # Because A-developed matches A, and A is older than B.
    # Without the fix, A-developed (3000) would be after B (2000).

    names = [im.path.name for im in images]
    assert names == ["A.jpg", "A-developed.jpg", "B.jpg"]


def test_developed_orphan_sorting(tmp_path):
    """
    Test that a developed image without a base image is sorted by its own mtime.
    """
    # A.jpg (1000)
    # B-developed.jpg (2000) - orphan
    # C.jpg (3000)

    (tmp_path / "A.jpg").touch()
    os.utime(tmp_path / "A.jpg", (1000, 1000))

    (tmp_path / "B-developed.jpg").touch()
    os.utime(tmp_path / "B-developed.jpg", (2000, 2000))

    (tmp_path / "C.jpg").touch()
    os.utime(tmp_path / "C.jpg", (3000, 3000))

    images = find_images(tmp_path)
    names = [im.path.name for im in images]
    assert names == ["A.jpg", "B-developed.jpg", "C.jpg"]


def test_base_resolution_preference(tmp_path):
    """
    Test that A-developed.jpg prefers A.jpg over A (1).jpg.
    """
    (tmp_path / "A.jpg").touch()
    os.utime(tmp_path / "A.jpg", (1000, 1000))

    (tmp_path / "A (1).jpg").touch()
    os.utime(tmp_path / "A (1).jpg", (1100, 1100))

    (tmp_path / "A-developed.jpg").touch()
    os.utime(tmp_path / "A-developed.jpg", (3000, 3000))

    images = find_images(tmp_path)
    names = [im.path.name for im in images]

    # A-developed should match A.jpg and stay at 1000 (after A.jpg)
    # Order: A.jpg (1000), A-developed.jpg (1000 rank 1), A (1).jpg (1100)
    assert names == ["A.jpg", "A-developed.jpg", "A (1).jpg"]


def test_raw_pairing_with_developed(tmp_path):
    """
    Test that A.orf pairs with A.jpg, not A-developed.jpg.
    AND that A-developed still appears adjacent to A.jpg.
    """
    a_jpg = tmp_path / "A.jpg"
    a_orf = tmp_path / "A.orf"
    a_dev = tmp_path / "A-developed.jpg"

    a_jpg.touch()
    os.utime(a_jpg, (1000, 1000))

    a_orf.touch()
    os.utime(a_orf, (1000, 1000))

    a_dev.touch()
    os.utime(a_dev, (3000, 3000))

    images = find_images(tmp_path)

    # Should have 2 images in list:
    # 1. A.jpg (paired with A.orf)
    # 2. A-developed.jpg (no pair)

    assert len(images) == 2

    # Check pairing
    img_a = next(im for im in images if im.path.name == "A.jpg")
    img_dev = next(im for im in images if im.path.name == "A-developed.jpg")

    assert img_a.raw_pair is not None
    assert img_a.raw_pair.name == "A.orf"
    assert img_dev.raw_pair is None

    # Check ordering
    names = [im.path.name for im in images]
    assert names == ["A.jpg", "A-developed.jpg"]


def test_case_insensitivity(tmp_path):
    """Test that a-DEVELOPED.JPG matches A.jpg."""
    (tmp_path / "A.jpg").touch()
    os.utime(tmp_path / "A.jpg", (1000, 1000))

    (tmp_path / "a-DEVELOPED.JPG").touch()
    os.utime(tmp_path / "a-DEVELOPED.JPG", (3000, 3000))

    images = find_images(tmp_path)
    names = [im.path.name for im in images]
    # Note: casefold sorting might affect order if original names differ only in case,
    # but here they are grouped by A.jpg's time.
    assert names == ["A.jpg", "a-DEVELOPED.JPG"]


def test_orphan_chain_prevention(tmp_path):
    """
    A-developed (1).jpg should be treated as an orphan,
    not matched to A-developed.jpg or A.jpg accidentally.
    """
    (tmp_path / "A.jpg").touch()
    os.utime(tmp_path / "A.jpg", (1000, 1000))

    (tmp_path / "A-developed.jpg").touch()
    os.utime(tmp_path / "A-developed.jpg", (1100, 1100))

    # This one has -developed (1) suffix.
    # Our simple logic should either not match it or match it to A (1).jpg if it existed.
    # Without A (1).jpg, it should be an orphan.
    (tmp_path / "A-developed (1).jpg").touch()
    os.utime(tmp_path / "A-developed (1).jpg", (1200, 1200))

    images = find_images(tmp_path)
    names = [im.path.name for im in images]
    assert names == ["A.jpg", "A-developed.jpg", "A-developed (1).jpg"]


def test_tiebreaker_stability(tmp_path):
    """
    Test that the tiebreaker (last element of the sorting key)
    provides stable ordering when mtime and casefolded names are identical.
    """
    p1 = tmp_path / "100.jpg"
    p2 = tmp_path / "200.jpg"

    p1.touch()
    os.utime(p1, (1000, 1000))

    p2.touch()
    os.utime(p2, (1000, 1000))

    images = find_images(tmp_path)
    names = [im.path.name for im in images]

    # Both have same mtime (1000) and priority 0.
    # Tiebreakers are now name-based, so "100.jpg" comes before "200.jpg".
    assert names == ["100.jpg", "200.jpg"]
