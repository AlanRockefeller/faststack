"""Qt controller behind the iNaturalist upload dialog.

Owns the observation cards (a :class:`CardModel` the QML Repeater reads),
runs identification and upload on worker threads, and exposes the settings
the dialog edits (credentials, grouping thresholds, tile cache).

Everything that touches the model runs on the GUI thread: workers emit the
private ``_*Done`` signals, which Qt queues back to the controller's thread.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from faststack.inat import images
from faststack.inat.credentials import INAT_TOKEN, MYCOMAP_KEY, CredentialStore
from faststack.inat.grouping import (
    Location,
    ObservationGroup,
    PhotoRecord,
    group_sort_key,
    haversine_m,
    initial_grouping,
    merge_groups,
    move_photos,
    split_group,
)
from faststack.inat.http import ApiError, HttpClient
from faststack.inat.inat_client import (
    API_TOKEN_URL,
    OBSERVATION_URL,
    INatClient,
    MapObservation,
    ObservationDraft,
    Taxon,
    inat_taxon_name,
    rank_suggestions,
)
from faststack.inat.inat_client import (
    TOKEN_REJECTED,
    describe_token,
    is_jwt,
    normalise_token,
    token_expires_at,
)
from faststack.inat.matching import (
    OFFSETS_FILE,
    CardFacts,
    ClockOffsetStore,
    ExistingObservation,
    Match,
    OffsetGuess,
    describe_offset,
    detect_offsets,
    fetch_window,
    match_card,
    offset_for_pair,
)
from faststack.inat.inat_client import describe_error as describe_inat_error
from faststack.inat.mycomap_vision import (
    MycoMapVisionClient,
    VisionResult,
    client_ref_for,
)
from faststack.inat.mycomap_vision import describe_error as describe_vision_error
from faststack.inat.photo_facts import read_photo_facts
from faststack.inat.tiles import TileCache, TileFetcher
from faststack.inat.tuning import HISTORY_FILE, GroupingHistory, pairs_from_groups
from faststack.logging_setup import get_app_data_dir
from faststack.util.executors import create_daemon_threadpool_executor

log = logging.getLogger(__name__)

CONFIG_SECTION = "inat"
INAT_CONFIG_DEFAULTS = {
    "time_gap_minutes": "10",
    "distance_meters": "100",
    "spread_warning_meters": "100",
    "tile_cache_dir": "",
    "tile_cache_mb": "0",  # 0 = size from free disk space
    "geoprivacy": "open",
    "inat_login": "",
}

STATUS_PENDING = "pending"
STATUS_IDENTIFYING = "identifying"
STATUS_IDENTIFIED = "identified"
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"
STATUS_ERROR = "error"

# MycoMap Vision limits each key to a number of photos per minute and refuses
# calls while its queue is full. Both replies say when to come back, so an
# identification waits and retries instead of failing the card - up to this
# long in total, after which the card shows the error.
IDENTIFY_MAX_WAIT_SECONDS = 180.0
_RETRY_DELAY_RANGE = (2.0, 60.0)


def retry_delay_seconds(exc: ApiError) -> Optional[float]:
    """How long to wait before repeating a refused Vision call; None = don't."""
    if exc.status == 429 and exc.code != "quota_exceeded":
        default = 15.0
    elif exc.status == 503:
        default = 10.0
    else:
        return None
    delay = float(exc.retry_after) if exc.retry_after else default
    low, high = _RETRY_DELAY_RANGE
    return max(low, min(high, delay))


DEFAULT_MANUAL_ACCURACY_M = 25
# The user's own iNaturalist observations shown on the map picker: how many
# one press of the button asks for, and how many thumbnails are kept.
MAP_OBSERVATIONS_PER_FETCH = 200
MAP_THUMB_CACHE_ENTRIES = 3000
# Map marker thumbnails download in parallel on their own threads; markers
# get their image only once it is here (see myObservations).
MAP_THUMB_WORKERS = 4
MAP_THUMB_TIMEOUT_S = 20
MAX_ACCURACY_M = 1_000_000
# iNaturalist API tokens last 24 hours. In their last minutes the dialog
# warns, and Upload asks first: a run that outlives the token fails partway.
TOKEN_WARN_SECONDS = 15 * 60
TOKEN_TICK_MS = 30_000


@dataclass
class Card:
    id: int
    group: ObservationGroup
    manual_location: Optional[tuple[float, float]] = None
    manual_accuracy_m: Optional[int] = None  # the user's own estimate for a map point
    status: str = STATUS_PENDING
    status_text: str = ""
    vision: Optional[VisionResult] = None
    chosen_name: str = ""
    chosen_rank: str = ""
    taxon: Optional[Taxon] = None
    geoprivacy: str = "open"
    description: str = ""
    obs_id: Optional[int] = None
    selected: bool = False
    token: int = 0  # bumps whenever the photo set changes; stale results are dropped
    identify_pending: bool = False
    upload_error: str = ""
    photo_ids: dict[str, int] = field(default_factory=dict)
    # Observations already on iNaturalist these photos may belong to.
    matches: list[Match] = field(default_factory=list)
    # The user chose one: upload adds the photos to it instead of creating
    # an observation.
    attach_to: Optional[ExistingObservation] = None
    # Where the new photos go among the observation's own: "inat:<index>"
    # for its photos, photo keys for this card's. None = after them.
    merged_order: Optional[list[str]] = None

    @property
    def locked(self) -> bool:
        """Photos can't move once an observation exists for them on iNat."""
        return (
            self.status in (STATUS_UPLOADING, STATUS_UPLOADED)
            or self.obs_id is not None
        )

    def location(self, spread_warning_m: float) -> Optional[Location]:
        if self.manual_location is not None:
            lat, lon = self.manual_location
            return Location(lat=lat, lon=lon, source="manual")
        return self.group.exif_location(spread_warning_m)

    def accuracy_m(self, loc: Optional[Location]) -> Optional[int]:
        """Positional accuracy to send to iNaturalist for ``loc``."""
        if loc is None:
            return None
        if loc.source == "exif":
            return int(max(10.0, loc.spread_m))
        return self.manual_accuracy_m or DEFAULT_MANUAL_ACCURACY_M


class CardModel(QAbstractListModel):
    ROLE_NAMES = [
        "cardId",
        "photos",
        "photoCount",
        "dateText",
        "hasLocation",
        "lat",
        "lon",
        "locationSource",
        "accuracyMeters",
        "gpsCount",
        "spreadMeters",
        "spreadWarning",
        "uploadError",
        "status",
        "statusText",
        "suggestedName",
        "suggestedRank",
        "choices",
        "chosenName",
        "chosenRank",
        "taxonLabel",
        "hints",
        "gateMessage",
        "geoprivacy",
        "description",
        "obsUrl",
        "selected",
        "canSplit",
        "locked",
        "visionWarning",
        "canSplitByVision",
        "existingMatches",
        "attachedObs",
    ]

    def __init__(self, controller: "INatUploadController"):
        super().__init__(controller)
        self._controller = controller
        self._cards: list[Card] = []
        self._roles = {
            Qt.ItemDataRole.UserRole + i: name.encode()
            for i, name in enumerate(self.ROLE_NAMES)
        }
        self._role_by_name = {
            name: Qt.ItemDataRole.UserRole + i for i, name in enumerate(self.ROLE_NAMES)
        }

    # Qt model API --------------------------------------------------------

    def roleNames(self):  # noqa: N802
        return self._roles

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self._cards)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._cards):
            return None
        name = self._roles.get(role)
        if name is None:
            return None
        return self._controller.card_value(self._cards[index.row()], name.decode())

    # Helpers -------------------------------------------------------------

    @property
    def cards(self) -> list[Card]:
        return self._cards

    def reset(self, cards: list[Card]) -> None:
        self.beginResetModel()
        self._cards = cards
        self.endResetModel()

    def refresh(self, card: Card, *names: str) -> None:
        try:
            row = self._cards.index(card)
        except ValueError:
            return
        idx = self.index(row, 0)
        roles = [self._role_by_name[n] for n in names if n in self._role_by_name]
        self.dataChanged.emit(idx, idx, roles)

    def sync(self, cards: list[Card]) -> None:
        """Make the list ``cards`` by removing and inserting rows only.

        Unlike :meth:`reset`, the cards that stay keep their place in the
        view (and the view its scroll position). Cards that stay must be in
        the order they already have.
        """
        keep = {id(c) for c in cards}
        for row in range(len(self._cards) - 1, -1, -1):
            if id(self._cards[row]) not in keep:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._cards[row]
                self.endRemoveRows()
        for row, card in enumerate(cards):
            if row < len(self._cards) and self._cards[row] is card:
                continue
            self.beginInsertRows(QModelIndex(), row, row)
            self._cards.insert(row, card)
            self.endInsertRows()

    def refresh_all(self, card: Card) -> None:
        """Every value of this card may have changed."""
        try:
            row = self._cards.index(card)
        except ValueError:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [])

    def replace(self, old: Card, new_cards: list[Card]) -> None:
        try:
            row = self._cards.index(old)
        except ValueError:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._cards[row]
        self.endRemoveRows()
        if new_cards:
            self.beginInsertRows(QModelIndex(), row, row + len(new_cards) - 1)
            self._cards[row:row] = new_cards
            self.endInsertRows()

    def remove(self, card: Card) -> None:
        self.replace(card, [])

    def append(self, card: Card) -> None:
        row = len(self._cards)
        self.beginInsertRows(QModelIndex(), row, row)
        self._cards.append(card)
        self.endInsertRows()


class INatUploadController(QObject):
    """QML-facing object (context property ``inatUpload``)."""

    cardsChanged = Signal()
    busyChanged = Signal()
    statusTextChanged = Signal()
    usageTextChanged = Signal()
    settingsChanged = Signal()
    uploadProgressChanged = Signal()
    thresholdsChanged = Signal()
    uploadFinished = Signal(list, bool)  # paths that went up, all cards done
    showMessage = Signal(str)
    tokenClockChanged = Signal()  # the token's time left, every half minute
    tokenRejected = Signal(str)  # the saved token no longer works (dialog open)
    uploadCardFinished = Signal(int)  # card id; the dialog scrolls it into view
    # Observations uploaded, then one {cardId, number, name, message} per failure.
    uploadFailed = Signal(int, list)

    _identifyDone = Signal(int, int, object, str)  # card id, token, result, error
    _identifyProgress = Signal(int, int, str)  # card id, token, status line
    # card id, token, obs id, keys of photos attached this run, error, and
    # why the photos of an existing observation could not be put in order
    _uploadCardDone = Signal(int, int, object, object, str, str)
    _uploadRunFinished = Signal()
    _usageDone = Signal(str)
    _verifyDone = Signal(str, str)  # login, error
    _tokenCheckDone = Signal(int, str, str, bool)  # seq, login, error, rejected
    _taxonLookupDone = Signal(object, str, object)  # taxon, error, (name, rank)
    # Name-field autocomplete: card id, the text it answers, rows for the list
    taxonSuggestions = Signal(int, str, list)
    _suggestDone = Signal(int, int, str, object)  # card id, seq, text, taxa
    # The user's observations already on iNaturalist, shown on the map picker.
    myObservationsChanged = Signal()
    _myObservationsDone = Signal(int, object, int, str)  # seq, rows, total, error
    _thumbFetched = Signal(int, int)  # seq, observation id
    # Observations already on iNaturalist on the batch's dates, and the
    # camera clock offsets matching them found.
    existingChanged = Signal()
    _existingDone = Signal(int, object, str)  # seq, observations, error

    def __init__(self, config, parent: Optional[QObject] = None, user_agent: str = ""):
        super().__init__(parent)
        self._config = config
        self._credentials = CredentialStore()
        self._model = CardModel(self)
        self._photos: list[PhotoRecord] = []
        self._next_card_id = 1
        self._busy_jobs = 0
        self._status_text = ""
        self._usage_text = ""
        self._vision_requests = 0  # identify calls sent this session
        self._upload_done = 0
        self._upload_total = 0
        self._upload_results: list[bool] = []  # this run, in the order finished
        self._cancel_upload = threading.Event()
        self._user_agent = (
            user_agent or "FastStack (+https://github.com/AlanRockefeller/faststack)"
        )
        self._http = HttpClient(self._user_agent)
        self._executor = create_daemon_threadpool_executor(2, "inat-upload")
        # Identifications run one at a time on their own thread: Vision meters
        # photos per minute, and a call waiting out that limit must not hold up
        # an upload or a taxon lookup on the shared pool.
        self._identify_executor = create_daemon_threadpool_executor(1, "inat-identify")
        self._cancel_identify = threading.Event()
        # Name autocomplete has its own thread too, so typing is answered
        # while an upload holds the shared pool.
        self._suggest_executor = create_daemon_threadpool_executor(1, "inat-suggest")
        self._suggest_seq = 0  # only the newest request is worth sending
        self._suggest_cache: dict[str, list[Taxon]] = {}
        self._suggested: dict[int, Taxon] = {}  # taxon id -> rows offered so far
        self._upload_active = False
        # An upload run ended on a rejected token; saving a token that
        # verifies picks the upload up again without another press.
        self._token_failed_run = False
        self._resume_after_verify = False
        self._grouping_recorded = False
        # iNat taxon for a (name, rank) the user or Vision chose. Regrouping
        # re-creates cards with the same names; asking iNaturalist again for
        # each one on every slider move would hit its rate limit.
        self._taxon_cache: dict[tuple[str, str], Optional[Taxon]] = {}
        # Cards waiting on a lookup that is already in flight, per (name, rank).
        self._taxon_waiting: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._history = GroupingHistory(get_app_data_dir() / HISTORY_FILE)
        self._vision_cache: dict[frozenset, VisionResult] = {}
        self._time_gap_s = float(self.timeGapMinutes * 60)
        self._distance_m = float(self.distanceMeters)
        self._thresholds_source = "default"
        self._thresholds_note = ""
        self._folder = ""
        self._manual_edits = False
        self._batch_generation = 0
        # Set by the app to the photo provider's cache-clearing function.
        self.release_photo_cache: Optional[Callable[[], None]] = None
        self._tile_cache: Optional[TileCache] = None
        self._tile_fetcher: Optional[TileFetcher] = None
        self._tile_lock = threading.Lock()
        self._inat_login = config.get(CONFIG_SECTION, "inat_login", "") or ""
        self._verify_error = ""
        self._verify_seq = 0  # a newer check or verify outdates an older answer
        self._uploaded_paths: list[str] = []
        self._my_observations: dict[int, MapObservation] = {}
        self._my_observations_busy = False
        self._my_observations_status = ""
        self._my_observations_seq = 0  # a Clear drops the answer still on its way
        # (observation id, size) -> image bytes, read by Qt's image threads.
        self._thumb_cache: dict[tuple[int, str], bytes] = {}
        self._thumb_lock = threading.Lock()
        # Marker thumbnails are fetched here, not by the image provider: Qt
        # runs every provider of the engine on one thread, so a provider
        # downloading 200 of them in a row would hold up the map tiles.
        self._thumb_executor = create_daemon_threadpool_executor(
            MAP_THUMB_WORKERS, "inat-obs-thumb"
        )
        self._thumb_pending: set[int] = set()  # GUI thread only
        # Markers are rebuilt once per burst of finished downloads.
        self._thumb_refresh = QTimer(self)
        self._thumb_refresh.setSingleShot(True)
        self._thumb_refresh.setInterval(250)
        self._thumb_refresh.timeout.connect(self.myObservationsChanged)
        # Existing observations the cards may belong to (see matching.py).
        self._existing: list[ExistingObservation] = []
        self._existing_by_id: dict[int, ExistingObservation] = {}
        self._existing_seq = 0
        self._existing_busy = False
        self._existing_fetched = False  # for this batch
        self._existing_status = ""
        self._offset_store = ClockOffsetStore(get_app_data_dir() / OFFSETS_FILE)
        self._clock: dict[str, OffsetGuess] = {}  # camera -> offset in use
        self._clock_saved: dict[str, OffsetGuess] = {}  # from the store, per batch
        self._clock_decisions: dict[str, OffsetGuess] = {}  # the user's, this batch
        self._camera_days: dict[str, Any] = {}  # camera -> the batch's middle date
        self._match_signature: Any = None
        self._clock_version = 0  # bumps on every clock decision
        # Offset sets this batch was already regrouped for. Regrouping
        # changes the cards, the cards feed offset detection, so each set
        # regroups once at most: no back and forth between two answers.
        self._regrouped_for_clock: set[frozenset] = set()
        # Matches the user said are wrong: observation id -> keys of the
        # photos it was offered for. A card holding any of them is not
        # offered it again (also after a split or a regroup).
        self._dismissed: dict[int, set[str]] = {}
        self._dismiss_version = 0
        # Matching reruns once per burst of card changes, after they settle.
        self._rematch_timer = QTimer(self)
        self._rematch_timer.setSingleShot(True)
        self._rematch_timer.setInterval(0)
        self._rematch_timer.timeout.connect(self._rematch)
        self.cardsChanged.connect(self._rematch_timer.start)

        self._identifyDone.connect(self._on_identify_done)
        self._identifyProgress.connect(self._on_identify_progress)
        self._uploadCardDone.connect(self._on_upload_card_done)
        self._uploadRunFinished.connect(self._on_upload_run_finished)
        self._usageDone.connect(self._on_usage_done)
        self._verifyDone.connect(self._on_verify_done)
        self._tokenCheckDone.connect(self._on_token_check_done)
        self._taxonLookupDone.connect(self._on_taxon_lookup_done)
        self._suggestDone.connect(self._on_suggest_done)
        self._myObservationsDone.connect(self._on_my_observations_done)
        self._thumbFetched.connect(self._on_thumb_fetched)
        self._existingDone.connect(self._on_existing_done)
        self._token_warned_for = ""  # the token the "expires soon" toast was for
        self._token_timer = QTimer(self)
        self._token_timer.setInterval(TOKEN_TICK_MS)
        self._token_timer.timeout.connect(self._on_token_tick)
        self._token_timer.start()
        self.settingsChanged.connect(self.tokenClockChanged)

    # ------------------------------------------------------------------
    # Providers / lifecycle

    @Slot()
    def releasePhotoCache(self) -> None:  # noqa: N802
        """The dialog closed: free the full-screen viewer's decoded photos."""
        if self.release_photo_cache is not None:
            self.release_photo_cache()

    def photo_path(self, index: int) -> Optional[Path]:
        if 0 <= index < len(self._photos):
            return self._photos[index].path
        return None

    def tile_fetcher(self) -> Optional[TileFetcher]:
        with self._tile_lock:
            if self._tile_fetcher is None:
                try:
                    mb = self._config.getint(CONFIG_SECTION, "tile_cache_mb", 0)
                    budget = mb * 1024 * 1024 if mb and mb > 0 else None
                    cache = TileCache(self.tile_cache_path(), budget_bytes=budget)
                    self._tile_cache = cache
                    self._tile_fetcher = TileFetcher(cache, self._user_agent)
                except Exception as exc:
                    log.warning("Tile cache unavailable: %s", exc)
                    return None
            return self._tile_fetcher

    def tile_cache_path(self) -> Path:
        configured = (
            self._config.get(CONFIG_SECTION, "tile_cache_dir", "") or ""
        ).strip()
        if configured:
            return Path(configured).expanduser()
        return get_app_data_dir() / "map_tiles"

    def shutdown(self) -> None:
        self._cancel_upload.set()
        self._cancel_identify.set()
        for executor in (
            self._executor,
            self._identify_executor,
            self._suggest_executor,
            self._thumb_executor,
        ):
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass
        with self._tile_lock:
            if self._tile_cache is not None:
                self._tile_cache.close()
                self._tile_cache = None
                self._tile_fetcher = None

    # ------------------------------------------------------------------
    # QML properties

    @Property(QObject, constant=True)
    def cards(self):
        return self._model

    @Property(int, notify=cardsChanged)
    def cardCount(self) -> int:
        return len(self._model.cards)

    @Property(int, notify=cardsChanged)
    def photoCount(self) -> int:
        return len(self._photos)

    @Property(int, notify=cardsChanged)
    def selectedCount(self) -> int:
        return sum(1 for c in self._model.cards if c.selected)

    @Property(int, notify=cardsChanged)
    def cardsWithoutLocation(self) -> int:
        warn = self.spreadWarningMeters
        return sum(
            1
            for c in self._model.cards
            if c.location(warn) is None
            and c.status != STATUS_UPLOADED
            and c.attach_to is None  # the observation has its own location
        )

    @Slot(result=list)
    def cardsWithoutLocationList(self) -> list:  # noqa: N802
        """{cardId, number, name, photoCount} for each card that would go up
        with no location, so the dialog can name them and jump to them."""
        warn = self.spreadWarningMeters
        return [
            {
                "cardId": c.id,
                "number": number,
                "name": c.chosen_name,
                "photoCount": len(c.group.photos),
            }
            for number, c in enumerate(self._model.cards, start=1)
            if c.location(warn) is None
            and c.status != STATUS_UPLOADED
            and c.attach_to is None
        ]

    @Property(int, notify=cardsChanged)
    def cardsToUpload(self) -> int:
        return sum(1 for c in self._model.cards if c.status != STATUS_UPLOADED)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy_jobs > 0

    @Property(bool, notify=busyChanged)
    def uploading(self) -> bool:
        """An upload run is in flight (the one job the dialog must not close on)."""
        return self._upload_active

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=usageTextChanged)
    def usageText(self) -> str:
        return self._usage_text

    @Property(int, notify=usageTextChanged)
    def visionRequests(self) -> int:  # noqa: N802
        """MycoMap Vision identify calls sent since FastStack started."""
        return self._vision_requests

    @Property(str, notify=usageTextChanged)
    def visionCounterText(self) -> str:  # noqa: N802
        """Lower-right counter: calls this session plus the account's period use."""
        parts = [f"Vision requests: {self._vision_requests} this session"]
        if self._usage_text and self._usage_text.startswith("MycoMap Vision: "):
            # MycoMap's own count for the whole API key, not for this batch.
            parts.append(
                "your key in total: " + self._usage_text[len("MycoMap Vision: ") :]
            )
        return " · ".join(parts)

    @Property(int, notify=uploadProgressChanged)
    def uploadDone(self) -> int:
        return self._upload_done

    @Property(int, notify=uploadProgressChanged)
    def uploadTotal(self) -> int:
        return self._upload_total

    @Property(list, notify=uploadProgressChanged)
    def uploadResults(self) -> list:  # noqa: N802
        """One bool per observation finished in this run: did it go up whole?"""
        return list(self._upload_results)

    @Property(str, notify=settingsChanged)
    def inatLogin(self) -> str:
        return self._inat_login

    @Property(str, notify=settingsChanged)
    def verifyError(self) -> str:
        return self._verify_error

    @Property(bool, notify=settingsChanged)
    def hasInatToken(self) -> bool:
        return bool(self._credentials.get(INAT_TOKEN))

    @Property(bool, notify=settingsChanged)
    def hasVisionKey(self) -> bool:
        return bool(self._credentials.get(MYCOMAP_KEY))

    @Property(str, constant=True)
    def inatTokenUrl(self) -> str:
        return API_TOKEN_URL

    @Property(str, constant=True)
    def keyringBackend(self) -> str:
        return self._credentials.backend_name

    @Property(int, notify=settingsChanged)
    def timeGapMinutes(self) -> int:
        return self._config.getint(CONFIG_SECTION, "time_gap_minutes", 10)

    @Property(int, notify=settingsChanged)
    def distanceMeters(self) -> int:
        return self._config.getint(CONFIG_SECTION, "distance_meters", 100)

    @Property(int, notify=settingsChanged)
    def spreadWarningMeters(self) -> int:
        return self._config.getint(CONFIG_SECTION, "spread_warning_meters", 100)

    @Property(str, notify=settingsChanged)
    def defaultGeoprivacy(self) -> str:
        return self._config.get(CONFIG_SECTION, "geoprivacy", "open") or "open"

    @Property(str, notify=settingsChanged)
    def tileCacheDir(self) -> str:
        return str(self.tile_cache_path())

    @Property(int, notify=settingsChanged)
    def tileCacheMb(self) -> int:
        return self._config.getint(CONFIG_SECTION, "tile_cache_mb", 0)

    @Property(str, notify=settingsChanged)
    def tileCacheStats(self) -> str:
        fetcher = self.tile_fetcher()
        if fetcher is None:
            return "Tile cache unavailable"
        stats = fetcher.cache.stats()
        used_mb = stats.bytes / (1024 * 1024)
        budget_mb = stats.budget_bytes / (1024 * 1024)
        auto = " (auto, from free disk space)" if self.tileCacheMb <= 0 else ""
        return f"{stats.tiles} tiles, {used_mb:.1f} MB of {budget_mb:.0f} MB{auto}"

    # ------------------------------------------------------------------
    # Settings slots

    # ------------------------------------------------------------------
    # Token lifetime

    def _token_seconds_left(self) -> int:
        """Seconds until the saved token expires; 0 once it has, -1 when
        there is none or it does not say."""
        token = self._credentials.get(INAT_TOKEN)
        if not token:
            return -1
        expires = token_expires_at(token)
        if expires is None:
            return -1
        return max(0, int(expires - time.time()))

    @Property(int, notify=tokenClockChanged)
    def tokenSecondsLeft(self) -> int:  # noqa: N802
        return self._token_seconds_left()

    @Property(int, constant=True)
    def tokenWarnSeconds(self) -> int:  # noqa: N802
        return TOKEN_WARN_SECONDS

    @Property(str, notify=tokenClockChanged)
    def tokenExpiryText(self) -> str:  # noqa: N802
        left = self._token_seconds_left()
        if left < 0:
            return ""
        if left == 0:
            return "token expired"
        hours, rest = divmod(left, 3600)
        minutes = rest // 60
        if hours:
            return f"token good for {hours} h {minutes:02d} min"
        return f"token expires in {max(1, minutes)} min"

    @Slot()
    def _on_token_tick(self) -> None:
        self.tokenClockChanged.emit()
        token = self._credentials.get(INAT_TOKEN)
        left = self._token_seconds_left()
        if 0 < left <= TOKEN_WARN_SECONDS and token != self._token_warned_for:
            self._token_warned_for = token
            self.showMessage.emit(
                f"Your iNaturalist token expires in {max(1, left // 60)} min. "
                "Get a fresh one (Settings) before a long upload."
            )

    @staticmethod
    def _pasted_token_problem(token: str) -> str:
        """Why a pasted token is not worth saving; "" when it is."""
        if not is_jwt(token):
            return (
                "That is not an iNaturalist API token. On the token page, copy "
                "the long code that starts with eyJ (between the quotes)."
            )
        expires = token_expires_at(token)
        if expires is not None and expires <= time.time():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires))
            return (
                f"That token already expired at {when}: it is an old copy. "
                "Reload the token page (each visit makes a new one) and copy again."
            )
        return ""

    @Slot(str, str)
    def saveCredentials(self, inat_token: str, vision_key: str) -> None:  # noqa: N802
        saved = True
        if inat_token.strip():
            token = normalise_token(inat_token)
            problem = self._pasted_token_problem(token)
            if problem:
                # Not saved: the token that is there (if any) stays.
                self._verify_error = problem
                self.showMessage.emit(problem)
                inat_token = ""
            else:
                saved = self._credentials.set(INAT_TOKEN, token) and saved
        if vision_key.strip():
            saved = self._credentials.set(MYCOMAP_KEY, vision_key) and saved
        if not saved:
            self.showMessage.emit(
                "Could not save the credentials (see the log); they are not stored."
            )
        self.settingsChanged.emit()
        if inat_token.strip():
            self._token_warned_for = ""
            self._resume_after_verify = self._token_failed_run
            self.verifyInat()
        if vision_key.strip():
            self.refreshUsage()

    @Slot()
    def clearInatToken(self) -> None:  # noqa: N802
        self._credentials.set(INAT_TOKEN, "")
        self._inat_login = ""
        self._config.set(CONFIG_SECTION, "inat_login", "")
        self._config.save()
        self.settingsChanged.emit()

    @Slot()
    def clearVisionKey(self) -> None:  # noqa: N802
        self._credentials.set(MYCOMAP_KEY, "")
        self._usage_text = ""
        self.usageTextChanged.emit()
        self.settingsChanged.emit()

    @Slot(int, int, int, str)
    def saveGroupingSettings(  # noqa: N802
        self,
        time_gap_minutes: int,
        distance_m: int,
        spread_warn_m: int,
        geoprivacy: str,
    ) -> None:
        self._config.set(
            CONFIG_SECTION, "time_gap_minutes", str(max(1, time_gap_minutes))
        )
        self._config.set(CONFIG_SECTION, "distance_meters", str(max(1, distance_m)))
        self._config.set(
            CONFIG_SECTION, "spread_warning_meters", str(max(1, spread_warn_m))
        )
        if geoprivacy in ("open", "obscured", "private"):
            self._config.set(CONFIG_SECTION, "geoprivacy", geoprivacy)
        self._config.save()
        self.settingsChanged.emit()

    @Slot(str, int)
    def saveTileCacheSettings(self, directory: str, size_mb: int) -> None:  # noqa: N802
        directory = directory.strip()
        default_dir = get_app_data_dir() / "map_tiles"
        if directory and Path(directory).expanduser() == default_dir:
            # The field is pre-filled with the folder in use. Saving that back
            # would pin today's app-data path in the config.
            directory = ""
        self._config.set(CONFIG_SECTION, "tile_cache_dir", directory)
        self._config.set(CONFIG_SECTION, "tile_cache_mb", str(max(0, size_mb)))
        self._config.save()
        with self._tile_lock:
            if self._tile_cache is not None:
                self._tile_cache.close()
            self._tile_cache = None
            self._tile_fetcher = None
        self.settingsChanged.emit()

    @Slot()
    def clearTileCache(self) -> None:  # noqa: N802
        fetcher = self.tile_fetcher()
        if fetcher is not None:
            fetcher.cache.clear()
        self.settingsChanged.emit()

    @Slot()
    def verifyInat(self) -> None:  # noqa: N802
        token = self._credentials.get(INAT_TOKEN)
        if not token:
            self._verify_error = "No iNaturalist token saved."
            self.settingsChanged.emit()
            return
        self._verify_seq += 1
        self._begin_job()

        def work():
            try:
                user = INatClient(token, self._http).me()
                self._verifyDone.emit(user.login, "")
            except ApiError as exc:
                self._verifyDone.emit("", describe_inat_error(exc, token))
            except Exception as exc:  # network
                self._verifyDone.emit("", f"Could not reach iNaturalist: {exc}")

        self._executor.submit(work)

    @Slot(str, str)
    def _on_verify_done(self, login: str, error: str) -> None:
        self._end_job()
        self._verify_error = error
        resume = self._resume_after_verify
        self._resume_after_verify = False
        if login:
            self._inat_login = login
            self._config.set(CONFIG_SECTION, "inat_login", login)
            self._config.save()
        self.settingsChanged.emit()
        self._check_existing_if_due()
        if not resume or self._upload_active:
            return
        if login:
            # The last run stopped on the old token and this one works: carry
            # on with the cards that are still waiting.
            self.showMessage.emit("Token accepted. Uploading the rest...")
            self.uploadAll()
        else:
            self.showMessage.emit(f"Still not uploading: {error}")

    @Slot()
    def checkToken(self) -> None:  # noqa: N802
        """Ask iNaturalist whether the saved token still works (dialog opened).

        Tokens last 24 hours, so the one from yesterday's session is usually
        dead; finding that out now beats finding it out as every card fails.
        Quiet: the dialog is not marked busy while the answer is on its way.
        """
        token = self._credentials.get(INAT_TOKEN)
        if not token or self._upload_active:
            return
        self._verify_seq += 1
        seq = self._verify_seq

        def work():
            try:
                user = INatClient(token, self._http).me()
                self._tokenCheckDone.emit(seq, user.login, "", False)
            except ApiError as exc:
                self._tokenCheckDone.emit(
                    seq, "", describe_inat_error(exc, token), exc.status == 401
                )
            except Exception as exc:  # network
                self._tokenCheckDone.emit(
                    seq, "", f"Could not reach iNaturalist: {exc}", False
                )

        self._executor.submit(work)

    @Slot(int, str, str, bool)
    def _on_token_check_done(
        self, seq: int, login: str, error: str, rejected: bool
    ) -> None:
        if seq != self._verify_seq:
            return
        self._verify_error = error
        if login:
            self._inat_login = login
            self._config.set(CONFIG_SECTION, "inat_login", login)
            self._config.save()
        self.settingsChanged.emit()
        self._check_existing_if_due()
        if rejected:
            self.tokenRejected.emit(error)

    @Slot()
    def refreshUsage(self) -> None:  # noqa: N802
        key = self._credentials.get(MYCOMAP_KEY)
        if not key:
            self._usage_text = "No MycoMap Vision key saved"
            self.usageTextChanged.emit()
            return

        def work():
            try:
                usage = MycoMapVisionClient(key, self._http).usage()
                self._usageDone.emit("MycoMap Vision: " + usage.text)
            except ApiError as exc:
                self._usageDone.emit(describe_vision_error(exc))
            except Exception as exc:
                self._usageDone.emit(f"MycoMap Vision unreachable: {exc}")

        self._executor.submit(work)

    @Slot(str)
    def _on_usage_done(self, text: str) -> None:
        self._usage_text = text
        self.usageTextChanged.emit()

    # ------------------------------------------------------------------
    # Opening a batch

    @Slot(list, result=bool)
    def open(self, paths: list) -> bool:
        """Load the batch and build the first grouping. False when empty.

        Opening the dialog again for the photos it already shows picks up
        where it left off: identifications, edits and - most importantly -
        the observations already created on iNaturalist stay attached to
        their cards, so nothing is asked of Vision twice and a card that was
        only partly uploaded is finished rather than uploaded a second time.
        """
        existing = [Path(str(raw)) for raw in paths]
        existing = [path for path in existing if path.exists()]
        if not existing:
            return False
        if self._upload_active:
            # The dialog is modal and refuses to close mid-upload, so this is
            # only a safety net: never rebuild the cards under a running upload.
            return True
        if self._model.cards and [str(p) for p in existing] == [
            p.key for p in self._photos
        ]:
            # Thumbnails are re-read: a photo may have been edited meanwhile.
            self._batch_generation += 1
            self._model.reset(list(self._model.cards))
            self._set_status("Picked up where you left off with these photos.")
            self.cardsChanged.emit()
            self.refreshUsage()
            return True

        records: list[PhotoRecord] = []
        for path in existing:
            try:
                records.append(read_photo_facts(path, len(records)))
            except Exception as exc:
                log.warning("Skipping %s: %s", path, exc)
        if not records:
            return False
        self._photos = records
        self._uploaded_paths = []
        keys = {p.key for p in records}
        self._vision_cache = {
            photo_set: result
            for photo_set, result in self._vision_cache.items()
            if photo_set <= keys
        }
        self._manual_edits = False
        self._grouping_recorded = False
        self._token_failed_run = False
        self._resume_after_verify = False
        self._upload_done = self._upload_total = 0
        self._upload_results = []
        self.uploadProgressChanged.emit()
        self._batch_generation += 1
        self._cancel_upload.clear()
        self._folder = str(records[0].path.parent)
        suggestion = self._history.suggest(
            self._folder,
            float(self.timeGapMinutes * 60),
            float(self.distanceMeters),
        )
        self._time_gap_s = suggestion.time_gap_s
        self._distance_m = suggestion.distance_m
        self._thresholds_source = suggestion.source
        if suggestion.source == "folder":
            self._thresholds_note = (
                "sliders restored from your last upload of this folder"
            )
        elif suggestion.source == "learned":
            self._thresholds_note = (
                f"sliders learned from {suggestion.batches} past uploads"
            )
        else:
            self._thresholds_note = "sliders at the settings defaults"
        self.thresholdsChanged.emit()
        self._reset_existing()
        # An offset confirmed earlier for a camera (same trip) shapes the
        # first grouping already.
        self._photos, _ = self._with_clock(self._photos)
        self._regrouped_for_clock.add(
            frozenset((p.camera, p.clock_offset_s) for p in self._photos)
        )
        self._regroup(announce=True)
        self.refreshUsage()
        self.checkExisting()
        return True

    def _regroup(self, announce: bool = False) -> None:
        """Group the batch, keeping every card whose photos did not change.

        A card with the same photos as before is the same observation: its
        notes, name, map location and Vision answer carry over instead of
        being thrown away by a slider nudge. A card that already has an
        observation on iNaturalist is never regrouped at all - its photos
        are taken out of the pool first - so it cannot be uploaded twice.
        """
        old_cards = list(self._model.cards)
        by_key = {p.key: p for p in self._photos}
        kept: list[Card] = []
        claimed: set[str] = set()
        for card in old_cards:
            if not card.locked:
                continue
            keys = [p.key for p in card.group.photos]
            if keys and all(k in by_key for k in keys) and not claimed & set(keys):
                card.group = ObservationGroup(
                    [by_key[k] for k in keys], ordered=card.group.ordered
                )
                card.selected = False
                kept.append(card)
                claimed.update(keys)
        reusable = {self._photo_set(c): c for c in old_cards if not c.locked}
        groups = initial_grouping(
            [p for p in self._photos if p.key not in claimed],
            time_gap_s=self._time_gap_s,
            distance_m=self._distance_m,
        )
        # Photos the user said go to an existing observation keep going there
        # when a regroup moves them into a new card (unless that card would
        # mix photos meant for two different observations).
        attached_by_key = {
            p.key: c.attach_to
            for c in old_cards
            if not c.locked and c.attach_to is not None
            for p in c.group.photos
        }
        cards = list(kept)
        for group in groups:
            card = reusable.pop(frozenset(p.key for p in group.photos), None)
            if card is None:
                card = self._new_card(group)
                targets = {
                    attached_by_key[p.key].id: attached_by_key[p.key]
                    for p in group.photos
                    if p.key in attached_by_key
                }
                if len(targets) == 1:
                    card.attach_to = next(iter(targets.values()))
            else:
                if card.group.ordered:  # keep the order the user dragged
                    group = ObservationGroup(
                        [by_key[p.key] for p in card.group.photos], ordered=True
                    )
                card.group = group  # same photos, this batch's records
                card.selected = False
            cards.append(card)
        cards.sort(key=lambda c: group_sort_key(c.group))
        self._model.reset(cards)
        if announce:
            self._set_status(
                f"{len(self._photos)} photo(s) grouped into {len(cards)} observation(s)."
            )
        self.cardsChanged.emit()

    @Property(float, notify=thresholdsChanged)
    def timeGapSeconds(self) -> float:  # noqa: N802
        return self._time_gap_s

    @Property(float, notify=thresholdsChanged)
    def distanceMetersLive(self) -> float:  # noqa: N802
        return self._distance_m

    @Property(str, notify=thresholdsChanged)
    def thresholdsNote(self) -> str:  # noqa: N802
        return self._thresholds_note

    @Property(bool, notify=cardsChanged)
    def canRegroup(self) -> bool:  # noqa: N802
        return bool(self._photos) and not any(c.locked for c in self._model.cards)

    @Property(bool, notify=cardsChanged)
    def hasManualEdits(self) -> bool:  # noqa: N802
        return self._manual_edits

    @Slot(float, float)
    def setThresholds(self, time_gap_s: float, distance_m: float) -> None:  # noqa: N802
        """The sliders moved: regroup with the new thresholds.

        Manual edits (split / merge / drag) are discarded; a card whose photos
        come out the same keeps its name, notes, location and Vision answer.
        """
        if not self.canRegroup:
            return
        time_gap_s = max(1.0, float(time_gap_s))
        distance_m = max(1.0, float(distance_m))
        if time_gap_s == self._time_gap_s and distance_m == self._distance_m:
            return
        self._time_gap_s = time_gap_s
        self._distance_m = distance_m
        self._thresholds_source = "manual"
        self._thresholds_note = "sliders set by hand"
        self._manual_edits = False
        self.thresholdsChanged.emit()
        self._regroup()
        self._set_status(
            f"Regrouped into {len(self._model.cards)} observation(s) "
            f"({_fmt_seconds(time_gap_s)} / {_fmt_meters(distance_m)})."
        )

    def _new_card(self, group: ObservationGroup) -> Card:
        card = Card(
            id=self._next_card_id, group=group, geoprivacy=self.defaultGeoprivacy
        )
        self._next_card_id += 1
        self._apply_cached_vision(card)
        return card

    @staticmethod
    def _photo_set(card: Card) -> frozenset:
        return frozenset(p.key for p in card.group.photos)

    def _apply_cached_vision(self, card: Card) -> None:
        """Reuse an earlier Vision answer when the card's photo set is the same."""
        result = self._vision_cache.get(self._photo_set(card))
        if result is None:
            return
        card.vision = result
        card.status = STATUS_IDENTIFIED
        card.status_text = self._vision_status_text(result)
        if result.suggested_name:
            card.chosen_name = result.suggested_name
            card.chosen_rank = result.suggested_rank
            self._lookup_taxon(card)

    @staticmethod
    def _vision_status_text(result: VisionResult) -> str:
        if result.answer_name:
            pct = int((result.answer_confidence or 0) * 100)
            return f"Vision: {result.answer_name} ({pct}% at {result.answer_rank})"
        if result.suggested_name:
            return f"Vision's best guess: {result.suggested_name} (not confident)"
        return "Vision had no suggestion."

    # ------------------------------------------------------------------
    # Card values for the model

    def card_value(self, card: Card, name: str) -> Any:
        warn = float(self.spreadWarningMeters)
        if name == "cardId":
            return card.id
        if name == "photos":
            return [self._photo_dict(p) for p in card.group.sorted_photos()]
        if name == "photoCount":
            return len(card.group.photos)
        if name == "dateText":
            when, own = self._observed_at(card, exif_only=False)
            if when is None:
                return "No capture time"
            text = when.strftime("%Y-%m-%d %H:%M")
            if own is not None and own != when:
                fmt = "%H:%M" if own.date() == when.date() else "%Y-%m-%d %H:%M"
                text += f" (camera clock {own.strftime(fmt)})"
            return text
        if name == "existingMatches":
            if card.locked:
                return []
            return [self._match_dict(card, m) for m in card.matches]
        if name == "attachedObs":
            if card.attach_to is None:
                return {"obsId": 0}
            out = self._obs_dict(card.attach_to)
            out["order"] = self._order_dicts(card)
            return out
        loc = card.location(warn)
        if name == "hasLocation":
            return loc is not None
        if name == "lat":
            return loc.lat if loc else 0.0
        if name == "lon":
            return loc.lon if loc else 0.0
        if name == "locationSource":
            return loc.source if loc else ""
        if name == "accuracyMeters":
            return card.accuracy_m(loc) or 0
        if name == "gpsCount":
            return loc.photos_with_gps if loc else 0
        if name == "spreadMeters":
            return round(loc.spread_m) if loc else 0
        if name == "spreadWarning":
            return bool(loc and loc.spread_warning)
        if name == "uploadError":
            return card.upload_error if card.status == STATUS_ERROR else ""
        if name == "status":
            return card.status
        if name == "statusText":
            return card.status_text
        if name == "suggestedName":
            return card.vision.suggested_name if card.vision else ""
        if name == "suggestedRank":
            return card.vision.suggested_rank if card.vision else ""
        if name == "choices":
            if not card.vision:
                return []
            return [
                {
                    "name": c.name,
                    "rank": c.rank,
                    "confidencePct": c.confidence_pct,
                    "confident": c.confident,
                    "records": c.reference_records or 0,
                }
                for c in card.vision.choices()
            ]
        if name == "chosenName":
            return card.chosen_name
        if name == "chosenRank":
            return card.chosen_rank
        if name == "taxonLabel":
            return card.taxon.label if card.taxon else ""
        if name == "hints":
            return list(card.vision.hints) if card.vision else []
        if name == "gateMessage":
            return (card.vision.gate_message or "") if card.vision else ""
        if name == "geoprivacy":
            return card.geoprivacy
        if name == "description":
            return card.description
        if name == "obsUrl":
            return OBSERVATION_URL.format(id=card.obs_id) if card.obs_id else ""
        if name == "selected":
            return card.selected
        if name == "canSplit":
            return len(card.group.photos) > 1 and not card.locked
        if name == "locked":
            return card.locked
        if name == "visionWarning":
            return self._vision_warning(card)
        if name == "canSplitByVision":
            return (
                not card.locked
                and card.vision is not None
                and card.vision.disagreement() is not None
            )
        return None

    def _vision_warning(self, card: Card) -> str:
        """Vision scored some photos as a different genus: probably two finds."""
        if card.vision is None:
            return ""
        dis = card.vision.disagreement()
        if dis is None:
            return ""
        by_key = {p.key: p for p in card.group.photos}
        names = []
        for index, genus in dis.outliers:
            if index < len(card.vision.photo_keys):
                photo = by_key.get(card.vision.photo_keys[index])
                if photo is not None:
                    names.append(f"{photo.path.name}: {genus}")
        if not names:
            return ""
        return (
            f"Vision sees {dis.combined_genus} in most photos but "
            + ", ".join(names)
            + " - this card may hold two different fungi."
        )

    @Slot(int)
    def splitByVision(self, card_id: int) -> None:  # noqa: N802
        """Partition the card by each photo's own top genus."""
        card = self._card(card_id)
        if card is None or card.locked or card.vision is None:
            return
        dis = card.vision.disagreement()
        if dis is None:
            return
        keys = card.vision.photo_keys
        by_key = {p.key: p for p in card.group.photos}
        indices = [i for i, k in enumerate(keys) if k in by_key]
        groups = dis.groups(indices)
        if len(groups) < 2:
            return
        placed = set()
        new_cards = []
        for idx_list in groups:
            photos = [by_key[keys[i]] for i in idx_list]
            placed.update(p.key for p in photos)
            new_cards.append(ObservationGroup(photos))
        leftovers = [p for p in card.group.photos if p.key not in placed]
        if leftovers:  # photos beyond Vision's 10-photo limit stay with the first
            new_cards[0].photos.extend(leftovers)
        cards = []
        for g in new_cards:
            c = self._new_card(g)
            c.geoprivacy = card.geoprivacy
            c.description = card.description
            c.manual_location = card.manual_location
            c.manual_accuracy_m = card.manual_accuracy_m
            cards.append(c)
        self._model.replace(card, cards)
        self._manual_edits = True
        self._set_status(f"Split into {len(cards)} by Vision's per-photo genus.")
        self.cardsChanged.emit()

    def _photo_dict(self, p: PhotoRecord) -> dict[str, Any]:
        index = p.order
        return {
            "index": index,
            "key": p.key,
            "name": p.path.name,
            "source": f"image://inatphoto/{self._batch_generation}/{index}?w=360",
            # The full-screen viewer appends the width it needs.
            "full": f"image://inatphoto/{self._batch_generation}/{index}",
            # Local time when the camera clock is corrected (cards sort by it).
            "taken": p.time.strftime("%H:%M:%S") if p.taken else "",
            "hasGps": p.has_gps,
        }

    def _card(self, card_id: int) -> Optional[Card]:
        for c in self._model.cards:
            if c.id == card_id:
                return c
        return None

    def _touch(self, card: Card) -> None:
        """The photo set changed: drop any identification in flight/held."""
        card.token += 1
        card.vision = None
        card.chosen_name = ""
        card.chosen_rank = ""
        card.taxon = None
        card.identify_pending = False
        if card.status in (STATUS_IDENTIFIED, STATUS_IDENTIFYING, STATUS_ERROR):
            card.status = STATUS_PENDING
            card.status_text = ""
        self._apply_cached_vision(card)

    # ------------------------------------------------------------------
    # Grouping edits

    @Slot(int, int)
    def splitCard(self, card_id: int, n: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None or card.locked:
            return
        parts = split_group(card.group, max(2, n))
        if len(parts) < 2:
            return
        new_cards = []
        for g in parts:
            c = self._new_card(g)
            c.geoprivacy = card.geoprivacy
            c.description = card.description
            c.manual_location = card.manual_location
            c.manual_accuracy_m = card.manual_accuracy_m
            new_cards.append(c)
        self._model.replace(card, new_cards)
        self._manual_edits = True
        self._set_status(f"Split into {len(new_cards)} observations.")
        self.cardsChanged.emit()

    @Slot(int, list)
    def splitOffPhotos(self, card_id: int, photo_keys: list) -> None:  # noqa: N802
        """Split with photos selected: those become their own observation,
        placed right after the card they came from."""
        card = self._card(card_id)
        if card is None or card.locked:
            return
        wanted = {str(k) for k in photo_keys}
        shown = card.group.sorted_photos()
        taken = [p for p in shown if p.key in wanted]
        if not taken or len(taken) == len(shown):
            return
        split = self._new_card(ObservationGroup(taken, ordered=card.group.ordered))
        split.geoprivacy = card.geoprivacy
        split.description = card.description
        split.manual_location = card.manual_location
        split.manual_accuracy_m = card.manual_accuracy_m
        card.group.photos = [p for p in card.group.photos if p.key not in wanted]
        self._touch(card)
        cards = list(self._model.cards)
        cards.insert(cards.index(card) + 1, split)
        self._model.sync(cards)  # the card itself stays put on screen
        self._model.refresh_all(card)
        self._manual_edits = True
        count = len(taken)
        self._set_status(
            f"Split {count} selected photo{'' if count == 1 else 's'} "
            "into their own observation."
        )
        self.cardsChanged.emit()

    @Slot(int)
    def toggleSelected(self, card_id: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None:
            return
        card.selected = not card.selected
        self._model.refresh(card, "selected")
        self.cardsChanged.emit()

    @Slot()
    def mergeSelected(self) -> None:  # noqa: N802
        chosen = [c for c in self._model.cards if c.selected and not c.locked]
        if len(chosen) < 2:
            return
        merged = self._new_card(merge_groups([c.group for c in chosen]))
        first = chosen[0]
        merged.geoprivacy = first.geoprivacy
        merged.description = first.description
        # A point someone picked on the map is deliberate; keep the first one.
        placed = next((c for c in chosen if c.manual_location is not None), None)
        if placed is not None:
            merged.manual_location = placed.manual_location
            merged.manual_accuracy_m = placed.manual_accuracy_m
        self._model.replace(first, [merged])
        for c in chosen[1:]:
            self._model.remove(c)
        self._manual_edits = True
        self._set_status(f"Merged {len(chosen)} cards.")
        self.cardsChanged.emit()

    @Slot(list, int, str, bool)
    def movePhotos(  # noqa: N802
        self, photo_keys: list, target_card_id: int, target_key: str, after: bool
    ) -> None:
        """Drag-and-drop of one or more photos onto a card (-1 = a new card).

        They land just before (or ``after``) the photo ``target_key`` of that
        card, or at its end when ``target_key`` is empty, and that order is
        the order on iNaturalist. Photos dragged within their own card are
        reordered; the card's identification stays, since its photo set did
        not change.
        """
        cards = self._model.cards
        wanted = {str(k) for k in photo_keys}
        movable = {
            p.key
            for c in cards
            if not c.locked
            for p in c.group.photos
            if p.key in wanted
        }
        if not movable:
            return
        target = self._card(target_card_id) if target_card_id >= 0 else None
        if target_card_id >= 0 and (target is None or target.locked):
            return
        if target is None and any(self._photo_set(c) == movable for c in cards):
            return  # they already are an observation of their own
        source = next(c for c in cards if self._photo_set(c) & movable)
        shown_before = {c.id: [p.key for p in c.group.sorted_photos()] for c in cards}
        by_group = {id(c.group): c for c in cards}
        groups = [c.group for c in cards]
        target_index = cards.index(target) if target is not None else len(groups)
        new_groups = move_photos(groups, movable, target_index, target_key, after)
        new_cards: list[Card] = []
        reordered: list[Card] = []
        changed: list[Card] = []
        regrouped = len(new_groups) != len(cards)
        for g in new_groups:
            card = by_group.get(id(g))
            if card is None:
                card = self._new_card(g)
                card.geoprivacy = source.geoprivacy
                regrouped = True
            else:
                shown = [p.key for p in g.sorted_photos()]
                if set(shown) != set(shown_before[card.id]):
                    self._touch(card)
                    changed.append(card)
                    regrouped = True
                elif shown != shown_before[card.id]:
                    reordered.append(card)
            new_cards.append(card)
        if regrouped:
            # Row by row, not a reset: the cards that stay are not rebuilt,
            # so the list does not jump while the user is looking at it.
            self._model.sync(new_cards)
            for card in changed:
                self._model.refresh_all(card)
            self._manual_edits = True
            self.cardsChanged.emit()
        else:
            for card in reordered:
                self._model.refresh(card, "photos", "attachedObs")

    # ------------------------------------------------------------------
    # Map picker: where the other observations are

    @Slot(str)
    def copyText(self, text: str) -> None:  # noqa: N802
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    @Slot(int, result=list)
    def mapNeighbours(self, card_id: int) -> list:  # noqa: N802
        """The other cards of this upload that have a location, for the map."""
        warn = float(self.spreadWarningMeters)
        cards = self._model.cards
        here = next((i for i, c in enumerate(cards) if c.id == card_id), 0)
        out = []
        for i, card in enumerate(cards):
            loc = card.location(warn)
            photos = card.group.sorted_photos()
            if card.id == card_id or loc is None or not photos:
                continue
            base = f"image://inatphoto/{self._batch_generation}/{photos[0].order}"
            label = f"Observation {i + 1}"
            if card.chosen_name:
                label += f" · {card.chosen_name}"
            out.append(
                {
                    "kind": "upload",
                    "lat": loc.lat,
                    "lon": loc.lon,
                    "source": base + "?w=128",
                    "preview": base + "?w=360",
                    "label": label,
                    "cardsAway": abs(i - here),
                }
            )
        return out

    @Property(list, notify=myObservationsChanged)
    def myObservations(self) -> list:  # noqa: N802
        with self._thumb_lock:
            have = {key[0] for key in self._thumb_cache if key[1] == "square"}
        return [
            {
                "kind": "inat",
                "lat": o.lat,
                "lon": o.lon,
                # Only once downloaded: the provider never fetches a marker
                # image, and a missing one is just an empty frame.
                "source": f"image://inatobs/{o.id}/square" if o.id in have else "",
                "preview": f"image://inatobs/{o.id}/small" if o.photo_url else "",
                "label": " · ".join(
                    bit for bit in (o.name or "Unknown", o.observed_on) if bit
                ),
            }
            for o in self._my_observations.values()
        ]

    @Property(bool, notify=myObservationsChanged)
    def myObservationsBusy(self) -> bool:  # noqa: N802
        return self._my_observations_busy

    @Property(str, notify=myObservationsChanged)
    def myObservationsStatus(self) -> str:  # noqa: N802
        return self._my_observations_status

    @Slot(float, float, float, float)
    def fetchMyObservations(  # noqa: N802
        self, swlat: float, swlng: float, nelat: float, nelng: float
    ) -> None:
        """Add the user's iNaturalist observations inside this map view.

        Each call adds to what is already shown, so zooming out and asking
        again brings in more.
        """
        if self._my_observations_busy:
            return
        login = self._inat_login
        if not login:
            self._my_observations_status = (
                "Not signed in: add your iNaturalist token in Settings first."
            )
            self.myObservationsChanged.emit()
            return
        token = self._credentials.get(INAT_TOKEN) or ""
        box = (
            max(-90.0, float(swlat)),
            max(-180.0, float(swlng)),
            min(90.0, float(nelat)),
            min(180.0, float(nelng)),
        )
        self._my_observations_seq += 1
        seq = self._my_observations_seq
        self._my_observations_busy = True
        self._my_observations_status = "Asking iNaturalist…"
        self.myObservationsChanged.emit()

        def work():
            client = INatClient(token, self._http)
            try:
                try:
                    rows, total = client.observations_in_box(
                        login, *box, per_page=MAP_OBSERVATIONS_PER_FETCH
                    )
                except ApiError as exc:
                    if exc.status != 401:
                        raise
                    # The token ran out (24 h); the public record still answers.
                    rows, total = client.observations_in_box(
                        login,
                        *box,
                        per_page=MAP_OBSERVATIONS_PER_FETCH,
                        authenticated=False,
                    )
                self._myObservationsDone.emit(seq, rows, total, "")
            except ApiError as exc:
                self._myObservationsDone.emit(seq, [], 0, describe_inat_error(exc, token))
            except Exception as exc:  # network
                self._myObservationsDone.emit(
                    seq, [], 0, f"Could not reach iNaturalist: {exc}"
                )

        self._executor.submit(work)

    @Slot(int, object, int, str)
    def _on_my_observations_done(self, seq: int, rows, total: int, error: str) -> None:
        if seq != self._my_observations_seq:
            return
        self._my_observations_busy = False
        if error:
            self._my_observations_status = error
        else:
            added = 0
            for row in rows:
                if row.id not in self._my_observations:
                    added += 1
                self._my_observations[row.id] = row
            if not rows:
                text = "You have no iNaturalist observations in this view."
            elif total > len(rows):
                text = (
                    f"Showing the newest {len(rows)} of your {total} observations "
                    "here; zoom in and press again for the rest."
                )
            else:
                text = f"{len(rows)} of your observations here ({added} new)."
            self._my_observations_status = text
            self._prefetch_marker_thumbs()
        self.myObservationsChanged.emit()

    @Slot()
    def clearMyObservations(self) -> None:  # noqa: N802
        self._my_observations_seq += 1
        self._my_observations_busy = False
        self._my_observations = {}
        self._my_observations_status = ""
        self._thumb_pending = set()
        self._thumb_refresh.stop()
        with self._thumb_lock:
            self._thumb_cache.clear()
        self.myObservationsChanged.emit()

    def _download_thumb(self, observation, size: str) -> Optional[bytes]:
        try:
            return self._http.get_bytes(
                observation.photo(size), timeout=MAP_THUMB_TIMEOUT_S
            )
        except Exception as exc:
            log.debug("Observation photo %s failed: %s", observation.id, exc)
            return None

    def _remember_thumb(self, key: tuple[int, str], data: bytes) -> None:
        with self._thumb_lock:
            while len(self._thumb_cache) >= MAP_THUMB_CACHE_ENTRIES:
                self._thumb_cache.pop(next(iter(self._thumb_cache)))
            self._thumb_cache[key] = data

    def _prefetch_marker_thumbs(self) -> None:
        """Download the marker image of every observation that lacks one."""
        seq = self._my_observations_seq
        with self._thumb_lock:
            have = {key[0] for key in self._thumb_cache if key[1] == "square"}
        for observation in list(self._my_observations.values()):
            oid = observation.id
            if not observation.photo_url or oid in have or oid in self._thumb_pending:
                continue
            self._thumb_pending.add(oid)

            def work(observation=observation) -> None:
                data = None
                if seq == self._my_observations_seq:  # not cleared meanwhile
                    data = self._download_thumb(observation, "square")
                if data and seq == self._my_observations_seq:
                    self._remember_thumb((observation.id, "square"), data)
                self._thumbFetched.emit(seq, observation.id)

            try:
                self._thumb_executor.submit(work)
            except RuntimeError:  # shut down
                return

    @Slot(int, int)
    def _on_thumb_fetched(self, seq: int, obs_id: int) -> None:
        if seq != self._my_observations_seq:
            return
        self._thumb_pending.discard(obs_id)
        if not self._thumb_refresh.isActive():
            self._thumb_refresh.start()

    def observation_thumb(
        self, obs_id: int, size: str, index: int = 0
    ) -> Optional[bytes]:
        """A photo of one of the fetched observations; runs on image threads.

        Marker images ("square") come from the cache only (downloaded by
        ``_prefetch_marker_thumbs``); the one large preview ("small") of the
        marker under the pointer is downloaded here, as are the photos of an
        existing observation a card adds to (``index`` > 0 for the others).
        """
        if size not in ("square", "small"):
            return None
        key = (obs_id, size if index == 0 else f"{size}:{index}")
        with self._thumb_lock:
            cached = self._thumb_cache.get(key)
        if cached is not None or size == "square":
            return cached
        observation = (
            self._my_observations.get(obs_id)
            if index == 0
            else None
        ) or self._existing_observation(obs_id)
        if observation is None or not observation.photo_url:
            return None
        if index == 0:
            data = self._download_thumb(observation, size)
        else:
            url = observation.photo(size, index)
            try:
                data = self._http.get_bytes(url, timeout=MAP_THUMB_TIMEOUT_S) if url else None
            except Exception as exc:
                log.debug("Observation %s photo %s failed: %s", obs_id, index, exc)
                data = None
        if data:
            self._remember_thumb(key, data)
        return data

    def _existing_observation(self, obs_id: int) -> Optional[ExistingObservation]:
        """A fetched observation, or the one a card adds to (kept after a
        fresh fetch no longer lists it). Safe from image threads."""
        found = self._existing_by_id.get(obs_id)
        if found is not None:
            return found
        for card in list(self._model.cards):
            if card.attach_to is not None and card.attach_to.id == obs_id:
                return card.attach_to
        return None

    @Slot(int, float, float, int)
    def setLocation(  # noqa: N802
        self, card_id: int, lat: float, lon: float, accuracy_m: int
    ) -> None:
        """A point chosen on the map, with the user's accuracy estimate in metres."""
        card = self._card(card_id)
        if card is None:
            return
        card.manual_location = (float(lat), float(lon))
        card.manual_accuracy_m = (
            min(int(accuracy_m), MAX_ACCURACY_M)
            if accuracy_m > 0
            else DEFAULT_MANUAL_ACCURACY_M
        )
        self._model.refresh(
            card,
            "hasLocation",
            "lat",
            "lon",
            "locationSource",
            "accuracyMeters",
            "spreadWarning",
            "spreadMeters",
            "gpsCount",
        )
        self.cardsChanged.emit()

    @Slot(int)
    def clearManualLocation(self, card_id: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None:
            return
        card.manual_location = None
        card.manual_accuracy_m = None
        self._model.refresh(
            card,
            "hasLocation",
            "lat",
            "lon",
            "locationSource",
            "accuracyMeters",
            "spreadWarning",
            "spreadMeters",
            "gpsCount",
        )
        self.cardsChanged.emit()

    @Slot(int, str)
    def setGeoprivacy(self, card_id: int, value: str) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None or value not in ("open", "obscured", "private"):
            return
        card.geoprivacy = value
        self._model.refresh(card, "geoprivacy")

    @Slot(int, str)
    def setDescription(self, card_id: int, text: str) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None:
            return
        card.description = text
        self._model.refresh(card, "description")

    @Slot(int, str, str)
    def setChosenName(self, card_id: int, name: str, rank: str) -> None:  # noqa: N802
        """The user picked a name (from Vision's list or typed)."""
        card = self._card(card_id)
        if card is None:
            return
        name = inat_taxon_name(name)
        if name == card.chosen_name and card.taxon is not None:
            return
        card.chosen_name = name
        card.chosen_rank = rank
        card.taxon = None
        self._model.refresh(card, "chosenName", "chosenRank", "taxonLabel")
        if name:
            self._lookup_taxon(card)

    @Slot(int, str)
    def suggestTaxa(self, card_id: int, text: str) -> None:  # noqa: N802
        """Autocomplete for the name field; answers with taxonSuggestions.

        Abbreviations work: "p semilanc" finds Psilocybe semilanceata.
        """
        query = " ".join(text.split())
        if len(query) < 2:
            self.taxonSuggestions.emit(card_id, text, [])
            return
        self._suggest_seq += 1
        seq = self._suggest_seq
        cached = self._suggest_cache.get(query.casefold())
        if cached is not None:
            self._on_suggest_done(card_id, seq, text, cached)
            return

        def work():
            if seq != self._suggest_seq:  # more was typed while this waited
                return
            try:
                found = INatClient("", self._http).taxa_autocomplete(query, 30)
            except Exception as exc:
                log.debug("Taxon autocomplete failed for %r: %s", query, exc)
                return
            self._suggestDone.emit(card_id, seq, text, rank_suggestions(query, found))

        self._suggest_executor.submit(work)

    @Slot(int, int, str, object)
    def _on_suggest_done(self, card_id: int, seq: int, text: str, taxa) -> None:
        taxa = list(taxa)
        if len(self._suggest_cache) > 500:
            self._suggest_cache.clear()
        self._suggest_cache[" ".join(text.split()).casefold()] = taxa
        if seq != self._suggest_seq:
            return
        rows = []
        for taxon in taxa[:8]:
            self._suggested[taxon.id] = taxon
            rows.append(
                {
                    "taxonId": taxon.id,
                    "name": taxon.name,
                    "rank": taxon.rank,
                    "commonName": taxon.common_name,
                }
            )
        self.taxonSuggestions.emit(card_id, text, rows)

    @Slot(int, int)
    def chooseSuggestion(self, card_id: int, taxon_id: int) -> None:  # noqa: N802
        """A row of the autocomplete list was picked: name and iNat taxon."""
        taxon = self._suggested.get(taxon_id)
        if taxon is None:
            return
        # iNaturalist just returned this taxon; no second lookup needed.
        self._taxon_cache[self._taxon_key(taxon.name, taxon.rank)] = taxon
        self.setChosenName(card_id, taxon.name, taxon.rank)

    @staticmethod
    def _taxon_key(name: str, rank: str) -> tuple[str, str]:
        return (name.strip().casefold(), (rank or "").strip().casefold())

    def _lookup_taxon(self, card: Card) -> None:
        name = card.chosen_name
        rank = card.chosen_rank
        key = self._taxon_key(name, rank)
        if key in self._taxon_cache:
            card.taxon = self._taxon_cache[key]
            self._model.refresh(card, "taxonLabel")
            return
        waiting = self._taxon_waiting.get(key)
        if waiting is not None:
            # The same name is already being looked up for another card.
            waiting.append((card.id, card.token))
            return
        self._taxon_waiting[key] = [(card.id, card.token)]
        token = self._credentials.get(INAT_TOKEN)

        def work():
            try:
                taxon = INatClient(token, self._http).resolve_taxon(name, rank)
                self._taxonLookupDone.emit(taxon, "", key)
            except Exception as exc:
                self._taxonLookupDone.emit(None, str(exc), key)

        self._executor.submit(work)

    @Slot(object, str, object)
    def _on_taxon_lookup_done(self, taxon, error: str, key) -> None:
        key = tuple(key)
        if not error:
            # "No exact match" is an answer too; a failed call is not.
            self._taxon_cache[key] = taxon
        for card_id, token in self._taxon_waiting.pop(key, []):
            card = self._card(card_id)
            if card is None or card.token != token:
                continue
            if (
                taxon is not None
                and taxon.name.casefold() == card.chosen_name.casefold()
            ):
                card.taxon = taxon
            self._model.refresh(card, "taxonLabel")

    # ------------------------------------------------------------------
    # Observations already on iNaturalist (phone observations, earlier
    # uploads of the same find) and the camera clock offset

    def _reset_existing(self) -> None:
        """A new batch: forget the last one's observations and decisions,
        and start each camera from the offset saved nearest its dates."""
        self._existing_seq += 1
        self._existing = []
        self._existing_by_id = {}
        self._existing_busy = False
        self._existing_fetched = False
        self._existing_status = ""
        self._clock_decisions = {}
        self._clock_version += 1
        self._regrouped_for_clock = set()
        self._dismissed = {}
        self._dismiss_version += 1
        self._match_signature = None
        days: dict[str, list] = {}
        for p in self._photos:
            if p.taken is not None and p.taken_from_exif:
                days.setdefault(p.camera, []).append(p.taken.date())
        self._camera_days = {
            camera: statistics.median_low(dates) for camera, dates in days.items()
        }
        self._clock_saved = {}
        for camera, day in self._camera_days.items():
            record = self._offset_store.lookup(camera, day)
            if record is not None:
                self._clock_saved[camera] = OffsetGuess(
                    offset_s=record.offset_s,
                    source="saved",
                    from_obs=[record.obs_id] if record.obs_id else [],
                    saved_on=record.on,
                )
        self._clock = dict(self._clock_saved)
        self.existingChanged.emit()

    def _check_existing_if_due(self) -> None:
        if self._photos and not self._existing_fetched and not self._existing_busy:
            self.checkExisting()

    @Slot()
    def checkExisting(self) -> None:  # noqa: N802
        """Fetch the user's observations on the batch's dates, then match."""
        if self._existing_busy:
            return
        window = fetch_window(self._photos)
        login = self._inat_login
        if window is None:
            self._existing_status = (
                "No EXIF capture times: not checking for observations "
                "already on iNaturalist."
            )
            self.existingChanged.emit()
            return
        if not login:
            self._existing_status = (
                "Add your iNaturalist token in Settings to check for "
                "observations you already made of these finds."
            )
            self.existingChanged.emit()
            return
        token = self._credentials.get(INAT_TOKEN) or ""
        self._existing_seq += 1
        seq = self._existing_seq
        self._existing_busy = True
        self._existing_status = (
            "Checking iNaturalist for observations you made on these dates…"
        )
        self.existingChanged.emit()
        first, last = window

        def work():
            client = INatClient(token, self._http)
            try:
                try:
                    rows = client.observations_between(login, first, last)
                except ApiError as exc:
                    if exc.status != 401:
                        raise
                    # The token ran out (24 h); the public record still
                    # answers, without the true spot of obscured ones.
                    rows = client.observations_between(
                        login, first, last, authenticated=False
                    )
                self._existingDone.emit(seq, rows, "")
            except ApiError as exc:
                self._existingDone.emit(seq, [], describe_inat_error(exc, token))
            except Exception as exc:  # network
                self._existingDone.emit(seq, [], f"Could not reach iNaturalist: {exc}")

        self._executor.submit(work)

    @Slot(int, object, str)
    def _on_existing_done(self, seq: int, rows, error: str) -> None:
        if seq != self._existing_seq:
            return
        self._existing_busy = False
        if error:
            self._existing_status = f"Could not check for existing observations: {error}"
            self.existingChanged.emit()
            return
        self._existing_fetched = True
        self._existing = list(rows)
        self._existing_by_id = {o.id: o for o in self._existing}
        self._match_signature = None
        self._rematch()

    def _card_facts(self, card: Card) -> CardFacts:
        loc = card.location(float(self.spreadWarningMeters))
        return CardFacts(
            key=card.id,
            photos=card.group.photos,
            lat=loc.lat if loc else None,
            lon=loc.lon if loc else None,
            spread_m=loc.spread_m if loc else 0.0,
        )

    def _rematch(self) -> None:
        """Work out the clock offsets and each card's possible matches.

        Runs after every change to the cards (coalesced by a timer), but
        only does the work when a photo set, a location, the observations
        or a clock decision changed.
        """
        cards = list(self._model.cards)
        facts = {c.id: self._card_facts(c) for c in cards}
        signature = (
            self._existing_seq,
            self._clock_version,
            self._dismiss_version,
            tuple(
                (c.id, self._photo_set(c), facts[c.id].lat, facts[c.id].lon, c.locked)
                for c in cards
            ),
        )
        if signature == self._match_signature:
            return
        self._match_signature = signature
        before = {
            c.id: (self.card_value(c, "existingMatches"), self.card_value(c, "dateText"))
            for c in cards
        }
        priors = dict(self._clock_saved)
        priors.update(self._clock_decisions)
        dismissed = {c.id: self._dismissed_for(c) for c in cards}
        if self._existing:
            self._clock = detect_offsets(
                list(facts.values()),
                self._existing,
                priors,
                skip_pairs={(cid, oid) for cid, ids in dismissed.items() for oid in ids},
            )
        else:
            self._clock = priors
        if self._apply_clock_to_photos():
            return  # the cards changed under us; matching runs again
        offsets = {camera: g.offset_s for camera, g in self._clock.items()}
        exclude = {c.obs_id for c in cards if c.obs_id}
        for c in cards:
            if c.locked or not self._existing:
                c.matches = []
            else:
                c.matches = match_card(
                    facts[c.id], self._existing, offsets, exclude | dismissed[c.id]
                )
        for c in cards:
            after = (self.card_value(c, "existingMatches"), self.card_value(c, "dateText"))
            if after != before[c.id]:
                self._model.refresh(c, "existingMatches", "dateText")
        if self._existing_fetched:
            window = fetch_window(self._photos)
            span = (
                f" from {window[0]:%b %d} to {window[1]:%b %d}" if window else ""
            )
            waiting = sum(
                1 for c in cards if c.matches and c.attach_to is None and not c.locked
            )
            adding = sum(1 for c in cards if c.attach_to is not None)
            parts = []
            if waiting:
                parts.append(
                    f"{waiting} card{'' if waiting == 1 else 's'} may already be "
                    "on iNaturalist (see the blue boxes)"
                )
            if adding:
                parts.append(
                    f"{adding} card{'' if adding == 1 else 's'} "
                    f"add{'s' if adding == 1 else ''} photos to one of them"
                )
            self._existing_status = (
                f"{len(self._existing)} of your iNaturalist observations{span}; "
                + ("; ".join(parts) if parts else "none of the cards look like one of them")
                + "."
            )
        self.existingChanged.emit()

    def _card_offset(self, card: Card) -> int:
        camera = self._card_facts(card).main_camera()
        guess = self._clock.get(camera) if camera is not None else None
        return guess.offset_s if guess is not None else 0

    def _obs_dict(self, obs: ExistingObservation) -> dict[str, Any]:
        if obs.wall_time is not None:
            when = obs.wall_time.strftime("%Y-%m-%d %H:%M")
        elif obs.observed_on is not None:
            when = obs.observed_on.isoformat()
        else:
            when = "no date"
        return {
            "obsId": obs.id,
            "url": OBSERVATION_URL.format(id=obs.id),
            "name": obs.name or "Unknown",
            "photoCount": obs.photo_count,
            "thumb": f"image://inatobs/{obs.id}/small" if obs.photo_url else "",
            "when": when,
            "detail": "",
        }

    def _match_dict(self, card: Card, match: Match) -> dict[str, Any]:
        out = self._obs_dict(match.obs)
        bits = []
        if match.time_gap_s is None:
            bits.append("same day (it has no time of day)")
        elif match.time_gap_s < 60:
            bits.append("taken while these photos were")
        else:
            bits.append(f"{round(match.time_gap_s / 60)} min from these photos")
        if match.camera_clock:
            bits.append("by this camera's own clock")
        else:
            offset = self._card_offset(card)
            if offset:
                bits.append(f"with the camera clock {describe_offset(offset)}")
        if match.distance_m is None:
            bits.append("no GPS to compare")
        elif match.distance_m < 1000:
            bits.append(f"{round(match.distance_m)} m away")
        else:
            bits.append(f"{match.distance_m / 1000:.1f} km away")
        out["detail"] = ", ".join(bits)
        return out

    def _local_time(self, p: PhotoRecord) -> Optional[datetime]:
        """``p``'s capture time corrected by a confirmed camera clock offset
        (the records carry it, see ``_apply_clock_to_photos``)."""
        return p.time

    def _with_clock(
        self, records: list[PhotoRecord]
    ) -> tuple[list[PhotoRecord], bool]:
        """``records`` with each camera's confirmed clock offset in them
        (0 for a camera without one); and whether any changed."""
        wanted = {
            camera: guess.offset_s
            for camera, guess in self._clock.items()
            if guess.corrects_times
        }
        out = []
        changed = False
        for p in records:
            offset = wanted.get(p.camera, 0) if p.taken_from_exif else 0
            if p.clock_offset_s != offset:
                p = replace(p, clock_offset_s=offset)
                changed = True
            out.append(p)
        return out, changed

    def _apply_clock_to_photos(self) -> bool:
        """A confirmed offset changed: give the photos their corrected times.

        A constant offset leaves the gaps between one camera's photos as
        they were, so only a batch from more than one camera (a phone and
        a camera, say) can group differently. Such a batch is regrouped,
        unless that would throw away the user's own splits, merges and
        drags, or a card is already on iNaturalist. False when nothing
        changed.
        """
        records, changed = self._with_clock(self._photos)
        if not changed:
            return False
        state = frozenset((p.camera, p.clock_offset_s) for p in records)
        first_time = state not in self._regrouped_for_clock
        self._regrouped_for_clock.add(state)
        self._photos = records
        by_key = {p.key: p for p in records}
        for card in self._model.cards:
            card.group.photos = [by_key.get(p.key, p) for p in card.group.photos]
        cameras = {p.camera for p in records if p.taken is not None and p.taken_from_exif}
        if len(cameras) > 1:
            if not first_time:
                pass  # regrouped for these offsets already
            elif self.canRegroup and not self._manual_edits:
                before = len(self._model.cards)
                self._regroup()
                self._set_status(
                    "Regrouped with the camera clock corrected: "
                    f"{before} → {len(self._model.cards)} observation(s)."
                )
            else:
                self._set_status(
                    "Camera clock corrected. The photos of more than one camera "
                    "may now group differently; move a grouping slider to "
                    "regroup (that undoes your own splits and merges)."
                )
        for card in self._model.cards:
            self._model.refresh(card, "photos", "dateText")
        self._match_signature = None
        self._rematch_timer.start()
        return True

    def _observed_at(
        self, card: Card, exif_only: bool
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        """(earliest local time of the card's photos, that photo's own time)."""
        best: Optional[tuple[datetime, datetime]] = None
        for p in card.group.photos:
            if p.taken is None or (exif_only and not p.taken_from_exif):
                continue
            local = self._local_time(p)
            if best is None or local < best[0]:
                best = (local, p.taken)
        return best if best is not None else (None, None)

    def _dismissed_for(self, card: Card) -> set[int]:
        keys = self._photo_set(card)
        return {oid for oid, held in self._dismissed.items() if held & keys}

    @Slot(int, int)
    def dismissMatch(self, card_id: int, obs_id: int) -> None:  # noqa: N802
        """"Not this one": stop offering ``obs_id`` for this card's photos.

        The pair also stops counting as evidence for a camera clock offset.
        """
        card = self._card(card_id)
        if card is None:
            return
        self._dismissed.setdefault(int(obs_id), set()).update(self._photo_set(card))
        self._dismiss_version += 1
        self._rematch()

    @Slot(int)
    def dismissAllMatches(self, card_id: int) -> None:  # noqa: N802
        """Close the card's box: none of the offered observations is it."""
        card = self._card(card_id)
        if card is None or not card.matches:
            return
        keys = self._photo_set(card)
        for match in card.matches:
            self._dismissed.setdefault(match.obs.id, set()).update(keys)
        self._dismiss_version += 1
        self._rematch()

    @Property(int, notify=existingChanged)
    def dismissedCount(self) -> int:  # noqa: N802
        return len(self._dismissed)

    @Slot()
    def restoreDismissed(self) -> None:  # noqa: N802
        """Offer every dismissed match again."""
        if not self._dismissed:
            return
        self._dismissed = {}
        self._dismiss_version += 1
        self._rematch()

    def _combined_order(self, card: Card) -> list[str]:
        """The observation's photos ("inat:<index>") and the card's (photo
        keys), in the order they will be on iNaturalist.

        The card's photos keep the places the user dragged them to among
        the observation's; which of them sits in each place follows the
        card's own order, so reordering either view agrees with the other.
        Photos that joined the card since go last.
        """
        obs = card.attach_to
        if obs is None:
            return []
        existing = [f"inat:{i}" for i in range(len(obs.photos))]
        local = [p.key for p in card.group.sorted_photos()]
        if card.merged_order is None:
            return existing + local
        known = set(existing)
        present = set(local)
        slots = [t for t in card.merged_order if t in known or t in present]
        out: list[str] = []
        taken = 0
        for token in slots:
            if token in known:
                out.append(token)
            else:
                out.append(local[taken])
                taken += 1
        out.extend(local[taken:])
        missing = [t for t in existing if t not in out]
        return missing + out

    def _order_dicts(self, card: Card) -> list[dict[str, Any]]:
        obs = card.attach_to
        by_key = {p.key: p for p in card.group.photos}
        out = []
        for token in self._combined_order(card):
            if token.startswith("inat:"):
                index = int(token[5:])
                photo = obs.photos[index] if index < len(obs.photos) else None
                out.append(
                    {
                        "token": token,
                        "isNew": False,
                        "thumb": (
                            f"image://inatobs/{obs.id}/small/{index}"
                            if photo is not None and photo.url
                            else ""
                        ),
                    }
                )
            else:
                p = by_key.get(token)
                if p is None:
                    continue
                out.append(
                    {
                        "token": token,
                        "isNew": True,
                        "thumb": f"image://inatphoto/{self._batch_generation}/{p.order}?w=360",
                    }
                )
        return out

    @Slot(int, int, int)
    def moveInObservation(  # noqa: N802
        self, card_id: int, from_index: int, to_index: int
    ) -> None:
        """Drag in the "order on iNaturalist" strip: the item at
        ``from_index`` goes before the one now at ``to_index`` (the length
        of the list = last)."""
        card = self._card(card_id)
        if card is None or card.locked or card.attach_to is None:
            return
        order = self._combined_order(card)
        if not (0 <= from_index < len(order)):
            return
        to_index = max(0, min(len(order), to_index))
        token = order.pop(from_index)
        if to_index > from_index:
            to_index -= 1
        order.insert(to_index, token)
        card.merged_order = order
        # The card's own photos follow the same order.
        local = [t for t in order if not t.startswith("inat:")]
        if local != [p.key for p in card.group.sorted_photos()]:
            by_key = {p.key: p for p in card.group.photos}
            card.group.photos = [by_key[k] for k in local if k in by_key]
            card.group.ordered = True
            self._model.refresh(card, "photos", "attachedObs")
        else:
            self._model.refresh(card, "attachedObs")

    @Property(str, notify=existingChanged)
    def existingStatus(self) -> str:  # noqa: N802
        return self._existing_status

    @Property(bool, notify=existingChanged)
    def existingBusy(self) -> bool:  # noqa: N802
        return self._existing_busy

    @Property(list, notify=existingChanged)
    def clockNotes(self) -> list:  # noqa: N802
        """One line per camera whose clock looks off, for the dialog banner."""
        out = []
        for camera, guess in sorted(self._clock.items()):
            if guess.offset_s == 0 or guess.source not in (
                "detected",
                "saved",
                "confirmed",
            ):
                continue
            label = camera.split(" #")[0] or "camera"
            how = describe_offset(guess.offset_s)
            source = ""
            if guess.from_obs:
                source = f"observation {guess.from_obs[0]}"
            if guess.source == "detected":
                count = guess.support
                cards_text = "1 card lines up" if count == 1 else f"{count} cards line up"
                text = (
                    f"{label} clock looks {how}: {cards_text} with your "
                    "observations at that offset. Matching uses it; upload "
                    "times change only if you use it."
                )
            else:
                when = (
                    f"confirmed {guess.saved_on:%b %d}"
                    if guess.source == "saved" and guess.saved_on
                    else "confirmed"
                )
                text = (
                    f"{label} clock is {how} ({when}"
                    + (f" from {source}" if source else "")
                    + "). New observations get the corrected time."
                )
            out.append({"camera": camera, "source": guess.source, "text": text})
        return out

    def _decide_clock(self, camera: str, guess: OffsetGuess, save: bool) -> None:
        self._clock_decisions[camera] = guess
        if save:
            day = self._camera_days.get(camera)
            if day is not None:
                obs_id = guess.from_obs[0] if guess.from_obs else None
                self._offset_store.record(camera, guess.offset_s, day, obs_id)
        self._clock_version += 1
        self._rematch()  # refreshes every card whose date or matches moved

    @Slot(str)
    def useClockOffset(self, camera: str) -> None:  # noqa: N802
        """Trust a detected offset: correct upload times, remember it."""
        guess = self._clock.get(camera)
        if guess is None or guess.offset_s == 0:
            return
        status_before = self._status_text
        self._decide_clock(
            camera,
            OffsetGuess(
                offset_s=guess.offset_s,
                source="confirmed",
                support=guess.support,
                from_obs=list(guess.from_obs),
                saved_on=self._camera_days.get(camera),
            ),
            save=True,
        )
        regrouped = self._status_text if self._status_text != status_before else ""
        self._set_status(
            f"Camera clock: {describe_offset(guess.offset_s)}. "
            "New observations get the corrected time."
            + (f" {regrouped}" if regrouped else "")
        )

    @Slot(str)
    def ignoreClockOffset(self, camera: str) -> None:  # noqa: N802
        """A detected offset is wrong: match on the camera's own clock."""
        self._decide_clock(camera, OffsetGuess(0, "ignored"), save=False)

    @Slot(str)
    def clockWasRight(self, camera: str) -> None:  # noqa: N802
        """A saved or confirmed offset does not hold for these photos."""
        self._decide_clock(camera, OffsetGuess(0, "confirmed"), save=True)
        self._set_status("Camera clock taken as right for these dates.")

    @Slot(int, int)
    def attachToExisting(self, card_id: int, obs_id: int) -> None:  # noqa: N802
        """Upload adds this card's photos to observation ``obs_id``.

        The observation's time is local time from a phone (or wherever it
        was made), so the gap to the camera's photos is the camera clock
        offset: it is confirmed for this camera and remembered, unless the
        match was on the camera's own clock.
        """
        card = self._card(card_id)
        if card is None or card.locked:
            return
        match = next((m for m in card.matches if m.obs.id == obs_id), None)
        obs = match.obs if match is not None else self._existing_by_id.get(obs_id)
        if obs is None:
            return
        card.attach_to = obs
        card.merged_order = None
        note = ""
        facts = self._card_facts(card)
        camera = facts.main_camera()
        if camera is not None and not (match is not None and match.camera_clock):
            held = self._clock.get(camera)
            current = held.offset_s if held is not None else 0
            offset = offset_for_pair(facts, obs, camera, current)
            if offset is not None and not (
                held is not None
                and held.source == "confirmed"
                and held.offset_s == offset
            ):
                span = facts.span(camera=camera)
                self._clock_decisions[camera] = OffsetGuess(
                    offset_s=offset,
                    source="confirmed",
                    from_obs=[obs.id],
                    saved_on=span[0].date() if span else None,
                )
                if span is not None:
                    self._offset_store.record(camera, offset, span[0].date(), obs.id)
                self._clock_version += 1
                if offset:
                    note = f" Camera clock: {describe_offset(offset)}."
        self._model.refresh(card, "attachedObs", "existingMatches")
        self._set_status(
            f"Upload will add these photos to observation {obs.id} "
            f"({obs.name or 'unnamed'}) instead of making a new one.{note}"
        )
        self.cardsChanged.emit()  # reruns matching with the confirmed offset

    @Slot(int)
    def detachFromExisting(self, card_id: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None or card.locked or card.attach_to is None:
            return
        card.attach_to = None
        self._model.refresh(card, "attachedObs", "existingMatches")
        self._set_status("These photos will be a new observation again.")
        self.cardsChanged.emit()

    # ------------------------------------------------------------------
    # Identification

    @Slot()
    def identifyAll(self) -> None:  # noqa: N802
        """Identify every card that has no Vision answer yet.

        A card can also be in the error state because its *upload* failed.
        It already has its answer (and perhaps a name the user chose over
        it); asking again would spend quota and overwrite that choice.
        """
        for card in list(self._model.cards):
            if (
                card.vision is None
                and card.status in (STATUS_PENDING, STATUS_ERROR)
                and not card.identify_pending
                and not card.locked
                and card.attach_to is None  # identified on iNaturalist already
            ):
                self.identifyCard(card.id)

    @Slot()
    def cancelIdentify(self) -> None:  # noqa: N802
        """Drop identifications that are queued, waiting or in flight.

        Called when the dialog closes. Their results are ignored (the card
        token moves on) and each card goes back to how it was, so closing
        never has to wait out a slow or rate-limited Vision call.
        """
        self._cancel_identify.set()
        self._cancel_identify = threading.Event()  # for the next batch of calls
        for card in self._model.cards:
            if not card.identify_pending:
                continue
            card.identify_pending = False
            card.token += 1
            if card.vision is not None:
                card.status = STATUS_IDENTIFIED
                card.status_text = self._vision_status_text(card.vision)
            else:
                card.status = STATUS_PENDING
                card.status_text = ""
            self._model.refresh(card, "status", "statusText")

    @Slot(int)
    def identifyCard(self, card_id: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None or card.locked:
            return
        key = self._credentials.get(MYCOMAP_KEY)
        if not key:
            card.status = STATUS_ERROR
            card.status_text = "Add a MycoMap Vision key in settings to identify."
            self._model.refresh(card, "status", "statusText")
            return
        card.status = STATUS_IDENTIFYING
        card.status_text = "Asking MycoMap Vision..."
        card.identify_pending = True
        self._model.refresh(card, "status", "statusText")
        self._vision_requests += 1
        self.usageTextChanged.emit()
        token = card.token
        photos = card.group.sorted_photos()
        loc = card.location(float(self.spreadWarningMeters))
        observed, _ = self._observed_at(card, exif_only=True)
        cancel = self._cancel_identify
        self._begin_job()

        def work():
            try:
                if cancel.is_set():
                    self._identifyDone.emit(card.id, token, None, "Cancelled")
                    return
                payload = [
                    (p.path.name, images.prepare_jpeg(p.path, images.VISION_LONG_EDGE))
                    for p in photos[:10]
                ]
                client = MycoMapVisionClient(key, self._http)
                waited = 0.0
                while True:
                    try:
                        result = client.identify(
                            payload,
                            latitude=loc.lat if loc else None,
                            longitude=loc.lon if loc else None,
                            observed_on=(
                                observed.strftime("%Y-%m-%d") if observed else None
                            ),
                            client_ref=client_ref_for([p.path for p in photos]),
                        )
                        break
                    except ApiError as exc:
                        # "Too many photos this minute" and "queue full" both
                        # say when to come back. Wait and repeat rather than
                        # failing every card after the first few.
                        delay = retry_delay_seconds(exc)
                        if (
                            delay is None
                            or waited + delay > IDENTIFY_MAX_WAIT_SECONDS
                            or cancel.is_set()
                        ):
                            raise
                        reason = (
                            "is busy" if exc.status == 503 else "rate limit reached"
                        )
                        self._identifyProgress.emit(
                            card.id,
                            token,
                            f"MycoMap Vision {reason}; "
                            f"trying again in {int(delay)} s...",
                        )
                        if cancel.wait(delay):
                            raise
                        waited += delay
                        self._identifyProgress.emit(
                            card.id, token, "Asking MycoMap Vision..."
                        )
                result.photo_keys = [p.key for p in photos[:10]]
                self._identifyDone.emit(card.id, token, result, "")
            except ApiError as exc:
                self._identifyDone.emit(
                    card.id, token, None, describe_vision_error(exc)
                )
            except Exception as exc:
                log.exception("Identify failed")
                self._identifyDone.emit(
                    card.id, token, None, f"Identification failed: {exc}"
                )

        self._identify_executor.submit(work)

    @Slot(int, int, str)
    def _on_identify_progress(self, card_id: int, token: int, text: str) -> None:
        card = self._card(card_id)
        if card is None or card.token != token or not card.identify_pending:
            return
        card.status_text = text
        self._model.refresh(card, "statusText")

    @Slot(int, int, object, str)
    def _on_identify_done(self, card_id: int, token: int, result, error: str) -> None:
        self._end_job()
        card = self._card(card_id)
        if card is None or card.token != token:
            return
        card.identify_pending = False
        if error:
            card.status = STATUS_ERROR
            card.status_text = error
            self._model.refresh(card, "status", "statusText")
            return
        card.vision = result
        self._vision_cache[self._photo_set(card)] = result
        card.status = STATUS_IDENTIFIED
        card.status_text = self._vision_status_text(result)
        if result.usage:
            used = result.usage.get("used")
            limit = result.usage.get("limit")
            if used is not None:
                self._usage_text = (
                    f"MycoMap Vision: {used} / {limit} this period"
                    if limit is not None
                    else f"MycoMap Vision: {used} this period (unlimited)"
                )
                self.usageTextChanged.emit()
        self._model.refresh(
            card,
            "status",
            "statusText",
            "suggestedName",
            "suggestedRank",
            "choices",
            "hints",
            "gateMessage",
            "visionWarning",
            "canSplitByVision",
        )
        if result.suggested_name:
            self.setChosenName(card.id, result.suggested_name, result.suggested_rank)

    # ------------------------------------------------------------------
    # Upload

    @Slot()
    def uploadAll(self) -> None:  # noqa: N802
        token = self._credentials.get(INAT_TOKEN)
        if not token:
            self.showMessage.emit("Add an iNaturalist API token in settings first.")
            return
        if self._token_seconds_left() == 0:
            # Expired by its own clock: every card would fail on it. Say so
            # now; saving a fresh token then starts the upload by itself.
            message = (
                f"{TOKEN_REJECTED}: {describe_token(token)}. "
                "Get a fresh one and paste it in Settings."
            )
            self._verify_error = message
            self._token_failed_run = True
            self.settingsChanged.emit()
            self.tokenRejected.emit(message)
            return
        queue = [
            c
            for c in self._model.cards
            if c.status not in (STATUS_UPLOADING, STATUS_UPLOADED)
            and not c.identify_pending
        ]
        if not queue:
            return
        warn = float(self.spreadWarningMeters)
        jobs = []
        positions: dict[str, int] = {}  # photo key -> place in its card
        # Existing observation id -> where the next card's photos go in it,
        # after the photos it already has (two cards may add to one).
        attach_next: dict[int, int] = {}
        for card in queue:
            card.status = STATUS_UPLOADING
            card.status_text = "Queued for upload"
            card.upload_error = ""
            self._model.refresh(
                card, "status", "statusText", "uploadError", "canSplit", "locked"
            )
            loc = card.location(warn)
            # Only a capture time from EXIF goes to iNaturalist; a file's
            # modification time (the grouping fallback) is not when it was
            # found. A confirmed camera clock offset is applied.
            observed, _ = self._observed_at(card, exif_only=True)
            draft = ObservationDraft(
                observed_on_string=(
                    observed.strftime("%Y-%m-%d %H:%M:%S") if observed else None
                ),
                latitude=loc.lat if loc else None,
                longitude=loc.lon if loc else None,
                positional_accuracy_m=card.accuracy_m(loc),
                geoprivacy=card.geoprivacy,
                taxon_id=card.taxon.id if card.taxon else None,
                species_guess=card.chosen_name,
                description=card.description,
            )
            # A card that already has an observation (an earlier run attached
            # only some of its photos) resumes: no second observation, only
            # the photos still missing go up.
            card_photos = card.group.sorted_photos()
            target = card.obs_id
            reposition: list[tuple[int, int]] = []
            if card.attach_to is not None and target in (None, card.attach_to.id):
                # Photos for an observation that is already on iNaturalist
                # (a phone observation, an earlier upload): no new one.
                target = card.attach_to.id
                if target in attach_next:
                    # A second card adding to it: after the first card's.
                    base = attach_next[target]
                    positions.update(
                        {p.key: base + i for i, p in enumerate(card_photos)}
                    )
                    attach_next[target] = base + len(card_photos)
                else:
                    # Every photo goes to its place in the order shown;
                    # the observation's own photos move only if theirs
                    # changed (or never had one).
                    order = self._combined_order(card)
                    for index, entry in enumerate(order):
                        if entry.startswith("inat:"):
                            photo = card.attach_to.photos[int(entry[5:])]
                            if photo.op_id and photo.position != index:
                                reposition.append((photo.op_id, index))
                        else:
                            positions[entry] = index
                    attach_next[target] = len(order)
            else:
                positions.update({p.key: i for i, p in enumerate(card_photos)})
            pending_photos = [p for p in card_photos if p.key not in card.photo_ids]
            jobs.append(
                (
                    card.id,
                    card.token,
                    draft,
                    pending_photos,
                    card.chosen_name,
                    card.chosen_rank,
                    card.taxon,
                    target,
                    reposition,
                )
            )
        self._upload_done = 0
        self._upload_total = len(jobs)
        self._upload_results = []
        self.uploadProgressChanged.emit()
        self._cancel_upload.clear()
        self._token_failed_run = False
        self._upload_active = True
        self._begin_job()
        self.busyChanged.emit()  # `uploading` changed even if a job was running
        self.cardsChanged.emit()
        self._record_grouping()

        # The worker reads this name when it runs, not now: bind the token
        # under a name nothing else in this method assigns. (A loop above
        # once reused ``token`` and every upload sent a photo path instead.)
        api_token = token

        def work():
            client = INatClient(api_token, self._http)
            for (
                card_id,
                tok,
                draft,
                photos,
                name,
                rank,
                taxon,
                obs_id,
                reposition,
            ) in jobs:
                attached: list[tuple[str, int]] = []
                if self._cancel_upload.is_set():
                    self._uploadCardDone.emit(
                        card_id, tok, obs_id, attached, "Cancelled", ""
                    )
                    continue
                try:
                    if obs_id is None:
                        if draft.taxon_id is None and name and taxon is None:
                            # Only a convenience: without a taxon id the name
                            # still goes up as the species guess, so a failed
                            # lookup must not cost the whole observation.
                            try:
                                found = client.resolve_taxon(name, rank)
                            except Exception as exc:
                                log.warning(
                                    "Taxon lookup failed for %r; sending it as "
                                    "a guess: %s",
                                    name,
                                    exc,
                                )
                                found = None
                            if found is not None:
                                draft.taxon_id = found.id
                        obs_id = client.create_observation(draft)
                except ApiError as exc:
                    self._uploadCardDone.emit(
                        card_id, tok, None, attached, describe_inat_error(exc, api_token), ""
                    )
                    continue
                except Exception as exc:
                    log.exception("Observation create failed")
                    self._uploadCardDone.emit(
                        card_id, tok, None, attached, f"Upload failed: {exc}", ""
                    )
                    continue
                # The observation exists from here on: whatever happens to the
                # photos, its id goes back so a retry resumes instead of
                # creating a duplicate.
                error = ""
                for p in photos:
                    if self._cancel_upload.is_set():
                        error = "Cancelled before every photo was attached"
                        break
                    try:
                        data = images.prepare_jpeg(p.path, images.INAT_LONG_EDGE)
                        photo_id = client.add_photo(
                            obs_id, p.path.name, data, position=positions.get(p.key)
                        )
                        attached.append((p.key, int(photo_id or 0)))
                    except ApiError as exc:
                        log.warning("Photo upload failed %s: %s", p.path, exc)
                        error = describe_inat_error(exc, api_token)
                    except Exception as exc:
                        log.exception("Photo upload failed %s", p.path)
                        error = f"Photo upload failed: {exc}"
                # Photos already on the observation that the new ones now
                # sit among move to their places, once the new ones are in.
                order_error = ""
                for op_id, position in reposition:
                    if self._cancel_upload.is_set():
                        break
                    try:
                        client.set_photo_position(op_id, position)
                    except ApiError as exc:
                        log.warning("Reordering photo %s failed: %s", op_id, exc)
                        order_error = describe_inat_error(exc, api_token)
                    except Exception as exc:
                        log.exception("Reordering photo %s failed", op_id)
                        order_error = str(exc)
                self._uploadCardDone.emit(
                    card_id, tok, obs_id, attached, error, order_error
                )
            self._uploadRunFinished.emit()

        self._executor.submit(work)

    def _record_grouping(self) -> None:
        """The grouping the user is uploading is the ground truth to learn from."""
        try:
            groups = [c.group.photos for c in self._model.cards]
            pairs = pairs_from_groups(groups, haversine_m)
            self._history.record(
                self._folder,
                self._time_gap_s,
                self._distance_m,
                pairs,
                photos=len(self._photos),
                observations=len(groups),
                # Upload pressed again on this batch (a retry) is the same
                # decision, not a second batch to learn from.
                replace_last=self._grouping_recorded,
            )
            self._grouping_recorded = True
        except Exception:
            log.debug("Could not record grouping history", exc_info=True)

    @Slot()
    def cancelUpload(self) -> None:  # noqa: N802
        self._cancel_upload.set()

    @Slot(int, int, object, object, str, str)
    def _on_upload_card_done(
        self,
        card_id: int,
        token: int,
        obs_id,
        attached,
        error: str,
        order_error: str = "",
    ) -> None:
        self._upload_done += 1
        card = self._card(card_id)
        if card is None:
            self._upload_results.append(False)
            self.uploadProgressChanged.emit()
            return
        if obs_id:
            card.obs_id = int(obs_id)
        for key, photo_id in attached or []:
            card.photo_ids[key] = photo_id
        done_keys = set(card.photo_ids)
        attached_paths = [str(p.path) for p in card.group.photos if p.key in done_keys]
        missing = [p for p in card.group.photos if p.key not in done_keys]
        if obs_id and not missing:
            card.status = STATUS_UPLOADED
            card.status_text = (
                "Photos added to your existing observation"
                if card.attach_to is not None and card.obs_id == card.attach_to.id
                else "Uploaded"
            )
            if order_error:
                card.status_text += (
                    f", but its photos could not all be put in the order shown "
                    f"({order_error}); drag them on iNaturalist"
                )
            card.upload_error = ""
            self._uploaded_paths.extend(attached_paths)
        else:
            card.status = STATUS_ERROR
            if obs_id:
                card.status_text = (
                    f"{len(missing)} of {len(card.group.photos)} photo(s) not attached"
                    f" ({error or 'cancelled'}). Upload again to attach the rest."
                )
            else:
                card.status_text = error or "Upload failed"
            card.upload_error = card.status_text
            self._uploaded_paths.extend(attached_paths)
        self._upload_results.append(card.status == STATUS_UPLOADED)
        self.uploadProgressChanged.emit()
        self._model.refresh(
            card, "status", "statusText", "uploadError", "obsUrl", "canSplit", "locked"
        )
        self.cardsChanged.emit()
        if error != "Cancelled":  # cards skipped after Cancel were never tried
            self.uploadCardFinished.emit(card.id)

    @Slot()
    def _on_upload_run_finished(self) -> None:
        self._upload_active = False
        self._end_job()
        self.busyChanged.emit()  # `uploading` changed even if a job still runs
        uploaded = sum(1 for c in self._model.cards if c.status == STATUS_UPLOADED)
        failed = sum(
            1 for c in self._model.cards if c.status == STATUS_ERROR and c.upload_error
        )
        self._token_failed_run = any(
            c.status == STATUS_ERROR and TOKEN_REJECTED in c.upload_error
            for c in self._model.cards
        )
        if failed:
            self._set_status(
                f"{uploaded} uploaded, {failed} failed. "
                + (
                    "Save a fresh token in Settings and the upload continues."
                    if self._token_failed_run
                    else "Fix and retry the failed cards."
                )
            )
            self.uploadFailed.emit(
                uploaded,
                [
                    {
                        "cardId": c.id,
                        "number": number,
                        "name": c.chosen_name,
                        "message": c.upload_error,
                    }
                    for number, c in enumerate(self._model.cards, start=1)
                    if c.status == STATUS_ERROR and c.upload_error
                ],
            )
        else:
            self._set_status(f"Uploaded {uploaded} observation(s) to iNaturalist.")
        if self._uploaded_paths:
            paths = list(dict.fromkeys(self._uploaded_paths))
            all_done = all(c.status == STATUS_UPLOADED for c in self._model.cards)
            self.uploadFinished.emit(paths, all_done)

    @Property(bool, notify=cardsChanged)
    def allUploaded(self) -> bool:  # noqa: N802
        cards = self._model.cards
        return bool(cards) and all(c.status == STATUS_UPLOADED for c in cards)

    # ------------------------------------------------------------------

    def _begin_job(self) -> None:
        self._busy_jobs += 1
        if self._busy_jobs == 1:
            self.busyChanged.emit()

    def _end_job(self) -> None:
        self._busy_jobs = max(0, self._busy_jobs - 1)
        if self._busy_jobs == 0:
            self.busyChanged.emit()

    def _set_status(self, text: str) -> None:
        self._status_text = text
        self.statusTextChanged.emit()


def _fmt_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{int(round(seconds))} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _fmt_meters(meters: float) -> str:
    if meters < 1000:
        return f"{int(round(meters))} m"
    return f"{meters / 1000:.1f} km"
