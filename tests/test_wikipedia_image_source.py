"""Tests for WikipediaImageSource's license lookup (engine/render/asset_sourcer.py).

No existing test file covered this class at all before this fix — see
docs/specs/2026-09-wikipedia-license-url-decode-system-design.md.
"""
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import models
from engine.render.asset_sourcer import SourcedAsset, WikipediaImageSource, _cache_asset
from tests.test_sourcer_selection import _wikipedia_downloads_via_get_fakes  # noqa: F401  (autouse fixture)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _license_response(license_short="cc-by"):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "query": {
            "pages": {
                "1": {
                    "imageinfo": [{
                        "extmetadata": {
                            "LicenseShortName": {"value": license_short},
                            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                            "Artist": {"value": "Some Photographer"},
                        }
                    }]
                }
            }
        }
    }
    return resp


def test_fetch_license_forwards_an_already_decoded_title_verbatim():
    """_fetch_license() itself does no decoding -- decoding happens once, in
    search(), before it calls this method (see
    test_search_decodes_the_originalimage_filename_before_the_license_lookup
    for the actual regression coverage of the decode step). This test only
    confirms _fetch_license()'s own contract: whatever title string it's
    given goes into the titles= param unmodified, with no double-decoding
    or other mangling on the way in."""
    source = WikipediaImageSource.__new__(WikipediaImageSource)
    source._SEARCH = WikipediaImageSource._SEARCH
    source._HEADERS = WikipediaImageSource._HEADERS

    decoded = "Lionel_Messi_-_2022146154227 (cropped).jpg"

    with patch("engine.render.asset_sourcer.httpx.get", return_value=_license_response()) as mock_get:
        source._fetch_license(decoded)

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["titles"] == f"File:{decoded}"


def test_search_decodes_the_originalimage_filename_before_the_license_lookup(tmp_path):
    """End-to-end through search(): an accented/spaced originalimage URL must
    result in a DECODED titles= param, not the raw percent-encoded segment."""
    source = WikipediaImageSource(tmp_path)

    opensearch_resp = MagicMock()
    opensearch_resp.raise_for_status.return_value = None
    opensearch_resp.json.return_value = ["Lionel Messi", ["Lionel Messi (cropped)"]]

    summary_resp = MagicMock()
    summary_resp.raise_for_status.return_value = None
    summary_resp.json.return_value = {
        "originalimage": {
            "source": "https://upload.wikimedia.org/wikipedia/commons/x/xx/"
                       "Lionel_Messi_-_2022146154227%20%28cropped%29.jpg"
        },
        "thumbnail": {},
        "pageid": 12345,
    }

    license_resp = _license_response()
    image_resp = MagicMock()
    image_resp.status_code = 200
    image_resp.raise_for_status.return_value = None
    image_resp.content = b"fake-image-bytes"

    with patch(
        "engine.render.asset_sourcer.httpx.get",
        side_effect=[opensearch_resp, summary_resp, license_resp, image_resp],
    ) as mock_get:
        result = source.search("Lionel Messi")

    assert result is not None
    assert result.safe_to_publish is True
    license_call_kwargs = mock_get.call_args_list[2].kwargs
    assert license_call_kwargs["params"]["titles"] == "File:Lionel_Messi_-_2022146154227 (cropped).jpg"


def test_fetch_license_passes_through_an_already_plain_filename_unchanged():
    """No percent-encoding present -> unquote() is a no-op; a working lookup
    must stay working after this fix (regression guard, not new behavior)."""
    source = WikipediaImageSource.__new__(WikipediaImageSource)
    source._SEARCH = WikipediaImageSource._SEARCH
    source._HEADERS = WikipediaImageSource._HEADERS

    plain = "Diego_Maradona_1986.jpg"
    with patch("engine.render.asset_sourcer.httpx.get", return_value=_license_response()) as mock_get:
        source._fetch_license(plain)

    assert mock_get.call_args.kwargs["params"]["titles"] == f"File:{plain}"


def test_cache_asset_self_heals_an_existing_row_once_a_fresh_search_finds_a_real_license(db):
    """A row broken by the pre-fix bug (license_url=None, safe_to_publish=False)
    is retroactively updated the next time _cache_asset() sees a richer result
    for the same (source, source_ref) -- see the design doc's §4 correction."""
    broken = models.Asset(
        type="photo", source="wikipedia", source_ref="12345",
        local_path="/tmp/wiki_12345.jpg", license="unknown",
        license_url=None, attribution=None, safe_to_publish=False,
    )
    db.add(broken)
    db.flush()

    fresh_result = SourcedAsset(
        source="wikipedia", source_ref="12345", local_path=broken.local_path,
        license_str="cc-by", license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Some Photographer", safe_to_publish=True, duration_s=0.0,
    )
    asset, _ = _cache_asset(db, fresh_result, "photo")

    assert asset.id == broken.id
    assert asset.safe_to_publish is True
    assert asset.license_url == "https://creativecommons.org/licenses/by/4.0/"
    assert asset.attribution == "Some Photographer"


def test_cache_asset_does_not_downgrade_a_row_that_already_has_a_real_license(db):
    """Guard the other direction: an existing row with real license data must
    not be clobbered by a later, degraded ("unknown") result -- the `if
    result.license_url and not existing.license_url` condition only fires
    when the EXISTING row is the one missing data."""
    good = models.Asset(
        type="photo", source="wikipedia", source_ref="999",
        local_path="/tmp/wiki_999.jpg", license="cc-by",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution="Original Photographer", safe_to_publish=True,
    )
    db.add(good)
    db.flush()

    degraded_result = SourcedAsset(
        source="wikipedia", source_ref="999", local_path=good.local_path,
        license_str="unknown", license_url=None, attribution=None,
        safe_to_publish=False, duration_s=0.0,
    )
    asset, _ = _cache_asset(db, degraded_result, "photo")

    assert asset.safe_to_publish is True
    assert asset.license_url == "https://creativecommons.org/licenses/by/4.0/"
