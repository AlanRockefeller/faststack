"""Tiny stdlib HTTP helper shared by the MycoMap Vision and iNaturalist clients.

Kept on ``urllib`` so the frozen builds gain no new dependency (the updater
already uses it). A ``transport`` callable can be injected for tests.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union
from urllib import error, parse, request

log = logging.getLogger(__name__)

FilePart = tuple[str, Union[Path, bytes], Optional[str]]  # (filename, data, mime)


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


@dataclass
class ApiError(Exception):
    """A non-2xx reply, with the server's code/detail when it sent JSON."""

    status: int
    code: str = ""
    detail: str = ""
    retry_after: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        bits = [f"HTTP {self.status}"]
        if self.code:
            bits.append(self.code)
        if self.detail:
            bits.append(self.detail)
        return ": ".join(bits)


Transport = Callable[[str, str, dict[str, str], Optional[bytes], float], HttpResponse]


def _urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> HttpResponse:
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(),
            )
    except error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers={k.lower(): v for k, v in exc.headers.items()},
            body=exc.read() or b"",
        )


def _header_filename(filename: str) -> str:
    """A file name that cannot end or corrupt the part's header line.

    Local files may be called ``my "best" shot.jpg``; a raw quote would close
    the ``filename="..."`` value early and a line break would start a new
    header. Quotes are percent-encoded as browsers do (RFC 7578 4.2).
    """
    return str(filename).replace("\r", " ").replace("\n", " ").replace('"', "%22")


def encode_multipart(
    fields: dict[str, Any], files: Sequence[tuple[str, FilePart]]
) -> tuple[bytes, str]:
    """Return (body, content_type) for ``multipart/form-data``.

    ``files`` is a list of (field_name, (filename, Path-or-bytes, mime)).
    """
    boundary = f"----FastStack{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8")
        out += b"\r\n"
    for name, (filename, data, mime) in files:
        if isinstance(data, Path):
            data = data.read_bytes()
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{_header_filename(filename)}"\r\n'
        ).encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        transport: Optional[Transport] = None,
        timeout: float = 120.0,
    ):
        self.user_agent = user_agent
        self._transport = transport or _urllib_transport
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        json_body: Any = None,
        multipart: Optional[
            tuple[dict[str, Any], Sequence[tuple[str, FilePart]]]
        ] = None,
        query: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        body: Optional[bytes] = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif multipart is not None:
            body, ctype = encode_multipart(*multipart)
            hdrs["Content-Type"] = ctype
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            url = f"{url}?{parse.urlencode(clean)}"
        resp = self._transport(method, url, hdrs, body, timeout or self.timeout)
        if 200 <= resp.status < 300:
            return resp.json()
        raise self._error(resp)

    @staticmethod
    def _error(resp: HttpResponse) -> ApiError:
        data = resp.json()
        code = ""
        detail = ""
        retry_after: Optional[int] = None
        extra: dict[str, Any] = {}
        if isinstance(data, dict):
            code = str(data.get("code") or data.get("error") or "").strip()
            detail = str(data.get("detail") or data.get("message") or "").strip()
            errors = data.get("errors")
            if not detail and errors:
                detail = str(errors)
            try:
                retry_after = int(data.get("retry_after_seconds"))
            except (TypeError, ValueError):
                retry_after = None
            extra = {k: v for k, v in data.items() if k not in ("code", "detail")}
        elif resp.body:
            detail = resp.body[:200].decode("utf-8", errors="replace")
        if retry_after is None:
            try:
                retry_after = int(resp.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
        return ApiError(
            status=resp.status,
            code=code,
            detail=detail,
            retry_after=retry_after,
            extra=extra,
        )
