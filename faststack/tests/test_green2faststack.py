import importlib.util
import json
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "lightroom-catalog-import"
    / "green2faststack.py"
)
MODULE_NAME = "green2faststack_test_module"

spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
assert spec is not None
assert spec.loader is not None
green2faststack = importlib.util.module_from_spec(spec)
sys.modules[MODULE_NAME] = green2faststack
spec.loader.exec_module(green2faststack)


def test_missing_json_is_created_even_when_no_green_stems_match(tmp_path):
    target_dir = tmp_path / "photos"
    target_dir.mkdir()
    paths_file = tmp_path / "green-paths.txt"
    paths_file.write_text("C:/elsewhere/IMG_0001.jpg\n", encoding="utf-8")

    summary = green2faststack.update_faststack_json(
        paths_file=str(paths_file),
        json_path_str=str(target_dir),
        uploaded_date=green2faststack.DEFAULT_UPLOADED_DATE,
        dry_run=False,
        logger=green2faststack.Logger(),
    )

    json_path = target_dir / "faststack.json"
    assert summary.green_in_this_dir == 0
    assert summary.json_created is True
    assert summary.json_written is True
    assert summary.backup_path is None
    assert json_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8")) == (
        green2faststack.EMPTY_FASTSTACK_JSON
    )
