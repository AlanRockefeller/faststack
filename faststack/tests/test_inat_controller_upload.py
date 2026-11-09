"""Upload bookkeeping in INatUploadController: partial attachments resume on
the same observation, and thumbnails never alias across batches."""

import json
import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402

from faststack.config import config  # noqa: E402
from faststack.inat import controller as ctl  # noqa: E402
from faststack.inat.credentials import INAT_TOKEN, CredentialStore  # noqa: E402
from faststack.inat.http import HttpResponse  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class ScriptedTransport:
    """Answers create-observation and add-photo calls; can fail some photos."""

    def __init__(self, fail_photo_names=(), fail_create=False):
        self.calls = []
        self.fail_photo_names = set(fail_photo_names)
        self.fail_create = fail_create
        self.next_obs = 1000
        self.next_photo = 1
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.calls.append((method, url))
            if url.endswith("/observations") and method == "POST":
                if self.fail_create:
                    return HttpResponse(500, {}, b'{"error":"boom"}')
                self.next_obs += 1
                return HttpResponse(200, {}, json.dumps({"id": self.next_obs}).encode())
            if url.endswith("/observation_photos"):
                name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                if name in self.fail_photo_names:
                    return HttpResponse(422, {}, b'{"error":"bad photo"}')
                self.next_photo += 1
                return HttpResponse(
                    200, {}, json.dumps({"id": self.next_photo}).encode()
                )
            if "taxa/autocomplete" in url:
                return HttpResponse(200, {}, b'{"results": []}')
            return HttpResponse(404, {}, b"{}")


def _photos(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"IMG_{i}.jpg"
        img = Image.new("RGB", (400, 300), (i * 30, 90, 50))
        exif = Image.Exif()
        exif[0x0132] = f"2026:11:01 10:{i:02d}:00"
        img.save(p, "JPEG", exif=exif.tobytes())
        paths.append(str(p))
    return paths


def _make_controller(qapp, transport, tmp_path):
    up = ctl.INatUploadController(config, user_agent="test")
    up._http = ctl.HttpClient("test", transport=transport)
    # Never touch the developer's real keyring from a test.
    up._credentials = CredentialStore(fallback_dir=tmp_path / "secrets")
    up._credentials._keyring_checked = True
    up._credentials._keyring = None
    up._credentials.set(INAT_TOKEN, "a.b.c")
    up._history = ctl.GroupingHistory(None)  # no persistence
    return up


def _pump(qapp, up, seconds=5.0):
    import time

    deadline = time.time() + seconds
    while up.busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_partial_attachment_keeps_observation_and_resumes(qapp, tmp_path):
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    assert up.open(_photos(tmp_path, 3))
    assert up.cardCount == 1
    finished = []
    up.uploadFinished.connect(lambda paths, done: finished.append((paths, done)))

    up.uploadAll()
    _pump(qapp, up)
    card = up.cards.cards[0]
    assert card.obs_id == 1001
    assert card.status == ctl.STATUS_ERROR
    assert "1 of 3" in card.status_text
    assert card.locked  # its observation exists; no regrouping now
    assert not up.canRegroup
    # Only the two attached photos were reported uploaded, and not "all done".
    assert finished and finished[-1][1] is False
    assert sorted(os.path.basename(p) for p in finished[-1][0]) == [
        "IMG_0.jpg",
        "IMG_2.jpg",
    ]
    creates = [u for m, u in transport.calls if u.endswith("/observations")]
    assert len(creates) == 1

    # Retry: the failing photo now succeeds; no second observation is created.
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert card.status == ctl.STATUS_UPLOADED
    creates = [u for m, u in transport.calls if u.endswith("/observations")]
    assert len(creates) == 1
    photo_posts = [u for m, u in transport.calls if u.endswith("/observation_photos")]
    assert len(photo_posts) == 4  # 3 first run (one failed) + 1 retry
    assert finished[-1][1] is True
    assert len(finished[-1][0]) == 3
    up.shutdown()


def test_failed_create_leaves_card_retryable_without_id(qapp, tmp_path):
    transport = ScriptedTransport(fail_create=True)
    up = _make_controller(qapp, transport, tmp_path)
    assert up.open(_photos(tmp_path, 2))
    up.uploadAll()
    _pump(qapp, up)
    card = up.cards.cards[0]
    assert card.obs_id is None
    assert card.status == ctl.STATUS_ERROR
    assert not card.locked
    assert not card.photo_ids
    up.shutdown()


def test_thumbnail_urls_change_per_batch(qapp, tmp_path):
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    assert up.open(_photos(tmp_path, 1))
    first = up.card_value(up.cards.cards[0], "photos")[0]["source"]
    assert up.open(
        _photos(tmp_path / "b", 1) if (tmp_path / "b").mkdir() is None else []
    )
    second = up.card_value(up.cards.cards[0], "photos")[0]["source"]
    assert first != second
    assert first.endswith("/0?w=360") and second.endswith("/0?w=360")
    up.shutdown()
