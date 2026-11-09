import json
from pathlib import Path

import pytest
from PIL import Image

from faststack.inat import images
from faststack.inat.credentials import CredentialStore
from faststack.inat.http import ApiError, HttpClient, HttpResponse, encode_multipart
from faststack.inat.inat_client import INatClient, ObservationDraft, normalise_token
from faststack.inat.mycomap_vision import (
    MycoMapVisionClient,
    client_ref_for,
    describe_error,
    parse_result,
)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        status, payload = self.responses.pop(0)
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        return HttpResponse(status=status, headers={}, body=payload)


def _client(responses):
    t = FakeTransport(responses)
    return HttpClient("FastStack-test", transport=t), t


def test_multipart_encoding_includes_fields_and_files():
    body, ctype = encode_multipart(
        {"top_k": 5, "skip": None}, [("photos", ("a.jpg", b"\xff\xd8", "image/jpeg"))]
    )
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="top_k"\r\n\r\n5' in body
    assert b"skip" not in body
    assert b'filename="a.jpg"' in body
    assert b"Content-Type: image/jpeg" in body
    assert body.endswith(b"--\r\n")


def test_http_error_parsing_uses_json_code_and_retry():
    http, _ = _client(
        [
            (
                429,
                {
                    "code": "quota_exceeded",
                    "detail": "used up",
                    "resets_at": "2026-12-01",
                },
            )
        ]
    )
    with pytest.raises(ApiError) as info:
        http.request("GET", "https://x/y")
    err = info.value
    assert err.status == 429 and err.code == "quota_exceeded"
    assert err.extra["resets_at"] == "2026-12-01"
    assert "quota" in describe_error(err).lower()


def test_vision_identify_sends_photos_and_context():
    http, transport = _client(
        [
            (
                200,
                {
                    "answer": {
                        "rank": "species",
                        "name": "Amanita muscaria",
                        "confidence": 0.8,
                    },
                    "ranks": {
                        "species": [
                            {
                                "name": "Amanita muscaria",
                                "confidence": 0.8,
                                "score": 0.9,
                                "confident": True,
                                "reference_records": 400,
                            },
                            {
                                "name": "Amanita flavoconia",
                                "confidence": 0.1,
                                "score": 0.3,
                                "confident": False,
                            },
                        ],
                        "genus": [
                            {"name": "Amanita", "confidence": 0.95, "confident": True}
                        ],
                        "family": [
                            {
                                "name": "Amanitaceae",
                                "confidence": 0.96,
                                "confident": True,
                            }
                        ],
                    },
                    "gate": {"verdict": "fungus", "message": None},
                    "hints": ["Photograph the stem base."],
                    "model": {"version": "2026-10-13"},
                    "photos": 2,
                    "usage": {"used": 3, "limit": 300},
                },
            )
        ]
    )
    client = MycoMapVisionClient("vision_abc", http)
    result = client.identify(
        [("a.jpg", b"AAA"), ("b.jpg", b"BBB")],
        latitude=37.8,
        longitude=-122.2,
        observed_on="2026-11-01",
        client_ref="fs-1",
    )
    method, url, headers, body = transport.calls[0]
    assert method == "POST" and url.endswith("/api/v1/identify")
    assert headers["Authorization"] == "Bearer vision_abc"
    assert b'name="latitude"\r\n\r\n37.800000' in body
    assert b'name="observed_on"\r\n\r\n2026-11-01' in body
    assert body.count(b'name="photos"') == 2
    assert result.suggested_name == "Amanita muscaria"
    assert result.suggested_rank == "species"
    assert result.species[0].confident and result.species[0].confidence_pct == 80
    assert [c.name for c in result.choices()] == [
        "Amanita muscaria",
        "Amanita flavoconia",
        "Amanita",
        "Amanitaceae",
    ]
    assert result.hints == ["Photograph the stem base."]
    assert result.usage["used"] == 3


def test_vision_result_without_confident_answer_falls_back_to_genus():
    result = parse_result(
        {
            "answer": None,
            "ranks": {"species": [], "genus": [{"name": "Russula", "confidence": 0.4}]},
        }
    )
    assert result.suggested_name == "Russula"
    assert result.suggested_rank == "genus"


def test_client_ref_is_stable_and_order_independent():
    a = client_ref_for([Path("/p/1.jpg"), Path("/p/2.jpg")])
    b = client_ref_for([Path("/p/2.jpg"), Path("/p/1.jpg")])
    assert a == b and a.startswith("fs-") and len(a) <= 64


def test_normalise_token_extracts_jwt_from_paste():
    assert normalise_token('{"api_token": "aaa.bbb.ccc"}') == "aaa.bbb.ccc"
    assert normalise_token("  xyz  ") == "xyz"


def test_inat_create_observation_and_photo():
    http, transport = _client(
        [
            (200, {"results": [{"id": 7, "login": "alan", "name": "Alan R"}]}),
            (
                200,
                {
                    "results": [
                        {
                            "id": 48715,
                            "name": "Amanita muscaria",
                            "rank": "species",
                            "preferred_common_name": "Fly Agaric",
                        }
                    ]
                },
            ),
            (200, {"id": 123456789, "uuid": "u"}),
            (200, {"id": 999}),
        ]
    )
    client = INatClient("tok.en.jwt", http)
    me = client.me()
    assert me.login == "alan" and me.id == 7
    assert transport.calls[0][2]["Authorization"] == "tok.en.jwt"

    taxon = client.resolve_taxon("amanita MUSCARIA")
    assert (
        taxon and taxon.id == 48715 and taxon.label == "Amanita muscaria (Fly Agaric)"
    )

    draft = ObservationDraft(
        observed_on_string="2026-11-01 10:15:00",
        latitude=37.87,
        longitude=-122.27,
        positional_accuracy_m=15,
        geoprivacy="obscured",
        taxon_id=taxon.id,
        species_guess=taxon.name,
        description="Under oaks",
    )
    obs_id = client.create_observation(draft)
    assert obs_id == 123456789
    sent = json.loads(transport.calls[2][3])
    assert sent == {
        "observation": {
            "observed_on_string": "2026-11-01 10:15:00",
            "latitude": 37.87,
            "longitude": -122.27,
            "positional_accuracy": 15,
            "geoprivacy": "obscured",
            "taxon_id": 48715,
            "species_guess": "Amanita muscaria",
            "description": "Under oaks",
        }
    }
    photo_id = client.add_photo(obs_id, "IMG_1.jpg", b"\xff\xd8")
    assert photo_id == 999
    body = transport.calls[3][3]
    assert b'name="observation_photo[observation_id]"\r\n\r\n123456789' in body
    assert b'name="file"; filename="IMG_1.jpg"' in body


def test_draft_omits_open_geoprivacy_and_empty_fields():
    payload = ObservationDraft(None, None, None, None).payload()
    assert payload == {"observation": {}}


def _make_jpeg(path: Path, size=(3000, 2000)):
    img = Image.new("RGB", size, (120, 80, 40))
    exif = Image.Exif()
    exif[0x0110] = "TestCam"
    exif[0x0112] = 6  # orientation
    img.save(path, "JPEG", quality=90, exif=exif.tobytes())


def test_prepare_jpeg_resizes_and_keeps_exif(tmp_path):
    src = tmp_path / "big.jpg"
    _make_jpeg(src)
    out = images.prepare_jpeg(src, 1024)
    with Image.open(__import__("io").BytesIO(out)) as img:
        assert max(img.size) == 1024
        assert img.getexif()[0x0110] == "TestCam"
        assert img.getexif()[0x0112] == 6


def test_prepare_jpeg_leaves_small_files_alone(tmp_path):
    src = tmp_path / "small.jpg"
    _make_jpeg(src, size=(800, 600))
    assert images.prepare_jpeg(src, 1024) == src.read_bytes()


def test_credential_store_file_fallback(tmp_path):
    store = CredentialStore(fallback_dir=tmp_path)
    store._keyring_checked = True  # force the file path
    store._keyring = None
    assert store.get("x") == ""
    store.set("x", " secret ")
    assert store.get("x") == "secret"
    assert (tmp_path / "secrets.json").exists()
    store.set("x", "")
    assert store.get("x") == ""
    assert not (tmp_path / "secrets.json").exists()
