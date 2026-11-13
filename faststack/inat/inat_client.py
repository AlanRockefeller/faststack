"""iNaturalist API v1 client: verify a token, look up taxa, create observations.

Auth is the 24-hour API token from https://www.inaturalist.org/users/api_token
sent bare in the ``Authorization`` header, as the Observation-Workbench app
does.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from faststack.inat.http import ApiError, HttpClient
from faststack.inat.matching import ExistingObservation, observation_from_row

log = logging.getLogger(__name__)


# Words botanists and mycologists write between a species and its
# infraspecific epithet ("Lactarius deliciosus var. areolatus"). iNaturalist
# names those taxa without them ("Lactarius deliciosus areolatus") and finds
# nothing when they are included.
_RANK_CONNECTORS = frozenset(
    {
        "var", "subsp", "ssp", "subvar", "f", "fo", "forma", "subf",
        "nothosubsp", "nothovar", "morph",
    }
)


def inat_taxon_name(name: str) -> str:
    """``name`` as iNaturalist writes it: infraspecific rank words dropped.

    "Lactarius deliciosus var. areolatus" becomes
    "Lactarius deliciosus areolatus".  Only a rank word after the first two
    words and followed by the epithet it qualifies goes, and only in a name
    whose first word could be a genus.  So a name still being typed
    ("Amanita muscaria f" on the way to "f. flavivolvata") and a common name
    ("Red-tailed hawk dark morph") are left as they are.
    """
    words = (name or "").split()
    if len(words) <= 2 or not words[0].isalpha():
        return " ".join(words)
    last = len(words) - 1
    rest = [
        w
        for i, w in enumerate(words[2:], start=2)
        if i == last or w.rstrip(".").casefold() not in _RANK_CONNECTORS
    ]
    return " ".join(words[:2] + rest)

BASE_URL = "https://api.inaturalist.org/v1"
API_TOKEN_URL = "https://www.inaturalist.org/users/api_token"
OBSERVATION_URL = "https://www.inaturalist.org/observations/{id}"

GEOPRIVACY_CHOICES = ("open", "obscured", "private")

# A JWT is three base64url runs joined by dots; pulling it out of whatever the
# user pasted (JSON, quotes, a label) avoids a silently broken credential.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)


def _jwt_section(part: str) -> Optional[dict]:
    """One base64url section of a JWT as JSON; None when it is not one."""
    try:
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def is_jwt(token: str) -> bool:
    """True when ``token`` is shaped like an iNaturalist API token (a JWT
    whose first section names its signing algorithm)."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return False
    header = _jwt_section(parts[0])
    return bool(header and "alg" in header)


def normalise_token(raw: str) -> str:
    """The token in whatever was pasted (JSON, quotes, the page's address).

    Anything with two dots looks like a token ("www.inaturalist.org" does),
    so a run that decodes as a JWT wins over the first one found.
    """
    text = (raw or "").strip()
    candidates = _JWT_RE.findall(text)
    for candidate in candidates:
        if is_jwt(candidate):
            return candidate
    return candidates[0] if candidates else text.strip("\"' ")


def token_expires_at(token: str) -> Optional[float]:
    """When the token stops working (Unix time), from its own ``exp``
    claim; None when it is not a JWT or does not say."""
    token = normalise_token(token)
    if not is_jwt(token):
        return None
    payload = _jwt_section(token.split(".")[1])
    try:
        return float(payload["exp"]) if payload and payload.get("exp") else None
    except (TypeError, ValueError):
        return None


def describe_token(token: str, now: Optional[float] = None) -> str:
    """Why a token iNaturalist refused was refused, as far as it shows."""
    token = normalise_token(token)
    if not token:
        return "No iNaturalist token is saved."
    if not is_jwt(token):
        return (
            "the saved token is not an iNaturalist API token (it should be "
            "a long code starting with eyJ)"
        )
    expires = token_expires_at(token)
    if expires is None:
        return ""
    now = time.time() if now is None else now
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires))
    if expires <= now:
        return f"it expired at {when}"
    return (
        f"it is not expired (good until {when}); iNaturalist may have "
        "revoked it or be having trouble"
    )


@dataclass
class INatUser:
    id: int
    login: str
    name: str = ""


@dataclass
class Taxon:
    id: int
    name: str
    rank: str
    common_name: str = ""
    iconic: str = ""  # iNaturalist's broad group: "Fungi", "Plantae", ...

    @property
    def label(self) -> str:
        if self.common_name:
            return f"{self.name} ({self.common_name})"
        return self.name


@dataclass
class MapObservation:
    """One of the user's observations already on iNaturalist, for the map."""

    id: int
    lat: float
    lon: float
    name: str = ""
    observed_on: str = ""
    photo_url: str = ""  # the square (75 px) thumbnail; "" when it has no photo

    def photo(self, size: str) -> str:
        """The photo at another of iNaturalist's sizes ("small", "medium")."""
        return re.sub(r"/square(\.\w+)(\?.*)?$", rf"/{size}\1\2", self.photo_url)


@dataclass
class ObservationDraft:
    observed_on_string: Optional[str]  # "YYYY-MM-DD HH:MM:SS" (local, no tz)
    latitude: Optional[float]
    longitude: Optional[float]
    positional_accuracy_m: Optional[int]
    geoprivacy: str = "open"
    taxon_id: Optional[int] = None
    species_guess: str = ""
    description: str = ""
    tags: str = ""

    def payload(self) -> dict[str, Any]:
        obs: dict[str, Any] = {}
        if self.observed_on_string:
            obs["observed_on_string"] = self.observed_on_string
        if self.latitude is not None and self.longitude is not None:
            obs["latitude"] = round(self.latitude, 6)
            obs["longitude"] = round(self.longitude, 6)
            if self.positional_accuracy_m:
                obs["positional_accuracy"] = int(self.positional_accuracy_m)
        if self.geoprivacy in GEOPRIVACY_CHOICES and self.geoprivacy != "open":
            obs["geoprivacy"] = self.geoprivacy
        if self.taxon_id:
            obs["taxon_id"] = int(self.taxon_id)
        if self.species_guess:
            obs["species_guess"] = self.species_guess
        if self.description:
            obs["description"] = self.description
        if self.tags:
            obs["tag_list"] = self.tags
        return {"observation": obs}


class INatClient:
    def __init__(self, token: str, http: HttpClient, base_url: str = BASE_URL):
        self._token = normalise_token(token)
        self._http = http
        self._base = base_url.rstrip("/")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": self._token}

    def me(self) -> INatUser:
        data = self._http.request("GET", f"{self._base}/users/me", headers=self._auth())
        row = _first_result(data)
        if not row or "login" not in row:
            raise ApiError(status=401, code="invalid_token", detail="No user returned")
        return INatUser(
            id=int(row.get("id") or 0),
            login=str(row.get("login") or ""),
            name=str(row.get("name") or ""),
        )

    def taxa_autocomplete(self, query: str, per_page: int = 10) -> list[Taxon]:
        query = inat_taxon_name(query)
        if not query:
            return []
        data = self._http.request(
            "GET",
            f"{self._base}/taxa/autocomplete",
            # Active taxa only: a name that was merged or split away must not
            # be attached to a new observation.
            query={"q": query, "per_page": per_page, "is_active": "true"},
            timeout=30,
        )
        out: list[Taxon] = []
        for row in (data or {}).get("results", []) if isinstance(data, dict) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            out.append(
                Taxon(
                    id=int(row["id"]),
                    name=str(row.get("name") or ""),
                    rank=str(row.get("rank") or ""),
                    common_name=str(row.get("preferred_common_name") or ""),
                    iconic=str(row.get("iconic_taxon_name") or ""),
                )
            )
        return out

    def resolve_taxon(self, name: str, rank: str = "") -> Optional[Taxon]:
        """Exact (case-insensitive) scientific-name match, or ``None``.

        The same name can exist at two ranks or in two kingdoms. When
        ``rank`` is given, a match at that rank wins over the first hit.
        """
        name = inat_taxon_name(name)
        if not name:
            return None
        wanted = name.casefold()
        matches = [
            t for t in self.taxa_autocomplete(name) if t.name.casefold() == wanted
        ]
        if not matches:
            return None
        rank = (rank or "").strip().casefold()
        if rank:
            for taxon in matches:
                if taxon.rank.casefold() == rank:
                    return taxon
        return matches[0]

    def observations_in_box(
        self,
        login: str,
        swlat: float,
        swlng: float,
        nelat: float,
        nelng: float,
        per_page: int = 200,
        authenticated: bool = True,
    ) -> tuple[list[MapObservation], int]:
        """``login``'s observations inside a bounding box, newest first.

        Returns (observations, how many iNaturalist has there in total).
        With the token sent, the user's own obscured and private
        observations come back with their true coordinates.
        """
        data = self._http.request(
            "GET",
            f"{self._base}/observations",
            headers=self._auth() if authenticated and self._token else None,
            query={
                "user_login": login,
                "swlat": round(swlat, 6),
                "swlng": round(swlng, 6),
                "nelat": round(nelat, 6),
                "nelng": round(nelng, 6),
                "per_page": per_page,
                "order_by": "observed_on",
                "order": "desc",
            },
            timeout=60,
        )
        rows = data.get("results", []) if isinstance(data, dict) else []
        out: list[MapObservation] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            point = _lat_lon(row.get("private_location"))
            if point is None and not row.get("obscured"):
                # Without the owner's token an obscured one has a made-up
                # point: no marker rather than one in the wrong place.
                point = _lat_lon(row.get("location"))
            if point is None:
                continue
            taxon = row.get("taxon") if isinstance(row.get("taxon"), dict) else {}
            photos = row.get("photos") or []
            first = photos[0] if photos and isinstance(photos[0], dict) else {}
            url = str(first.get("url") or "")
            out.append(
                MapObservation(
                    id=int(row["id"]),
                    lat=point[0],
                    lon=point[1],
                    name=str(taxon.get("name") or row.get("species_guess") or ""),
                    observed_on=str(row.get("observed_on") or ""),
                    photo_url=url if url.startswith("https://") else "",
                )
            )
        try:
            total = int(data.get("total_results"))
        except (AttributeError, TypeError, ValueError):
            total = len(out)
        return out, total

    def observations_between(
        self,
        login: str,
        first: date,
        last: date,
        per_page: int = 200,
        max_pages: int = 5,
        authenticated: bool = True,
    ) -> tuple[list[ExistingObservation], bool]:
        """``login``'s observations observed from ``first`` to ``last``
        (inclusive), oldest first, up to ``max_pages`` pages.

        Returns (observations, complete): ``complete`` is False when
        iNaturalist has more than ``max_pages`` pages there, so the newest
        ones were not fetched.  With the token sent, the user's own obscured
        and private observations come back with their true coordinates.
        """
        out: list[ExistingObservation] = []
        complete = False
        for page in range(1, max_pages + 1):
            data = self._http.request(
                "GET",
                f"{self._base}/observations",
                headers=self._auth() if authenticated and self._token else None,
                query={
                    "user_login": login,
                    "d1": first.isoformat(),
                    "d2": last.isoformat(),
                    "per_page": per_page,
                    "page": page,
                    "order_by": "observed_on",
                    "order": "asc",
                },
                timeout=60,
            )
            rows = data.get("results", []) if isinstance(data, dict) else []
            for row in rows:
                if isinstance(row, dict):
                    obs = observation_from_row(row)
                    if obs is not None:
                        out.append(obs)
            try:
                total = int(data.get("total_results"))
            except (AttributeError, TypeError, ValueError):
                total = 0
            if len(rows) < per_page or page * per_page >= total:
                complete = True
                break
        return out, complete

    def create_observation(self, draft: ObservationDraft) -> int:
        data = self._http.request(
            "POST",
            f"{self._base}/observations",
            headers=self._auth(),
            json_body=draft.payload(),
        )
        row = _first_result(data)
        obs_id = (row or {}).get("id")
        if not obs_id:
            raise ApiError(status=500, code="no_id", detail="Observation id missing")
        return int(obs_id)

    def add_photo(
        self,
        observation_id: int,
        filename: str,
        data: Union[Path, bytes],
        position: Optional[int] = None,
    ) -> Optional[int]:
        """Attach a photo. ``position`` (0 = first) is its place in the
        observation, so a photo attached on a later retry still lands where
        the card shows it."""
        fields = {"observation_photo[observation_id]": int(observation_id)}
        if position is not None:
            fields["observation_photo[position]"] = int(position)
        files = [("file", (filename, data, "image/jpeg"))]
        resp = self._http.request(
            "POST",
            f"{self._base}/observation_photos",
            headers=self._auth(),
            multipart=(fields, files),
            timeout=300,
        )
        row = _first_result(resp)
        try:
            return int((row or {}).get("id"))
        except (TypeError, ValueError):
            return None


    def set_photo_position(self, observation_photo_id: int, position: int) -> None:
        """Move a photo already on an observation to ``position`` (0 = first)."""
        self._http.request(
            "PUT",
            f"{self._base}/observation_photos/{int(observation_photo_id)}",
            headers=self._auth(),
            multipart=({"observation_photo[position]": int(position)}, []),
            timeout=60,
        )


def matches_abbreviation(query: str, name: str) -> bool:
    """True when each typed word starts the same word of a scientific name.

    "p semilanc" and "psil sem" both match "Psilocybe semilanceata"; a single
    word matches the first word only ("amanita" matches the genus and its
    species, not "Russula amanita").
    """
    typed = query.casefold().split()
    words = name.casefold().split()
    return (
        bool(typed)
        and len(words) >= len(typed)
        and all(word.startswith(part) for word, part in zip(words, typed))
    )


def rank_suggestions(query: str, taxa: Sequence[Taxon]) -> list[Taxon]:
    """Order autocomplete results for someone typing a scientific name.

    iNaturalist also matches common names and words in the middle of a name,
    so "p cub" returns a kingfisher and a palm ahead of Psilocybe cubensis.
    An exact name comes first, then names the typed words abbreviate (fungi
    ahead of the rest: this dialog identifies with a fungus model), then
    everything else in iNaturalist's own order.
    """
    # As searched: "var." and the like dropped (see inat_taxon_name).
    query = inat_taxon_name(query)
    wanted = " ".join(query.casefold().split())

    def key(item: tuple[int, Taxon]):
        index, taxon = item
        if taxon.name.casefold() == wanted:
            group = 0
        elif matches_abbreviation(query, taxon.name):
            group = 1 if taxon.iconic == "Fungi" else 2
        else:
            group = 3
        return (group, index)

    seen: set[int] = set()
    out: list[Taxon] = []
    for _, taxon in sorted(enumerate(taxa), key=key):
        if taxon.id not in seen:
            seen.add(taxon.id)
            out.append(taxon)
    return out


def _first_result(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return results[0] if results and isinstance(results[0], dict) else None
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _lat_lon(text: Any) -> Optional[tuple[float, float]]:
    """iNaturalist's "lat,lon" string as numbers; None when it is not one."""
    try:
        lat, lon = (float(part) for part in str(text).split(","))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# Every token problem message starts with this (the upload resumes on a new
# token when a card failed with it).
TOKEN_REJECTED = "iNaturalist rejected the API token"


def describe_error(exc: ApiError, token: Optional[str] = None) -> str:
    """A message for a failed iNaturalist call. With the ``token`` that was
    sent, a 401 says why: iNaturalist itself only answers "Unauthorized"."""
    if exc.status == 401:
        why = describe_token(token) if token is not None else ""
        if why:
            return f"{TOKEN_REJECTED}: {why}. Get a fresh one and paste it in Settings."
        return f"{TOKEN_REJECTED} (they expire after 24 h). Paste a fresh one."
    if exc.status == 422:
        return f"iNaturalist refused the observation: {exc.detail or 'invalid fields'}"
    if exc.status == 429:
        return "iNaturalist rate limit hit; wait a minute and retry."
    if exc.status >= 500:
        return f"iNaturalist server error ({exc.status}); retry later."
    return f"iNaturalist error: {exc}"
