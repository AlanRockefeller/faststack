"""Qt controller behind the iNaturalist upload dialog.

Owns the observation cards (a :class:`CardModel` the QML Repeater reads),
runs identification and upload on worker threads, and exposes the settings
the dialog edits (credentials, grouping thresholds, tile cache).

Everything that touches the model runs on the GUI thread: workers emit the
private ``_*Done`` signals, which Qt queues back to the controller's thread.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from faststack.inat import images
from faststack.inat.credentials import INAT_TOKEN, MYCOMAP_KEY, CredentialStore
from faststack.inat.grouping import (
    Location,
    ObservationGroup,
    PhotoRecord,
    haversine_m,
    initial_grouping,
    merge_groups,
    move_photo,
    split_group,
)
from faststack.inat.http import ApiError, HttpClient
from faststack.inat.inat_client import (
    API_TOKEN_URL,
    OBSERVATION_URL,
    INatClient,
    ObservationDraft,
    Taxon,
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


@dataclass
class Card:
    id: int
    group: ObservationGroup
    manual_location: Optional[tuple[float, float]] = None
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

    def location(self, spread_warning_m: float) -> Optional[Location]:
        if self.manual_location is not None:
            lat, lon = self.manual_location
            return Location(lat=lat, lon=lon, source="manual")
        return self.group.exif_location(spread_warning_m)


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
        "gpsCount",
        "spreadMeters",
        "spreadWarning",
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

    _identifyDone = Signal(int, int, object, str)  # card id, token, result, error
    _uploadCardDone = Signal(int, int, object, str)  # card id, token, obs id, error
    _uploadRunFinished = Signal()
    _usageDone = Signal(str)
    _verifyDone = Signal(str, str)  # login, error
    _taxonLookupDone = Signal(int, int, object, str)

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
        self._upload_done = 0
        self._upload_total = 0
        self._cancel_upload = threading.Event()
        self._user_agent = (
            user_agent or "FastStack (+https://github.com/AlanRockefeller/faststack)"
        )
        self._http = HttpClient(self._user_agent)
        self._executor = create_daemon_threadpool_executor(2, "inat-upload")
        self._history = GroupingHistory(get_app_data_dir() / HISTORY_FILE)
        self._vision_cache: dict[frozenset, VisionResult] = {}
        self._time_gap_s = float(self.timeGapMinutes * 60)
        self._distance_m = float(self.distanceMeters)
        self._thresholds_source = "default"
        self._thresholds_note = ""
        self._folder = ""
        self._manual_edits = False
        self._tile_cache: Optional[TileCache] = None
        self._tile_fetcher: Optional[TileFetcher] = None
        self._tile_lock = threading.Lock()
        self._inat_login = config.get(CONFIG_SECTION, "inat_login", "") or ""
        self._verify_error = ""
        self._uploaded_paths: list[str] = []

        self._identifyDone.connect(self._on_identify_done)
        self._uploadCardDone.connect(self._on_upload_card_done)
        self._uploadRunFinished.connect(self._on_upload_run_finished)
        self._usageDone.connect(self._on_usage_done)
        self._verifyDone.connect(self._on_verify_done)
        self._taxonLookupDone.connect(self._on_taxon_lookup_done)

    # ------------------------------------------------------------------
    # Providers / lifecycle

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
        try:
            self._executor.shutdown(wait=False)
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
            if c.location(warn) is None and c.status != STATUS_UPLOADED
        )

    @Property(int, notify=cardsChanged)
    def cardsToUpload(self) -> int:
        return sum(1 for c in self._model.cards if c.status != STATUS_UPLOADED)

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy_jobs > 0

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(str, notify=usageTextChanged)
    def usageText(self) -> str:
        return self._usage_text

    @Property(int, notify=uploadProgressChanged)
    def uploadDone(self) -> int:
        return self._upload_done

    @Property(int, notify=uploadProgressChanged)
    def uploadTotal(self) -> int:
        return self._upload_total

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

    @Slot(str, str)
    def saveCredentials(self, inat_token: str, vision_key: str) -> None:  # noqa: N802
        if inat_token.strip():
            self._credentials.set(INAT_TOKEN, inat_token)
        if vision_key.strip():
            self._credentials.set(MYCOMAP_KEY, vision_key)
        self.settingsChanged.emit()
        if inat_token.strip():
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
        self._config.set(CONFIG_SECTION, "tile_cache_dir", directory.strip())
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
        self._begin_job()

        def work():
            try:
                user = INatClient(token, self._http).me()
                self._verifyDone.emit(user.login, "")
            except ApiError as exc:
                self._verifyDone.emit("", describe_inat_error(exc))
            except Exception as exc:  # network
                self._verifyDone.emit("", f"Could not reach iNaturalist: {exc}")

        self._executor.submit(work)

    @Slot(str, str)
    def _on_verify_done(self, login: str, error: str) -> None:
        self._end_job()
        self._verify_error = error
        if login:
            self._inat_login = login
            self._config.set(CONFIG_SECTION, "inat_login", login)
            self._config.save()
        self.settingsChanged.emit()

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
        """Load the batch and build the first grouping. False when empty."""
        records: list[PhotoRecord] = []
        for raw in paths:
            path = Path(str(raw))
            if not path.exists():
                continue
            try:
                records.append(read_photo_facts(path, len(records)))
            except Exception as exc:
                log.warning("Skipping %s: %s", path, exc)
        if not records:
            return False
        self._photos = records
        self._uploaded_paths = []
        self._vision_cache = {}
        self._manual_edits = False
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
        self._regroup(announce=True)
        self.refreshUsage()
        return True

    def _regroup(self, announce: bool = False) -> None:
        groups = initial_grouping(
            self._photos,
            time_gap_s=self._time_gap_s,
            distance_m=self._distance_m,
        )
        cards = [self._new_card(g) for g in groups]
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
        return bool(self._photos) and not any(
            c.status in (STATUS_UPLOADING, STATUS_UPLOADED) for c in self._model.cards
        )

    @Property(bool, notify=cardsChanged)
    def hasManualEdits(self) -> bool:  # noqa: N802
        return self._manual_edits

    @Slot(float, float)
    def setThresholds(self, time_gap_s: float, distance_m: float) -> None:  # noqa: N802
        """The sliders moved: regroup from scratch with the new thresholds.

        Manual edits (split / merge / drag) are discarded; Vision answers are
        kept for any card whose photo set is unchanged.
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
            when = card.group.observed_at
            return when.strftime("%Y-%m-%d %H:%M") if when else "No capture time"
        loc = card.location(warn)
        if name == "hasLocation":
            return loc is not None
        if name == "lat":
            return loc.lat if loc else 0.0
        if name == "lon":
            return loc.lon if loc else 0.0
        if name == "locationSource":
            return loc.source if loc else ""
        if name == "gpsCount":
            return loc.photos_with_gps if loc else 0
        if name == "spreadMeters":
            return round(loc.spread_m) if loc else 0
        if name == "spreadWarning":
            return bool(loc and loc.spread_warning)
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
            return len(card.group.photos) > 1 and card.status not in (
                STATUS_UPLOADING,
                STATUS_UPLOADED,
            )
        return None

    def _photo_dict(self, p: PhotoRecord) -> dict[str, Any]:
        index = p.order
        return {
            "index": index,
            "key": p.key,
            "name": p.path.name,
            "source": f"image://inatphoto/{index}?w=360",
            "taken": p.taken.strftime("%H:%M:%S") if p.taken else "",
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
        if card is None or card.status in (STATUS_UPLOADING, STATUS_UPLOADED):
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
            new_cards.append(c)
        self._model.replace(card, new_cards)
        self._manual_edits = True
        self._set_status(f"Split into {len(new_cards)} observations.")
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
        chosen = [
            c
            for c in self._model.cards
            if c.selected and c.status not in (STATUS_UPLOADING, STATUS_UPLOADED)
        ]
        if len(chosen) < 2:
            return
        merged = self._new_card(merge_groups([c.group for c in chosen]))
        first = chosen[0]
        merged.geoprivacy = first.geoprivacy
        merged.description = first.description
        self._model.replace(first, [merged])
        for c in chosen[1:]:
            self._model.remove(c)
        self._manual_edits = True
        self._set_status(f"Merged {len(chosen)} cards.")
        self.cardsChanged.emit()

    @Slot(str, int)
    def movePhoto(self, photo_key: str, target_card_id: int) -> None:  # noqa: N802
        """Drag-and-drop: move a photo into another card (-1 = a new card)."""
        source = None
        for c in self._model.cards:
            if any(p.key == photo_key for p in c.group.photos):
                source = c
                break
        if source is None or source.status in (STATUS_UPLOADING, STATUS_UPLOADED):
            return
        target = self._card(target_card_id) if target_card_id >= 0 else None
        if target is source:
            return
        if target is not None and target.status in (STATUS_UPLOADING, STATUS_UPLOADED):
            return
        groups = [c.group for c in self._model.cards]
        target_index = (
            self._model.cards.index(target) if target is not None else len(groups)
        )
        before = {id(g): c for g, c in zip(groups, self._model.cards)}
        new_groups = move_photo(groups, photo_key, target_index)
        # Rebuild the card list, keeping Card objects for surviving groups.
        new_cards: list[Card] = []
        for g in new_groups:
            card = before.get(id(g))
            if card is None:
                card = self._new_card(g)
                card.geoprivacy = source.geoprivacy
            new_cards.append(card)
        self._touch(source)
        if target is not None:
            self._touch(target)
        self._model.reset(new_cards)
        self._manual_edits = True
        self.cardsChanged.emit()

    @Slot(int, float, float)
    def setLocation(self, card_id: int, lat: float, lon: float) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None:
            return
        card.manual_location = (float(lat), float(lon))
        self._model.refresh(
            card,
            "hasLocation",
            "lat",
            "lon",
            "locationSource",
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
        self._model.refresh(
            card,
            "hasLocation",
            "lat",
            "lon",
            "locationSource",
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
        name = name.strip()
        if name == card.chosen_name and card.taxon is not None:
            return
        card.chosen_name = name
        card.chosen_rank = rank
        card.taxon = None
        self._model.refresh(card, "chosenName", "chosenRank", "taxonLabel")
        if name:
            self._lookup_taxon(card)

    def _lookup_taxon(self, card: Card) -> None:
        token = self._credentials.get(INAT_TOKEN)
        name = card.chosen_name
        tok = card.token

        def work():
            try:
                taxon = INatClient(token, self._http).resolve_taxon(name)
                self._taxonLookupDone.emit(card.id, tok, taxon, "")
            except Exception as exc:
                self._taxonLookupDone.emit(card.id, tok, None, str(exc))

        self._executor.submit(work)

    @Slot(int, int, object, str)
    def _on_taxon_lookup_done(
        self, card_id: int, token: int, taxon, error: str
    ) -> None:
        card = self._card(card_id)
        if card is None or card.token != token:
            return
        if taxon is not None and taxon.name.casefold() == card.chosen_name.casefold():
            card.taxon = taxon
        self._model.refresh(card, "taxonLabel")

    # ------------------------------------------------------------------
    # Identification

    @Slot()
    def identifyAll(self) -> None:  # noqa: N802
        for card in list(self._model.cards):
            if (
                card.status in (STATUS_PENDING, STATUS_ERROR)
                and not card.identify_pending
            ):
                self.identifyCard(card.id)

    @Slot(int)
    def identifyCard(self, card_id: int) -> None:  # noqa: N802
        card = self._card(card_id)
        if card is None or card.status in (STATUS_UPLOADING, STATUS_UPLOADED):
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
        token = card.token
        photos = card.group.sorted_photos()
        loc = card.location(float(self.spreadWarningMeters))
        observed = card.group.observed_at
        self._begin_job()

        def work():
            try:
                payload = [
                    (p.path.name, images.prepare_jpeg(p.path, images.VISION_LONG_EDGE))
                    for p in photos[:10]
                ]
                client = MycoMapVisionClient(key, self._http)
                result = client.identify(
                    payload,
                    latitude=loc.lat if loc else None,
                    longitude=loc.lon if loc else None,
                    observed_on=observed.strftime("%Y-%m-%d") if observed else None,
                    client_ref=client_ref_for([p.path for p in photos]),
                )
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

        self._executor.submit(work)

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
                    f"MycoMap Vision: {used} / {limit} identifications used"
                    if limit is not None
                    else f"MycoMap Vision: {used} identifications used"
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
        for card in queue:
            card.status = STATUS_UPLOADING
            card.status_text = "Queued for upload"
            card.upload_error = ""
            self._model.refresh(card, "status", "statusText", "canSplit")
            loc = card.location(warn)
            observed = card.group.observed_at
            draft = ObservationDraft(
                observed_on_string=(
                    observed.strftime("%Y-%m-%d %H:%M:%S") if observed else None
                ),
                latitude=loc.lat if loc else None,
                longitude=loc.lon if loc else None,
                positional_accuracy_m=(
                    int(max(10.0, loc.spread_m))
                    if loc and loc.source == "exif"
                    else (25 if loc else None)
                ),
                geoprivacy=card.geoprivacy,
                taxon_id=card.taxon.id if card.taxon else None,
                species_guess=card.chosen_name,
                description=card.description,
            )
            jobs.append(
                (
                    card.id,
                    card.token,
                    draft,
                    card.group.sorted_photos(),
                    card.chosen_name,
                    card.taxon,
                )
            )
        self._upload_done = 0
        self._upload_total = len(jobs)
        self.uploadProgressChanged.emit()
        self._cancel_upload.clear()
        self._begin_job()
        self.cardsChanged.emit()
        self._record_grouping()

        def work():
            client = INatClient(token, self._http)
            for card_id, tok, draft, photos, name, taxon in jobs:
                if self._cancel_upload.is_set():
                    self._uploadCardDone.emit(card_id, tok, None, "Cancelled")
                    continue
                try:
                    if draft.taxon_id is None and name and taxon is None:
                        found = client.resolve_taxon(name)
                        if found is not None:
                            draft.taxon_id = found.id
                    obs_id = client.create_observation(draft)
                    failures = 0
                    for p in photos:
                        if self._cancel_upload.is_set():
                            break
                        data = images.prepare_jpeg(p.path, images.INAT_LONG_EDGE)
                        try:
                            client.add_photo(obs_id, p.path.name, data)
                        except ApiError as exc:
                            failures += 1
                            log.warning("Photo upload failed %s: %s", p.path, exc)
                    if failures:
                        self._uploadCardDone.emit(
                            card_id,
                            tok,
                            obs_id,
                            f"{failures} photo(s) failed to attach; check the observation.",
                        )
                    else:
                        self._uploadCardDone.emit(card_id, tok, obs_id, "")
                except ApiError as exc:
                    self._uploadCardDone.emit(
                        card_id, tok, None, describe_inat_error(exc)
                    )
                except Exception as exc:
                    log.exception("Upload failed")
                    self._uploadCardDone.emit(
                        card_id, tok, None, f"Upload failed: {exc}"
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
            )
        except Exception:
            log.debug("Could not record grouping history", exc_info=True)

    @Slot()
    def cancelUpload(self) -> None:  # noqa: N802
        self._cancel_upload.set()

    @Slot(int, int, object, str)
    def _on_upload_card_done(
        self, card_id: int, token: int, obs_id, error: str
    ) -> None:
        self._upload_done += 1
        self.uploadProgressChanged.emit()
        card = self._card(card_id)
        if card is None:
            return
        if obs_id:
            card.obs_id = int(obs_id)
            card.status = STATUS_UPLOADED
            card.status_text = "Uploaded" + (f" ({error})" if error else "")
            self._uploaded_paths.extend(str(p.path) for p in card.group.photos)
        else:
            card.status = STATUS_ERROR
            card.status_text = error or "Upload failed"
            card.upload_error = error
        self._model.refresh(card, "status", "statusText", "obsUrl", "canSplit")
        self.cardsChanged.emit()

    @Slot()
    def _on_upload_run_finished(self) -> None:
        self._end_job()
        uploaded = sum(1 for c in self._model.cards if c.status == STATUS_UPLOADED)
        failed = sum(
            1 for c in self._model.cards if c.status == STATUS_ERROR and c.upload_error
        )
        if failed:
            self._set_status(
                f"{uploaded} uploaded, {failed} failed. Fix and retry the failed cards."
            )
        else:
            self._set_status(f"Uploaded {uploaded} observation(s) to iNaturalist.")
        if self._uploaded_paths:
            paths = list(dict.fromkeys(self._uploaded_paths))
            self.uploadFinished.emit(paths, failed == 0)

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
