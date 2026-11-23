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
        self.fail_create_marker = b""  # fail only creates whose body has it
        self.placed = []  # (observation id, file name, position) per photo added
        self.next_obs = 1000
        self.next_photo = 1
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.calls.append((method, url))
            if url.endswith("/observations") and method == "POST":
                if self.fail_create or (
                    self.fail_create_marker and self.fail_create_marker in body
                ):
                    return HttpResponse(500, {}, b'{"error":"boom"}')
                self.next_obs += 1
                return HttpResponse(200, {}, json.dumps({"id": self.next_obs}).encode())
            if url.endswith("/observation_photos"):
                name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                if name in self.fail_photo_names:
                    return HttpResponse(422, {}, b'{"error":"bad photo"}')
                fields = {}
                for field in (b"observation_id", b"position"):
                    marker = b'name="observation_photo[' + field + b']"\r\n\r\n'
                    if marker in body:
                        value = body.split(marker, 1)[1].split(b"\r\n", 1)[0]
                        fields[field] = int(value)
                self.placed.append(
                    (fields.get(b"observation_id"), name, fields.get(b"position"))
                )
                self.next_photo += 1
                return HttpResponse(
                    200, {}, json.dumps({"id": self.next_photo}).encode()
                )
            if "taxa/autocomplete" in url:
                return HttpResponse(200, {}, b'{"results": []}')
            return HttpResponse(404, {}, b"{}")


def _photos(tmp_path, n, first=0):
    paths = []
    for i in range(n):
        p = tmp_path / f"IMG_{first + i}.jpg"
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


def _card_state(up):
    return [
        (
            c.id,
            [p.key for p in c.group.photos],
            up.card_value(c, "chosenName"),
            up.card_value(c, "description"),
        )
        for c in up._model.cards
    ]


def test_reopening_after_another_batch_restores_the_first(qapp, tmp_path):
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _photos(first_dir, 4)
    second = _photos(second_dir, 2)

    assert up.open(first)
    up.splitCard(up._model.cards[0].id, 2)
    card = up._model.cards[0]
    up.setChosenName(card.id, "Amanita muscaria", "")
    up.setDescription(card.id, "under birch")
    up.setLocation(card.id, 10.0, 20.0, 30)
    qapp.processEvents()
    before = _card_state(up)
    assert up.hasManualEdits

    assert up.open(second)
    assert [p.key for p in up._photos] == second
    assert not up.hasManualEdits

    assert up.open(first)
    qapp.processEvents()
    assert _card_state(up) == before
    assert up.hasManualEdits
    restored = up._card(card.id)
    assert restored.manual_location is not None
    up.shutdown()


def test_unreadable_photo_does_not_stop_the_batch_resuming(
    qapp, tmp_path, monkeypatch
):
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 3)
    real = ctl.read_photo_facts

    def flaky(path, order):
        if path.name == "IMG_1.jpg":
            raise OSError("unreadable")
        return real(path, order)

    monkeypatch.setattr(ctl, "read_photo_facts", flaky)
    assert up.open(paths)
    assert len(up._photos) == 2
    assert len(up._model.cards) == 1
    up.splitCard(up._model.cards[0].id, 2)  # a regroup would merge them again
    up.setDescription(up._model.cards[0].id, "kept")
    before = _card_state(up)
    assert len(before) == 2

    assert up.open(paths)
    assert _card_state(up) == before
    up.shutdown()


def test_batch_sharing_uploaded_photos_is_not_restored_stale(qapp, tmp_path):
    """A, then B (part of A) uploaded, then A again: B's photos stay locked."""
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 4)
    first, second = paths, paths[:2]

    assert up.open(first)
    assert up.cardCount == 1
    up.setDescription(up._model.cards[0].id, "the whole find")
    assert up.open(second)
    up.uploadAll()
    _pump(qapp, up)
    assert all(c.status == ctl.STATUS_UPLOADED for c in up._model.cards)

    assert up.open(first)
    uploaded = set(second)
    for card in up._model.cards:
        keys = {p.key for p in card.group.photos}
        if keys & uploaded:
            assert card.locked
    records = {id(p) for p in up._photos}
    for card in up._model.cards:
        for p in card.group.photos:
            assert id(p) in records
    up.shutdown()


def test_stashed_batch_is_not_changed_by_the_next_one(qapp, tmp_path):
    """Two batches with the same photo set share no card objects."""
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 2)
    first = paths
    second = list(reversed(paths))  # same photos, another selection order

    assert up.open(first)
    up.setDescription(up._model.cards[0].id, "first")
    before = _card_state(up)
    assert up.open(second)
    up.setDescription(up._model.cards[0].id, "second")
    assert up.open(first)
    assert _card_state(up) == before
    records = {id(p) for p in up._photos}
    assert all(id(p) in records for c in up._model.cards for p in c.group.photos)
    up.shutdown()


def _identify(up, card, name):
    """Vision answers for ``card``; the answer is handed in, no network."""
    from faststack.inat.mycomap_vision import VisionResult

    result = VisionResult(
        answer_rank="species", answer_name=name, answer_confidence=0.9
    )
    up._begin_job()  # _on_identify_done ends the job identifyCard began
    up._on_identify_done(card.id, card.token, up._identify_run, result, "")
    return result


def test_vision_answers_from_other_batches_reach_a_restored_one(qapp, tmp_path):
    """A (two cards), then B and C (one of A's cards each) identified, then A
    again: A's cards get those answers instead of asking Vision again, and a
    name typed on A's card meanwhile stays."""
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 4)
    assert up.open(paths)
    up.splitCard(up._model.cards[0].id, 2)
    first, second = up._model.cards
    assert [p.key for p in first.group.photos] == paths[:2]
    up.setChosenName(second.id, "Russula", "genus")  # typed, not identified

    assert up.open(paths[:2])
    _identify(up, up._model.cards[0], "Amanita muscaria")
    assert up.open(paths[2:])
    _identify(up, up._model.cards[0], "Boletus edulis")

    assert up.open(paths)
    restored_first = up._card(first.id)
    restored_second = up._card(second.id)
    assert restored_first.status == ctl.STATUS_IDENTIFIED
    assert restored_first.vision.answer_name == "Amanita muscaria"
    assert restored_first.chosen_name == "Amanita muscaria"
    assert restored_second.status == ctl.STATUS_IDENTIFIED
    assert restored_second.vision.answer_name == "Boletus edulis"
    assert restored_second.chosen_name == "Russula"
    up.shutdown()


def test_vision_answers_of_a_batch_outlive_a_visit_to_another(qapp, tmp_path):
    """An answer A got for photos B does not have is still there for A's
    regroups after B was open: a split and re-merge does not ask again."""
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 4)
    assert up.open(paths)
    up.splitCard(up._model.cards[0].id, 2)
    _identify(up, up._model.cards[1], "Boletus edulis")  # photos 2 and 3

    assert up.open(paths[:2])
    assert up.open(paths)
    assert frozenset(paths[2:]) in up._vision_cache
    up.shutdown()


def test_vision_cache_keeps_only_the_newest_answers(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "MAX_VISION_CACHE", 2)
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 3)
    for i, path in enumerate(paths):
        assert up.open([path])
        _identify(up, up._model.cards[0], f"Species {i}")
    assert set(up._vision_cache) == {frozenset([paths[1]]), frozenset([paths[2]])}
    up.shutdown()


def _uploads(transport):
    creates = [u for m, u in transport.calls if u.endswith("/observations")]
    photos = [u for m, u in transport.calls if u.endswith("/observation_photos")]
    return len(creates), len(photos)


def test_full_stash_does_not_drop_the_batch_being_reopened(
    qapp, tmp_path, monkeypatch
):
    """With the stash full, the batch asked for is the oldest one in it:
    putting the current batch aside must not push it out first."""
    monkeypatch.setattr(ctl, "MAX_STASHED_BATCHES", 2)
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    batches = []
    for name in "abc":
        folder = tmp_path / name
        folder.mkdir()
        batches.append(_photos(folder, 1))
    assert up.open(batches[0])
    up.setDescription(up._model.cards[0].id, "first")
    before = _card_state(up)
    assert up.open(batches[1])
    assert up.open(batches[2])  # stash: a, b - full
    assert up.open(batches[0])
    assert _card_state(up) == before
    assert len(up._stashed) == 2
    up.shutdown()


def test_uploads_from_a_batch_put_aside_stay_locked_in_a_rebuilt_one(
    qapp, tmp_path
):
    """A, then B (part of A) uploaded, then C (other photos), then A again:
    B's photos come back as B's observation, not as a new card to upload."""
    transport = ScriptedTransport()
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 4)
    other = tmp_path / "other"
    other.mkdir()
    assert up.open(paths)
    assert up.cardCount == 1
    up.setDescription(up._model.cards[0].id, "the whole find")
    assert up.open(paths[:2])
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 2)
    assert up.open(_photos(other, 1))

    assert up.open(paths)
    for card in up._model.cards:
        if {p.key for p in card.group.photos} & set(paths[:2]):
            assert card.locked
            assert card.obs_id == 1001
    records = {id(p) for p in up._photos}
    assert all(id(p) in records for c in up._model.cards for p in c.group.photos)
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (2, 4)  # one more observation, for photos 2-3
    up.shutdown()


def test_restored_partial_upload_picks_up_photos_attached_from_another_batch(
    qapp, tmp_path
):
    """A attaches only its first photo; B (same photos, other order) attaches
    the rest; back in A the card knows, and nothing goes up twice."""
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 2)
    assert up.open(paths)
    up.uploadAll()
    _pump(qapp, up)
    card = up._model.cards[0]
    assert card.obs_id == 1001 and set(card.photo_ids) == {paths[0]}
    assert up.open(list(reversed(paths)))
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert up._model.cards[0].status == ctl.STATUS_UPLOADED
    assert _uploads(transport) == (1, 3)  # photo 1 failed once, then went up

    assert up.open(paths)
    restored = up._model.cards[0]
    assert restored.obs_id == 1001
    assert set(restored.photo_ids) == set(paths)
    assert restored.status == ctl.STATUS_UPLOADED
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 3)
    up.shutdown()


def test_rebuilt_batch_keeps_the_most_complete_copy_of_an_uploaded_card(
    qapp, tmp_path
):
    """A attaches one photo of two, B (same photos, other order) the other;
    A and B are both put aside, each with its own copy of the card. A new
    batch with those photos and one more keeps B's copy, so photo 1 is not
    sent again."""
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 3)
    other = tmp_path / "other"
    other.mkdir()
    assert up.open(paths[:2])
    up.uploadAll()
    _pump(qapp, up)
    assert up.open(list(reversed(paths[:2])))
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 3)
    assert up.open(_photos(other, 1))

    assert up.open(paths)
    by_photo = {p.key: c for c in up._model.cards for p in c.group.photos}
    card = by_photo[paths[0]]
    assert by_photo[paths[1]] is card
    assert card.status == ctl.STATUS_UPLOADED
    assert set(card.photo_ids) == set(paths[:2])
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (2, 4)  # only photo 2, as its own observation
    up.shutdown()


def test_identification_cancelled_by_a_restore_does_not_land_on_it(
    qapp, tmp_path, monkeypatch
):
    """B (A's photos, other order) shares A's card. An answer to an
    identification B asked for, still on its way when A is opened again,
    must not land on A's card (it has the same id and token) over its name."""
    from faststack.inat.credentials import MYCOMAP_KEY
    from faststack.inat.mycomap_vision import VisionResult

    class Vision:
        def __init__(self, key, http):
            pass

        def identify(self, payload, **kwargs):
            return VisionResult(
                answer_rank="species",
                answer_name="Amanita muscaria",
                answer_confidence=0.9,
            )

    monkeypatch.setattr(ctl, "MycoMapVisionClient", Vision)
    monkeypatch.setattr(ctl.images, "prepare_jpeg", lambda path, edge: b"jpeg")
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    up._credentials.set(MYCOMAP_KEY, "key")
    submitted = []
    monkeypatch.setattr(up._identify_executor, "submit", submitted.append)
    paths = _photos(tmp_path, 2)
    assert up.open(paths)
    card_id = up._model.cards[0].id
    up.setChosenName(card_id, "Russula", "genus")
    assert up.open(list(reversed(paths)))
    assert up._model.cards[0].id == card_id  # the same card, shared
    up.identifyCard(card_id)
    assert len(submitted) == 1
    # Vision answers before A is opened; the answer waits in the event queue.
    worker = threading.Thread(target=submitted[0])
    worker.start()
    worker.join()

    assert up.open(paths)
    restored = up._card(card_id)
    assert restored.chosen_name == "Russula"
    _pump(qapp, up)
    assert restored.chosen_name == "Russula"
    assert restored.vision is None
    assert restored.status == ctl.STATUS_PENDING
    assert not up.busy
    up.shutdown()


def test_rejected_restore_keeps_the_batch_own_uploaded_cards(qapp, tmp_path):
    """A uploads one card (the other fails), B uploads the failed card's
    photos, then A again: A can't come back as it was (its failed card would
    send B's photos twice), and the rebuilt A keeps the card it uploaded."""
    transport = ScriptedTransport()
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 4)
    assert up.open(paths)
    up.splitCard(up._model.cards[0].id, 2)
    first, second = up._model.cards
    assert [p.key for p in first.group.photos] == paths[:2]
    up.setDescription(second.id, "no signal")
    transport.fail_create_marker = b"no signal"
    up.uploadAll()
    _pump(qapp, up)
    assert first.locked and first.obs_id == 1001
    assert not second.locked
    transport.fail_create_marker = b""

    assert up.open(paths[2:])
    up.uploadAll()
    _pump(qapp, up)
    assert up._model.cards[0].obs_id == 1002
    assert _uploads(transport) == (3, 4)

    assert up.open(paths)
    by_photo = {p.key: c for c in up._model.cards for p in c.group.photos}
    assert by_photo[paths[0]].obs_id == by_photo[paths[1]].obs_id == 1001
    assert by_photo[paths[2]].obs_id == by_photo[paths[3]].obs_id == 1002
    assert all(c.locked for c in up._model.cards)
    records = {id(p) for p in up._photos}
    assert all(id(p) in records for c in up._model.cards for p in c.group.photos)
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (3, 4)
    up.shutdown()


def test_photos_of_an_uploaded_card_opened_alone_stay_on_its_observation(
    qapp, tmp_path
):
    """A card of photos 0 and 1 is uploaded; another batch, then photo 0
    alone: it is shown on that observation, not offered as a new one."""
    transport = ScriptedTransport()
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 2)
    other = tmp_path / "other"
    other.mkdir()
    assert up.open(paths)
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 2)
    assert up.open(_photos(other, 1))

    assert up.open(paths[:1])
    card = up._model.cards[0]
    assert [p.key for p in card.group.photos] == paths[:1]
    assert card.locked and card.obs_id == 1001
    assert card.status == ctl.STATUS_UPLOADED
    assert card.group.photos[0] is up._photos[0]
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 2)
    up.shutdown()


def test_photo_left_over_from_a_partial_upload_goes_to_its_observation(
    qapp, tmp_path
):
    """Photo 1 of a card failed to attach. Opened alone it goes to the card's
    observation on a retry; the whole card, rebuilt or restored, then knows
    it is there and sends nothing twice."""
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 3)
    assert up.open(paths[:2])
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (1, 2)

    assert up.open(paths[1:2])
    card = up._model.cards[0]
    assert card.locked and card.obs_id == 1001
    assert card.status == ctl.STATUS_ERROR
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert card.status == ctl.STATUS_UPLOADED
    assert _uploads(transport) == (1, 3)  # to observation 1001, no new one

    assert up.open(paths)  # rebuilt: photos 0 and 1 come back as one card
    by_photo = {p.key: c for c in up._model.cards for p in c.group.photos}
    assert by_photo[paths[0]] is by_photo[paths[1]]
    assert by_photo[paths[0]].status == ctl.STATUS_UPLOADED
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (2, 4)  # only photo 2, as its own observation

    assert up.open(paths[:2])  # restored as it was left, but caught up
    card = up._model.cards[0]
    assert card.status == ctl.STATUS_UPLOADED
    assert set(card.photo_ids) == set(paths[:2])
    up.uploadAll()
    _pump(qapp, up)
    assert _uploads(transport) == (2, 4)
    up.shutdown()


def test_retry_of_a_restored_batch_keeps_another_batch_grouping_record(
    qapp, tmp_path
):
    """A fails, B (same folder) is uploaded, A is retried: the history keeps
    B's grouping, and a second retry of A does not add another record."""
    transport = ScriptedTransport(fail_create=True)
    up = _make_controller(qapp, transport, tmp_path)
    first = _photos(tmp_path, 2)
    second = _photos(tmp_path, 3, first=10)
    assert up.open(first)
    up.uploadAll()
    _pump(qapp, up)
    assert [b.photos for b in up._history.batches] == [2]
    transport.fail_create = False
    assert up.open(second)
    up.uploadAll()
    _pump(qapp, up)
    assert [b.photos for b in up._history.batches] == [2, 3]

    transport.fail_create = True
    assert up.open(first)
    up.uploadAll()
    _pump(qapp, up)
    assert [b.photos for b in up._history.batches] == [2, 3, 2]
    transport.fail_create = False
    up.uploadAll()  # a retry right after: the same decision, replaced
    _pump(qapp, up)
    assert all(c.status == ctl.STATUS_UPLOADED for c in up._model.cards)
    assert [b.photos for b in up._history.batches] == [2, 3, 2]
    up.shutdown()


def test_cards_for_parts_of_one_observation_get_their_own_ids(
    qapp, tmp_path, monkeypatch
):
    """A card of photos 0-2 is uploaded with photo 1 failing; photo 0 alone,
    then photo 1 alone, each show a card for that observation. Once the
    whole card's batch is gone from the stash, opening photos 0 and 1 shows
    both of those cards: each has its own id, so the retry's answer reaches
    the card that sent it."""
    monkeypatch.setattr(ctl, "MAX_STASHED_BATCHES", 2)
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 3)
    assert up.open(paths)
    up.uploadAll()
    _pump(qapp, up)
    whole = up._model.cards[0]
    assert whole.obs_id == 1001 and set(whole.photo_ids) == {paths[0], paths[2]}
    assert up.open(paths[:1])
    assert up.open(paths[1:2])

    assert up.open(paths[:2])  # the whole card's batch drops out of the stash
    assert tuple(paths) not in up._stashed
    cards = up._model.cards
    assert len(cards) == 2
    assert len({c.id for c in cards}) == 2
    assert all(c.obs_id == 1001 for c in cards)
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert all(c.status == ctl.STATUS_UPLOADED for c in cards)
    assert transport.placed[-1][:2] == (1001, "IMG_1.jpg")
    assert _uploads(transport) == (1, 4)
    up.shutdown()


def test_retry_from_a_part_card_keeps_the_photo_place(qapp, tmp_path):
    """Photo 1 of a card failed; retried from a card holding only it, it
    goes where the whole card put it, after photo 0, not first."""
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 2)
    assert up.open(paths)
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed == [(1001, "IMG_0.jpg", 0)]

    assert up.open(paths[1:])
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed[-1] == (1001, "IMG_1.jpg", 1)
    up.shutdown()


def test_retry_from_a_part_card_keeps_the_place_among_existing_photos(
    qapp, tmp_path
):
    """The same for photos added to an observation already on iNaturalist:
    after its own photo and photo 0, as the whole card had them."""
    from faststack.inat.matching import ExistingObservation, ExistingPhoto

    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 2)
    assert up.open(paths)
    up._model.cards[0].attach_to = ExistingObservation(
        id=500, photos=(ExistingPhoto(op_id=0, position=0, url=""),)
    )
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed == [(500, "IMG_0.jpg", 1)]
    assert _uploads(transport) == (0, 2)

    assert up.open(paths[1:])
    card = up._model.cards[0]
    assert card.obs_id == 500 and card.attach_to is not None
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert card.status == ctl.STATUS_UPLOADED
    assert transport.placed[-1] == (500, "IMG_1.jpg", 2)
    assert _uploads(transport) == (0, 3)
    up.shutdown()


def test_parts_of_one_card_added_to_an_existing_observation_keep_places(
    qapp, tmp_path, monkeypatch
):
    """Two parts of one card for an observation already on iNaturalist,
    shown together: retrying the second puts its photo where the whole card
    had it, not after everything the first part's whole card holds."""
    from faststack.inat.matching import ExistingObservation, ExistingPhoto

    monkeypatch.setattr(ctl, "MAX_STASHED_BATCHES", 2)
    transport = ScriptedTransport(fail_photo_names={"IMG_1.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    paths = _photos(tmp_path, 3)
    assert up.open(paths)
    up._model.cards[0].attach_to = ExistingObservation(
        id=500, photos=(ExistingPhoto(op_id=0, position=0, url=""),)
    )
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed == [(500, "IMG_0.jpg", 1), (500, "IMG_2.jpg", 3)]
    assert up.open(paths[:1])
    assert up.open(paths[1:2])
    assert up.open(paths[:2])
    assert len(up._model.cards) == 2
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert all(c.status == ctl.STATUS_UPLOADED for c in up._model.cards)
    assert transport.placed[-1] == (500, "IMG_1.jpg", 2)
    up.shutdown()


def test_cards_shown_together_never_share_an_id(qapp, tmp_path):
    """Copies of one card from different batches can share an id while
    holding different photos (a card split differently in each). Shown
    together, each gets an id of its own - uploaded or not."""
    up = _make_controller(qapp, ScriptedTransport(), tmp_path)
    paths = _photos(tmp_path, 3)
    assert up.open(paths[:1])
    up.uploadAll()
    _pump(qapp, up)
    uploaded_id = up._model.cards[0].id
    assert up.open(paths[1:2])
    up.uploadAll()
    _pump(qapp, up)
    up._model.cards[0].id = uploaded_id  # two uploaded cards, one id
    assert up.open(paths[2:])
    up._model.cards[0].id = uploaded_id  # and a card not uploaded yet

    assert up.open(paths)
    ids = [c.id for c in up._model.cards]
    assert len(ids) == 3 and len(set(ids)) == 3
    up.shutdown()


def test_part_of_a_second_card_on_an_existing_observation_keeps_its_place(
    qapp, tmp_path
):
    """Two cards add photos to one observation already on iNaturalist: the
    second card's go after the first card's. Its last photo failed; retried
    from a card holding only it, it goes where it was sent, not among the
    first card's photos."""
    from faststack.inat.matching import ExistingObservation, ExistingPhoto

    paths = []
    for i, when in enumerate(["10:00:00", "10:01:00", "15:00:00", "15:01:00"]):
        p = tmp_path / f"IMG_{i}.jpg"
        exif = Image.Exif()
        exif[0x0132] = f"2026:11:01 {when}"
        Image.new("RGB", (400, 300), (i * 30, 90, 50)).save(
            p, "JPEG", exif=exif.tobytes()
        )
        paths.append(str(p))
    transport = ScriptedTransport(fail_photo_names={"IMG_3.jpg"})
    up = _make_controller(qapp, transport, tmp_path)
    assert up.open(paths)
    assert len(up._model.cards) == 2
    observation = ExistingObservation(
        id=500, photos=(ExistingPhoto(op_id=0, position=0, url=""),)
    )
    for card in up._model.cards:
        card.attach_to = observation
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed == [
        (500, "IMG_0.jpg", 1),
        (500, "IMG_1.jpg", 2),
        (500, "IMG_2.jpg", 3),
    ]

    assert up.open(paths[3:])
    transport.fail_photo_names.clear()
    up.uploadAll()
    _pump(qapp, up)
    assert transport.placed[-1] == (500, "IMG_3.jpg", 4)
    up.shutdown()
