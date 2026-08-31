from faststack.io.sidecar import SidecarManager
from faststack.models import ImageFile


def test_permanently_deleted_name_does_not_lend_metadata_to_future_file(
    app_controller, tmp_path
):
    image_dir = tmp_path / "metadata"
    image_dir.mkdir()
    path = image_dir / "photo.jpg"
    path.write_bytes(b"old image")
    sidecar = SidecarManager(image_dir, None)
    metadata = sidecar.get_metadata(path)
    metadata.favorite = True
    metadata.edited = True
    assert sidecar.save() is True
    app_controller.sidecar = sidecar

    path.unlink()
    app_controller._remove_permanently_deleted_metadata([ImageFile(path=path)])

    path.write_bytes(b"unrelated new image")
    reloaded = SidecarManager(image_dir, None)
    assert reloaded.get_metadata(path, create=False) is None
