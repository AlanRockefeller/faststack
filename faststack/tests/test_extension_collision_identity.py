from pathlib import Path

from PIL import Image

from faststack.app import AppController
from faststack.io.indexer import find_images_with_variants
from faststack.io.sidecar import SidecarManager
from faststack.io.variants import get_group_key_for_path


def _write_jpeg(path: Path, color: tuple[int, int, int], description: str) -> None:
    image = Image.new("RGB", (8, 8), color)
    exif = Image.Exif()
    exif[0x010E] = description
    image.save(path, exif=exif)


def test_jpg_and_jpeg_same_stem_keep_distinct_identity(tmp_path):
    jpg = tmp_path / "photo.jpg"
    jpeg = tmp_path / "photo.jpeg"
    _write_jpeg(jpg, (255, 0, 0), "jpg description")
    _write_jpeg(jpeg, (0, 0, 255), "jpeg description")

    images, variant_map = find_images_with_variants(tmp_path)

    assert {image.path.name for image in images} == {"photo.jpg", "photo.jpeg"}
    jpg_key = get_group_key_for_path(jpg, variant_map)
    jpeg_key = get_group_key_for_path(jpeg, variant_map)
    assert jpg_key is not None and jpeg_key is not None and jpg_key != jpeg_key
    assert variant_map[jpg_key].main_path == jpg
    assert variant_map[jpeg_key].main_path == jpeg

    controller = AppController.__new__(AppController)
    controller.image_files = images
    controller._variant_map = variant_map
    exif_sources = {
        image.path.name: controller._exif_source_path(index)
        for index, image in enumerate(images)
    }
    assert exif_sources == {"photo.jpg": jpg, "photo.jpeg": jpeg}

    with Image.open(jpg) as image:
        assert image.getexif()[0x010E] == "jpg description"
        assert image.getpixel((0, 0))[0] > image.getpixel((0, 0))[2]
    with Image.open(jpeg) as image:
        assert image.getexif()[0x010E] == "jpeg description"
        assert image.getpixel((0, 0))[2] > image.getpixel((0, 0))[0]


def test_jpg_and_jpeg_same_stem_do_not_share_sidecar_flags(tmp_path):
    jpg = tmp_path / "photo.jpg"
    jpeg = tmp_path / "photo.jpeg"
    _write_jpeg(jpg, (255, 0, 0), "jpg")
    _write_jpeg(jpeg, (0, 0, 255), "jpeg")
    sidecar = SidecarManager(tmp_path, None)

    jpg_meta = sidecar.get_metadata(jpg)
    jpeg_meta = sidecar.get_metadata(jpeg)
    jpg_meta.favorite = True
    jpeg_meta.uploaded = True

    assert jpg_meta is not jpeg_meta
    assert sidecar.metadata_key_for_path(jpg) == "photo.jpg"
    assert sidecar.metadata_key_for_path(jpeg) == "photo.jpeg"
    assert jpg_meta.favorite is True and jpg_meta.uploaded is False
    assert jpeg_meta.favorite is False and jpeg_meta.uploaded is True
    assert sidecar.save() is True

    reloaded = SidecarManager(tmp_path, None)
    assert reloaded.get_metadata(jpg, create=False).favorite is True
    assert reloaded.get_metadata(jpg, create=False).uploaded is False
    assert reloaded.get_metadata(jpeg, create=False).favorite is False
    assert reloaded.get_metadata(jpeg, create=False).uploaded is True
