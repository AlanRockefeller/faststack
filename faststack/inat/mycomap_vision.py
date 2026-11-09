"""MycoMap Vision v1 client: identify one observation from its photos.

Contract: https://github.com/BiodiverseLabs/mycomap-vision/blob/main/docs/api.md
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from faststack.inat.http import ApiError, HttpClient

log = logging.getLogger(__name__)

BASE_URL = "https://vision.mycomap.org/api/v1"
MAX_PHOTOS = 10


@dataclass
class Candidate:
    rank: str
    name: str
    confidence: Optional[float]
    score: Optional[float]
    confident: bool
    reference_records: Optional[int] = None

    @property
    def confidence_pct(self) -> int:
        return int(round((self.confidence or 0.0) * 100))


@dataclass
class VisionResult:
    """What the card shows: the headline answer plus every rank's candidates."""

    answer_rank: Optional[str]
    answer_name: Optional[str]
    answer_confidence: Optional[float]
    species: list[Candidate] = field(default_factory=list)
    genus: list[Candidate] = field(default_factory=list)
    family: list[Candidate] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    gate_message: Optional[str] = None
    gate_verdict: Optional[str] = None
    model_version: str = ""
    photos_scored: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def suggested_name(self) -> str:
        """Vision's top name: the confident answer, else the best species,
        else the best genus."""
        if self.answer_name:
            return self.answer_name
        if self.species:
            return self.species[0].name
        if self.genus:
            return self.genus[0].name
        if self.family:
            return self.family[0].name
        return ""

    @property
    def suggested_rank(self) -> str:
        if self.answer_rank:
            return self.answer_rank
        if self.species:
            return "species"
        if self.genus:
            return "genus"
        if self.family:
            return "family"
        return ""

    def choices(self) -> list[Candidate]:
        """Every candidate, species first, for the pick list."""
        return list(self.species) + list(self.genus) + list(self.family)


@dataclass
class Usage:
    used: int
    limit: Optional[int]
    period: str
    resets_at: str
    plan: str = ""

    @property
    def text(self) -> str:
        if self.limit is None:
            return f"{self.used} identifications this period (unlimited)"
        return f"{self.used} / {self.limit} identifications used ({self.period})"


def client_ref_for(paths: Sequence[Path]) -> str:
    """Stable id for a set of files so a re-run within 24 h isn't counted twice."""
    h = hashlib.sha1()
    for p in sorted(str(x) for x in paths):
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\0")
    return "fs-" + h.hexdigest()[:40]


class MycoMapVisionClient:
    def __init__(self, token: str, http: HttpClient, base_url: str = BASE_URL):
        self._token = token.strip()
        self._http = http
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def usage(self) -> Usage:
        data = self._http.request("GET", f"{self._base}/usage", headers=self._headers())
        return parse_usage(data if isinstance(data, dict) else {})

    def identify(
        self,
        photos: Sequence[tuple[str, bytes]],
        *,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        observed_on: Optional[str] = None,
        client_ref: Optional[str] = None,
        top_k: int = 5,
    ) -> VisionResult:
        """``photos`` are (filename, jpeg_bytes), already shrunk (1024 px)."""
        if not photos:
            raise ValueError("identify() needs at least one photo")
        photos = list(photos)[:MAX_PHOTOS]
        fields: dict[str, Any] = {"top_k": top_k}
        if latitude is not None and longitude is not None:
            fields["latitude"] = f"{latitude:.6f}"
            fields["longitude"] = f"{longitude:.6f}"
        if observed_on:
            fields["observed_on"] = observed_on
        if client_ref:
            fields["client_ref"] = client_ref[:64]
        files = [("photos", (name, data, "image/jpeg")) for name, data in photos]
        data = self._http.request(
            "POST",
            f"{self._base}/identify",
            headers=self._headers(),
            multipart=(fields, files),
            timeout=300,
        )
        return parse_result(data if isinstance(data, dict) else {})


def parse_usage(data: dict[str, Any]) -> Usage:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    usage = usage or {}
    limit = usage.get("limit")
    return Usage(
        used=int(usage.get("used") or 0),
        limit=int(limit) if limit is not None else None,
        period=str(usage.get("period") or ""),
        resets_at=str(usage.get("resets_at") or ""),
        plan=str(usage.get("plan") or ""),
    )


def _candidates(rank: str, rows: Any) -> list[Candidate]:
    out: list[Candidate] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        out.append(
            Candidate(
                rank=rank,
                name=str(row["name"]),
                confidence=_float(row.get("confidence")),
                score=_float(row.get("score")),
                confident=bool(row.get("confident")),
                reference_records=(
                    int(row["reference_records"])
                    if row.get("reference_records") is not None
                    else None
                ),
            )
        )
    return out


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_result(data: dict[str, Any]) -> VisionResult:
    ranks = data.get("ranks") if isinstance(data.get("ranks"), dict) else {}
    answer = data.get("answer") if isinstance(data.get("answer"), dict) else {}
    gate = data.get("gate") if isinstance(data.get("gate"), dict) else {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    hints = [str(h) for h in data.get("hints") or [] if h]
    return VisionResult(
        answer_rank=answer.get("rank"),
        answer_name=answer.get("name"),
        answer_confidence=_float(answer.get("confidence")),
        species=_candidates("species", ranks.get("species")),
        genus=_candidates("genus", ranks.get("genus")),
        family=_candidates("family", ranks.get("family")),
        hints=hints,
        gate_message=gate.get("message"),
        gate_verdict=gate.get("verdict"),
        model_version=str(model.get("version") or ""),
        photos_scored=int(data.get("photos") or 0),
        usage=usage,
        raw=data,
    )


def describe_error(exc: ApiError) -> str:
    """A sentence for the card."""
    if exc.status == 401:
        return "MycoMap Vision rejected the API key. Check it in settings."
    if exc.status == 403 and exc.code == "key_inactive":
        return "This MycoMap Vision key is pending approval, disabled or revoked."
    if exc.status == 429 and exc.code == "quota_exceeded":
        resets = exc.extra.get("resets_at") or "next period"
        return f"MycoMap Vision quota used up; resets {resets}."
    if exc.status == 429:
        wait = f" Try again in {exc.retry_after}s." if exc.retry_after else ""
        return "MycoMap Vision rate limit hit." + wait
    if exc.status == 503:
        wait = f" (about {exc.retry_after}s)" if exc.retry_after else ""
        return f"MycoMap Vision is busy{wait}; try again shortly."
    if exc.status == 413:
        return "MycoMap Vision refused the upload as too large."
    return f"MycoMap Vision error: {exc}"
