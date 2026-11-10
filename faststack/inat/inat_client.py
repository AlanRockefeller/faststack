"""iNaturalist API v1 client: verify a token, look up taxa, create observations.

Auth is the 24-hour API token from https://www.inaturalist.org/users/api_token
sent bare in the ``Authorization`` header, as the Observation-Workbench app
does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, Union

from faststack.inat.http import ApiError, HttpClient

log = logging.getLogger(__name__)

BASE_URL = "https://api.inaturalist.org/v1"
API_TOKEN_URL = "https://www.inaturalist.org/users/api_token"
OBSERVATION_URL = "https://www.inaturalist.org/observations/{id}"

GEOPRIVACY_CHOICES = ("open", "obscured", "private")

# A JWT is three base64url runs joined by dots; pulling it out of whatever the
# user pasted (JSON, quotes, a label) avoids a silently broken credential.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)(?![A-Za-z0-9_-])"
)


def normalise_token(raw: str) -> str:
    text = (raw or "").strip()
    match = _JWT_RE.search(text)
    return match.group(1) if match else text.strip("\"' ")


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
        query = (query or "").strip()
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
        name = (name or "").strip()
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


def describe_error(exc: ApiError) -> str:
    if exc.status == 401:
        return "iNaturalist rejected the API token (they expire after 24 h). Paste a fresh one."
    if exc.status == 422:
        return f"iNaturalist refused the observation: {exc.detail or 'invalid fields'}"
    if exc.status == 429:
        return "iNaturalist rate limit hit; wait a minute and retry."
    if exc.status >= 500:
        return f"iNaturalist server error ({exc.status}); retry later."
    return f"iNaturalist error: {exc}"
