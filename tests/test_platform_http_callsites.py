"""Characterization of the fourteen platform API call sites (OAuth, YouTube, Instagram, metrics).

Each site is driven through its REAL caller; only the bare `httpx.get` / `httpx.post` / `httpx.put`
are replaced (by `tests/_platform_http_helpers.py::Recorder`, which answers through a real
`httpx.Client` over a `MockTransport`). For every site the test pins the full literal wire request:
method, URL and query, the caller-supplied headers by name, value and relative order, the body, the
exact keyword names sent to httpx and the timeout; and, for a 400, a 500 and a 302 answer with a
body that is not JSON, that `httpx.HTTPStatusError` propagates before the caller touches the body.

Because the file patches only bare httpx functions, it is meant to pass unmodified on the code
before and after the platform calls were gathered into one module (docs/specs/2026-11-platform-http-
module-design.md, section 6.3). Site numbers are those of the spec's section 4.3:

  1 YouTubeOAuth.exchange_code      6 InstagramOAuth.discover_account (page)   11 InstagramPublisher._wait_until_ready
  2 YouTubeOAuth.refresh            7 youtube._upload_captions                 12 InstagramPublisher._publish_container
  3 InstagramOAuth.exchange_code    8 YouTubePublisher.publish (init POST)     13 YouTubeMetricsFetcher.fetch
  4 InstagramOAuth.exchange_long..  9 YouTubePublisher.publish (video PUT)     14 InstagramMetricsFetcher.fetch
  5 InstagramOAuth.discover_account (/me/accounts)                             10 InstagramPublisher._create_container
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.oauth import InstagramOAuth, YouTubeOAuth
from engine.publish.instagram import InstagramPublisher
from engine.publish.metrics import InstagramMetricsFetcher, YouTubeMetricsFetcher
from engine.publish.youtube import YouTubePublisher, _upload_captions
from tests._platform_http_helpers import Recorder
from tests._platform_http_helpers import platform_network_guard  # noqa: F401  (autouse fixture)

# The names a secret must never travel under in a URL query (normalised: lower case, letters and
# digits only). A literal copy on purpose: the production constant does not exist yet at this commit.
SECRET_NAMES = frozenset({
    "accesstoken", "refreshtoken", "idtoken", "clientsecret", "secret", "fbexchangetoken",
    "apikey", "xgoogapikey", "key", "token", "code", "password", "auth", "authorization", "bearer",
    "signature", "sig",
    "oauthtoken", "apitoken", "authtoken", "sessiontoken", "securitytoken", "accesskey",
    "secretkey", "privatekey", "accesssecret", "jwt", "passwd", "pwd",
    "appsecret", "apisecret", "sessionid", "credential", "assertion", "clientassertion",
    "authorizationcode", "authkey",
})

# Distinct sentinels: every operand of every call gets its own value, so a swap is never invisible.
YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REDIRECT = "yt-client-id-111", "yt-client-secret-222", "https://app.example/cb/yt-333"
YT_CODE, YT_REFRESH_TOKEN = "yt-code-444", "yt-refresh-555"
IG_APP_ID, IG_APP_SECRET, IG_REDIRECT = "ig-app-id-666", "ig-app-secret-777", "https://app.example/cb/ig-888"
IG_CODE, IG_SHORT_TOKEN = "ig-code-999", "ig-short-token-1010"
USER_TOKEN = "ig-user-token-1111"
PAGE_1, PAGE_1_TOKEN = "page-id-1212", "ig-page-token-1313"
PAGE_2, PAGE_2_TOKEN = "page-id-1414", "ig-page-token-1515"
IG_USER_FROM_PAGE = "ig-business-1616"
CAPTION_VIDEO_ID, CAPTION_TOKEN = "yt-video-1717", "yt-caption-token-1818"
BOUNDARY_HEX = "0123456789abcdef0123456789abcdef"
PUBLISH_TOKEN = "yt-publish-token-1919"
SESSION_PATH = "/session/abc-2020"
SESSION_URL = f"https://upload.example.com{SESSION_PATH}?upload_id=UP-2121&upload_protocol=resumable"
UPLOADED_VIDEO_ID = "yt-uploaded-2222"
IG_USER, IG_TOKEN = "ig-user-2323", "ig-token-2424"
IG_VIDEO_URL, CREATION_ID, MEDIA_ID = "https://reels.example/video-2525.mp4", "creation-2626", "media-2727"
YT_METRICS_VIDEO, YT_METRICS_TOKEN = "yt-metrics-video-2828", "yt-metrics-token-2929"
IG_METRICS_POST, IG_METRICS_TOKEN = "ig-metrics-post-3030", "ig-metrics-token-3131"

_NONE = object()


# ── the answer side ────────────────────────────────────────────────────────────────────────────────

def json_resp(payload) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(200, json=payload)


def failing(status: int) -> httpx.Response:
    """A failure whose body is NOT valid JSON (so a caller that reads the body first fails differently)."""
    if status == 302:
        return httpx.Response(302, headers={"Location": "https://other.example/"}, content=b"<html>moved</html>")
    return httpx.Response(status, content=b"<html>not json</html>")


def make_handler(routes: dict, fail: tuple[tuple[str, str], int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if fail is not None and key == fail[0]:
            return failing(fail[1])
        if key not in routes:
            raise AssertionError(f"unexpected request {key}")
        return routes[key](request)

    return handler


# ── the expectation side ───────────────────────────────────────────────────────────────────────────

@dataclass
class Exp:
    method: str
    url: str                                # the URL passed to httpx (no query unless `params` is None)
    kw: frozenset                           # the exact keyword names sent to httpx
    timeout: float = 30.0
    params: dict | None = None
    headers: list | None = None             # [(name, value), ...] in order
    form: dict | None = None
    json_body: Any = _NONE
    content: Any = _NONE
    secrets: tuple = ()                     # sentinels that must not appear anywhere in the URL


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def check_call(rec: Recorder, i: int, exp: Exp) -> None:
    call, req = rec.calls[i], rec.requests[i]
    where = f"call {i} ({exp.method} {exp.url})"
    assert call.method == exp.method, where
    assert call.url == exp.url, where
    assert set(call.kwargs) == set(exp.kw), where
    timeout = call.kwargs["timeout"]
    assert type(timeout) is float and timeout == exp.timeout, where

    # wire: method and URL
    assert req.method == exp.method, where
    if exp.params is None:
        assert str(req.url) == exp.url, where
    else:
        assert str(req.url.copy_with(query=None)) == exp.url, where
        assert call.kwargs["params"] == exp.params, where
        assert dict(req.url.params) == exp.params and len(req.url.params.multi_items()) == len(exp.params), where

    # no secret-named key in the query, and no token value anywhere in the URL
    queried = list(req.url.params.keys()) + list((call.kwargs.get("params") or {}).keys())
    assert not [k for k in queried if _norm(k) in SECRET_NAMES], where
    for sentinel in exp.secrets:
        assert sentinel not in str(req.url) and sentinel not in call.url, where

    # headers: names, values and order as the caller supplied them
    if exp.headers is not None:
        assert list(call.kwargs["headers"].items()) == exp.headers, where
        for name, value in exp.headers:
            assert req.headers[name] == value, where
    if "authorization" not in {name.lower() for name, _ in (exp.headers or [])}:
        assert "authorization" not in req.headers, where
        assert "Authorization" not in (call.kwargs.get("headers") or {}), where

    # body
    if exp.form is not None:
        assert call.kwargs["data"] == exp.form, where
        pairs = parse_qsl(req.content.decode("ascii"), keep_blank_values=True, strict_parsing=True)
        assert dict(pairs) == exp.form and len(pairs) == len(exp.form), where
        assert req.headers["content-type"] == "application/x-www-form-urlencoded", where
    elif exp.json_body is not _NONE:
        assert call.kwargs["json"] == exp.json_body, where
        assert json.loads(req.content) == exp.json_body, where
    elif exp.content is not _NONE:
        assert call.kwargs["content"] == exp.content, where
        assert req.content == exp.content, where
    else:
        assert req.content == b"", where


# ── the cases ──────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Case:
    site: int
    drive: Callable[[], Any]
    routes: dict
    expected: list
    fail_route: tuple
    fail_index: int
    result: Callable[[Any], None] | None = None
    after_fail: Callable[[], None] | None = None


class Ctx:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    return Ctx(tmp_path, monkeypatch)


def _token_case(site, drive, host_url, path, form, payload):
    return Case(
        site=site,
        drive=drive,
        routes={("POST", path): json_resp(payload)},
        expected=[Exp("POST", host_url, frozenset({"data", "timeout"}), form=form)],
        fail_route=("POST", path),
        fail_index=0,
        result=lambda r: _eq(r, payload),
    )


def _eq(actual, expected):
    assert actual == expected


def site01(ctx):
    payload = {"access_token": "yt-at-1", "refresh_token": "yt-rt-1"}
    provider = YouTubeOAuth(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REDIRECT)
    return _token_case(
        1, lambda: provider.exchange_code(YT_CODE), "https://oauth2.googleapis.com/token", "/token",
        {"code": YT_CODE, "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
         "redirect_uri": YT_REDIRECT, "grant_type": "authorization_code"},
        payload)


def site02(ctx):
    payload = {"access_token": "yt-at-2", "expires_in": 3599}
    provider = YouTubeOAuth(YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REDIRECT)
    return _token_case(
        2, lambda: provider.refresh(YT_REFRESH_TOKEN), "https://oauth2.googleapis.com/token", "/token",
        {"refresh_token": YT_REFRESH_TOKEN, "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
         "grant_type": "refresh_token"},
        payload)


def site03(ctx):
    payload = {"access_token": "ig-at-3"}
    provider = InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT)
    return _token_case(
        3, lambda: provider.exchange_code(IG_CODE), "https://graph.facebook.com/v19.0/oauth/access_token",
        "/v19.0/oauth/access_token",
        {"client_id": IG_APP_ID, "client_secret": IG_APP_SECRET, "redirect_uri": IG_REDIRECT, "code": IG_CODE},
        payload)


def site04(ctx):
    payload = {"access_token": "ig-at-4", "expires_in": 5183944}
    provider = InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT)
    return _token_case(
        4, lambda: provider.exchange_long_lived_token(IG_SHORT_TOKEN),
        "https://graph.facebook.com/v19.0/oauth/access_token", "/v19.0/oauth/access_token",
        {"grant_type": "fb_exchange_token", "client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,
         "fb_exchange_token": IG_SHORT_TOKEN},
        payload)


def _discover(site, fail_route, fail_index):
    provider = InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT)
    routes = {
        ("GET", "/v19.0/me/accounts"): json_resp({"data": [
            {"id": PAGE_1, "access_token": PAGE_1_TOKEN},
            {"id": PAGE_2, "access_token": PAGE_2_TOKEN},
        ]}),
        ("GET", f"/v19.0/{PAGE_1}"): json_resp({"id": PAGE_1}),
        ("GET", f"/v19.0/{PAGE_2}"): json_resp({"instagram_business_account": {"id": IG_USER_FROM_PAGE}, "id": PAGE_2}),
    }
    expected = [
        Exp("GET", "https://graph.facebook.com/v19.0/me/accounts", frozenset({"headers", "timeout"}),
            headers=[("Authorization", f"Bearer {USER_TOKEN}")], secrets=(USER_TOKEN,)),
        Exp("GET", f"https://graph.facebook.com/v19.0/{PAGE_1}", frozenset({"params", "headers", "timeout"}),
            params={"fields": "instagram_business_account"},
            headers=[("Authorization", f"Bearer {PAGE_1_TOKEN}")], secrets=(PAGE_1_TOKEN, USER_TOKEN)),
        Exp("GET", f"https://graph.facebook.com/v19.0/{PAGE_2}", frozenset({"params", "headers", "timeout"}),
            params={"fields": "instagram_business_account"},
            headers=[("Authorization", f"Bearer {PAGE_2_TOKEN}")], secrets=(PAGE_2_TOKEN, USER_TOKEN)),
    ]
    return Case(
        site, lambda: provider.discover_account(USER_TOKEN), routes, expected, fail_route, fail_index,
        result=lambda r: _eq(r, {"page_id": PAGE_2, "page_access_token": PAGE_2_TOKEN, "ig_user_id": IG_USER_FROM_PAGE}),
    )


def site05(ctx):
    return _discover(5, ("GET", "/v19.0/me/accounts"), 0)


def site06(ctx):
    return _discover(6, ("GET", f"/v19.0/{PAGE_1}"), 1)


SRT_BYTES = b"1\n00:00:00,000 --> 00:00:01,000\nHello caption\n"


def _expected_multipart(video_id: str) -> bytes:
    """The literal body of captions.insert (RFC 2387 multipart/related), spelled out byte by byte."""
    b = BOUNDARY_HEX.encode()
    snippet = ('{"snippet": {"videoId": "%s", "language": "en", "name": "", "isDraft": false}}' % video_id).encode()
    return (
        b"--" + b + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + snippet + b"\r\n"
        + b"--" + b + b"\r\nContent-Type: application/octet-stream\r\n\r\n" + SRT_BYTES + b"\r\n"
        + b"--" + b + b"--"
    )


CAPTIONS_PATH = "/upload/youtube/v3/captions"
CAPTIONS_URL = "https://www.googleapis.com" + CAPTIONS_PATH


def _captions_exp(video_id: str, token: str) -> Exp:
    return Exp(
        "POST", CAPTIONS_URL, frozenset({"params", "headers", "content", "timeout"}),
        params={"uploadType": "multipart", "part": "snippet"},
        headers=[("Authorization", f"Bearer {token}"),
                 ("Content-Type", f"multipart/related; boundary={BOUNDARY_HEX}")],
        content=_expected_multipart(video_id), secrets=(token,),
    )


def _pin_boundary(ctx):
    ctx.monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(BOUNDARY_HEX))


def site07(ctx):
    srt = ctx.tmp_path / "captions.srt"
    srt.write_bytes(SRT_BYTES)
    _pin_boundary(ctx)
    return Case(
        7, lambda: _upload_captions(CAPTION_VIDEO_ID, str(srt), CAPTION_TOKEN),
        {("POST", CAPTIONS_PATH): json_resp({"id": "caption-track-1"})},
        [_captions_exp(CAPTION_VIDEO_ID, CAPTION_TOKEN)], ("POST", CAPTIONS_PATH), 0,
        result=lambda r: _eq(r, None),
    )


# Sites 8 and 9: YouTubePublisher.publish, in the three variants of the spec (section 6.3).
UPLOAD_PATH = "/upload/youtube/v3/videos"
UPLOAD_URL = "https://www.googleapis.com" + UPLOAD_PATH
VIDEO_BYTES = b"\x00fake-mp4-bytes-for-the-put-2929"

FIRST_A = "A-first-line-" + "f" * 120               # well over 100 characters
CAPTION_A = FIRST_A + "\nSecond short line A\n" + "z" * 5200   # well over 5000 characters
TAGS_A = [f"tag{i:02d}" for i in range(1, 21)]         # 20 hashtags
CAPTION_B = "  B title line\nB second line"           # leading spaces: the title is stripped, the description is not
TAGS_B = [f"h{i}" for i in range(1, 19)]               # 18 short hashtags
CAPTION_C = "   \n  "                                  # whitespace only


def _publish_expectations(variant: str):
    """(caption, hashtags, title, description, tags) for a variant."""
    if variant == "A":
        return CAPTION_A, TAGS_A, FIRST_A[:100], CAPTION_A[:5000], TAGS_A[:15]
    if variant == "B":
        description = CAPTION_B + "\n\n" + " ".join(f"#h{i}" for i in range(1, 16))
        return CAPTION_B, TAGS_B, "B title line", description, [f"h{i}" for i in range(1, 16)]
    if variant == "C":
        return CAPTION_C, None, "Reel", CAPTION_C, []
    if variant == "C-empty":
        return CAPTION_C, [], "Reel", CAPTION_C, []
    if variant == "D":                         # no caption at all
        return None, None, "Reel", "", []
    raise AssertionError(variant)


def _publish_cut(ctx, hashtags):
    video = ctx.tmp_path / "video.mp4"
    video.write_bytes(VIDEO_BYTES)
    cut = MagicMock()
    cut.id, cut.reel_id = 5, 10
    cut.video_path = str(video)
    cut.hashtags = hashtags
    cut.subtitle_path = None
    return cut


def _credential(token=PUBLISH_TOKEN):
    cred = MagicMock()
    cred.token_blob = token
    cred.refresh_token_blob = "unused-refresh"
    cred.expires_at = None            # not expired: no refresh call
    return cred


def _publish(ctx, variant: str, fail_index: int):
    caption, hashtags, title, description, tags = _publish_expectations(variant)
    cut = _publish_cut(ctx, hashtags)
    credential = _credential()
    size = str(len(VIDEO_BYTES))
    routes = {
        ("POST", UPLOAD_PATH): lambda request: httpx.Response(200, headers={"Location": SESSION_URL}),
        ("PUT", SESSION_PATH): json_resp({"id": UPLOADED_VIDEO_ID}),
    }
    expected = [
        Exp("POST", UPLOAD_URL, frozenset({"params", "headers", "json", "timeout"}),
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=[("Authorization", f"Bearer {PUBLISH_TOKEN}"),
                     ("Content-Type", "application/json; charset=UTF-8"),
                     ("X-Upload-Content-Type", "video/mp4"),
                     ("X-Upload-Content-Length", size)],
            json_body={"snippet": {"title": title, "description": description, "tags": tags},
                       "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
            secrets=(PUBLISH_TOKEN,)),
        Exp("PUT", SESSION_URL, frozenset({"headers", "content", "timeout"}), timeout=300.0,
            headers=[("Content-Type", "video/mp4"), ("Content-Length", size)],
            content=VIDEO_BYTES, secrets=(PUBLISH_TOKEN,)),
    ]

    def result(r):
        assert r.platform_post_id == UPLOADED_VIDEO_ID
        assert r.url == f"https://youtube.com/shorts/{UPLOADED_VIDEO_ID}"

    return Case(
        8 if fail_index == 0 else 9,
        lambda: YouTubePublisher().publish(cut, credential, MagicMock(), caption=caption),
        routes, expected, [("POST", UPLOAD_PATH), ("PUT", SESSION_PATH)][fail_index], fail_index, result=result,
    )


def site08(ctx):
    return _publish(ctx, "A", 0)


def site09(ctx):
    return _publish(ctx, "A", 1)


def publish_b(ctx):
    return _publish(ctx, "B", 1)


def publish_c(ctx):
    return _publish(ctx, "C", 1)


def publish_c_empty(ctx):
    return _publish(ctx, "C-empty", 1)


def publish_d(ctx):
    return _publish(ctx, "D", 1)


# Sites 10-12: Instagram.
LONG_CAPTION = "a" * 2200 + "Z" * 100                 # the 2200-character cap is observable


def site10(ctx):
    path = f"/v19.0/{IG_USER}/media"
    return Case(
        10, lambda: InstagramPublisher()._create_container(IG_USER, IG_TOKEN, IG_VIDEO_URL, LONG_CAPTION),
        {("POST", path): json_resp({"id": CREATION_ID})},
        [Exp("POST", f"https://graph.facebook.com/v19.0/{IG_USER}/media", frozenset({"data", "timeout"}),
             form={"media_type": "REELS", "video_url": IG_VIDEO_URL, "caption": "a" * 2200,
                   "access_token": IG_TOKEN})],
        ("POST", path), 0, result=lambda r: _eq(r, CREATION_ID),
    )


def site11(ctx):
    ctx.monkeypatch.setattr(time, "sleep", lambda seconds: None)    # never wait out the poll interval
    path = f"/v19.0/{CREATION_ID}"
    return Case(
        11, lambda: InstagramPublisher()._wait_until_ready(CREATION_ID, IG_TOKEN),
        {("GET", path): json_resp({"status_code": "FINISHED"})},
        [Exp("GET", f"https://graph.facebook.com/v19.0/{CREATION_ID}", frozenset({"params", "headers", "timeout"}),
             params={"fields": "status_code"}, headers=[("Authorization", f"Bearer {IG_TOKEN}")],
             secrets=(IG_TOKEN,))],
        ("GET", path), 0, result=lambda r: _eq(r, None),
    )


def site12(ctx):
    path = f"/v19.0/{IG_USER}/media_publish"
    return Case(
        12, lambda: InstagramPublisher()._publish_container(IG_USER, IG_TOKEN, CREATION_ID),
        {("POST", path): json_resp({"id": MEDIA_ID})},
        [Exp("POST", f"https://graph.facebook.com/v19.0/{IG_USER}/media_publish", frozenset({"data", "timeout"}),
             form={"creation_id": CREATION_ID, "access_token": IG_TOKEN})],
        ("POST", path), 0, result=lambda r: _eq(r, MEDIA_ID),
    )


# Sites 13 and 14: metrics.
def _metrics_credential(token):
    cred = MagicMock()
    cred.token_blob = token
    cred.expires_at = None
    return cred


def site13(ctx):
    cut = MagicMock()
    cut.platform_post_id = YT_METRICS_VIDEO
    stats = {"viewCount": "1500", "likeCount": "42", "commentCount": "3"}

    def result(r):
        assert (r.views, r.likes, r.comments) == (1500, 42, 3)

    return Case(
        13, lambda: YouTubeMetricsFetcher().fetch(cut, _metrics_credential(YT_METRICS_TOKEN), MagicMock()),
        {("GET", "/youtube/v3/videos"): json_resp({"items": [{"statistics": stats}]})},
        [Exp("GET", "https://www.googleapis.com/youtube/v3/videos", frozenset({"params", "headers", "timeout"}),
             params={"part": "statistics", "id": YT_METRICS_VIDEO},
             headers=[("Authorization", f"Bearer {YT_METRICS_TOKEN}")], secrets=(YT_METRICS_TOKEN,))],
        ("GET", "/youtube/v3/videos"), 0, result=result,
    )


def _db_with_cut(post_id=IG_METRICS_POST):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id, platform=models.CutPlatform.instagram_reels,
        status=models.CutStatus.published, platform_post_id=post_id,
    )
    db.add(cut)
    db.commit()
    return db, reel, cut


IG_INSIGHTS = [
    {"name": "plays", "values": [{"value": 500}]},
    {"name": "likes", "values": [{"value": 30}]},
    {"name": "comments", "values": [{"value": 4}]},
]


def site14(ctx):
    db, reel, cut = _db_with_cut()
    path = f"/v19.0/{IG_METRICS_POST}/insights"

    def result(r):
        assert (r.views, r.likes, r.comments) == (500, 30, 4)
        events = db.query(models.StageEvent).all()
        assert len(events) == 1
        assert (events[0].stage, events[0].ok, events[0].detail) == ("instagram_metrics", True, {})
        assert (events[0].reel_id, events[0].cut_id) == (reel.id, cut.id)

    def after_fail():
        events = db.query(models.StageEvent).all()
        assert len(events) == 1
        assert events[0].stage == "instagram_metrics" and events[0].ok is False
        assert "error" in events[0].detail
        assert (events[0].reel_id, events[0].cut_id) == (reel.id, cut.id)

    return Case(
        14, lambda: InstagramMetricsFetcher().fetch(cut, SimpleNamespace(token_blob=IG_METRICS_TOKEN), db),
        {("GET", path): json_resp({"data": IG_INSIGHTS})},
        [Exp("GET", f"https://graph.facebook.com/v19.0/{IG_METRICS_POST}/insights",
             frozenset({"params", "headers", "timeout"}),
             params={"metric": "plays,likes,comments"},
             headers=[("Authorization", f"Bearer {IG_METRICS_TOKEN}")], secrets=(IG_METRICS_TOKEN,))],
        ("GET", path), 0, result=result, after_fail=after_fail,
    )


SITES = [site01, site02, site03, site04, site05, site06, site07, site08, site09, site10, site11, site12,
         site13, site14]
WIRE_CASES = SITES + [publish_b, publish_c, publish_c_empty, publish_d]


@pytest.mark.parametrize("build", WIRE_CASES, ids=lambda f: f.__name__)
def test_wire_request(build, ctx):
    case = build(ctx)
    rec = Recorder(make_handler(case.routes)).install(ctx.monkeypatch)
    result = case.drive()
    assert len(rec.calls) == len(case.expected) == len(rec.requests)
    for i, exp in enumerate(case.expected):
        check_call(rec, i, exp)
    case.result(result)


@pytest.mark.parametrize("status", [400, 500, 302])
@pytest.mark.parametrize("build", SITES, ids=lambda f: f.__name__)
def test_error_status_propagates_before_the_body_is_read(build, status, ctx):
    case = build(ctx)
    rec = Recorder(make_handler(case.routes, fail=(case.fail_route, status))).install(ctx.monkeypatch)
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        case.drive()
    assert excinfo.value.response.status_code == status
    # the flow stopped at the failing request, which ran exactly once (a 302 is not followed)
    assert len(rec.requests) == case.fail_index + 1
    assert [(r.method, r.url.path) for r in rec.requests].count(case.fail_route) == 1
    if status == 302:
        assert excinfo.value.response.headers["location"] == "https://other.example/"
    for i in range(case.fail_index + 1):
        check_call(rec, i, case.expected[i])
    if case.after_fail is not None:
        case.after_fail()


# ── behavior around the calls (kept so the literals of the enclosing functions are pinned too) ─────

def test_discover_account_without_a_linked_instagram_account(monkeypatch):
    routes = {
        ("GET", "/v19.0/me/accounts"): json_resp({"data": [{"id": PAGE_1, "access_token": PAGE_1_TOKEN}]}),
        ("GET", f"/v19.0/{PAGE_1}"): json_resp({"id": PAGE_1}),
    }
    Recorder(make_handler(routes)).install(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT).discover_account(USER_TOKEN)
    assert str(excinfo.value) == (
        "No Facebook Page with a linked Instagram Business/Creator account was found. "
        "In the Instagram app: Settings → Account type → switch to Professional, "
        "then connect it to a Facebook Page you manage."
    )


def test_discover_account_with_no_pages_at_all(monkeypatch):
    rec = Recorder(make_handler({("GET", "/v19.0/me/accounts"): json_resp({"data": []})})).install(monkeypatch)
    with pytest.raises(ValueError, match="No Facebook Page with a linked Instagram"):
        InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT).discover_account(USER_TOKEN)
    assert len(rec.calls) == 1


def test_discover_account_with_a_missing_data_key(monkeypatch):
    Recorder(make_handler({("GET", "/v19.0/me/accounts"): json_resp({})})).install(monkeypatch)
    with pytest.raises(ValueError, match="No Facebook Page with a linked Instagram"):
        InstagramOAuth(IG_APP_ID, IG_APP_SECRET, IG_REDIRECT).discover_account(USER_TOKEN)


def test_publish_without_a_session_url_raises(ctx):
    cut = _publish_cut(ctx, ["a"])
    routes = {("POST", UPLOAD_PATH): lambda request: httpx.Response(200, json={})}
    rec = Recorder(make_handler(routes)).install(ctx.monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        YouTubePublisher().publish(cut, _credential(), MagicMock(), caption="caption")
    assert str(excinfo.value) == "YouTube did not return a resumable upload session URL"
    assert len(rec.calls) == 1


def test_publish_with_captions_uploads_them_after_the_video(ctx, db_session):
    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db_session.add(reel)
    db_session.flush()
    srt = ctx.tmp_path / "captions.srt"
    srt.write_bytes(SRT_BYTES)
    video = ctx.tmp_path / "video.mp4"
    video.write_bytes(VIDEO_BYTES)
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved,
                     video_path=str(video), subtitle_path=str(srt), hashtags=["x"])
    db_session.add(cut)
    db_session.commit()
    _pin_boundary(ctx)
    routes = {
        ("POST", UPLOAD_PATH): lambda request: httpx.Response(200, headers={"Location": SESSION_URL}),
        ("PUT", SESSION_PATH): json_resp({"id": UPLOADED_VIDEO_ID}),
        ("POST", CAPTIONS_PATH): json_resp({"id": "caption-track-1"}),
    }
    rec = Recorder(make_handler(routes)).install(ctx.monkeypatch)
    result = YouTubePublisher().publish(cut, _credential(), db_session, caption="caption")
    assert result.platform_post_id == UPLOADED_VIDEO_ID
    assert [(c.method, c.url) for c in rec.calls] == [("POST", UPLOAD_URL), ("PUT", SESSION_URL), ("POST", CAPTIONS_URL)]
    check_call(rec, 2, _captions_exp(UPLOADED_VIDEO_ID, PUBLISH_TOKEN))
    (event,) = db_session.query(models.StageEvent).all()
    assert (event.stage, event.provider, event.ok, event.detail) == ("captions_upload", "youtube", True, {})
    assert (event.reel_id, event.cut_id) == (reel.id, cut.id)


def test_a_captions_failure_never_fails_the_publish(ctx, db_session, caplog):
    reel = models.Reel(context="c", status=models.ReelStatus.guide_ready)
    db_session.add(reel)
    db_session.flush()
    srt = ctx.tmp_path / "captions.srt"
    srt.write_bytes(SRT_BYTES)
    video = ctx.tmp_path / "video.mp4"
    video.write_bytes(VIDEO_BYTES)
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, status=models.CutStatus.approved,
                     video_path=str(video), subtitle_path=str(srt), hashtags=["x"])
    db_session.add(cut)
    db_session.commit()
    _pin_boundary(ctx)
    routes = {
        ("POST", UPLOAD_PATH): lambda request: httpx.Response(200, headers={"Location": SESSION_URL}),
        ("PUT", SESSION_PATH): json_resp({"id": UPLOADED_VIDEO_ID}),
        ("POST", CAPTIONS_PATH): lambda request: failing(500),
    }
    Recorder(make_handler(routes)).install(ctx.monkeypatch)
    with caplog.at_level(logging.ERROR, logger="engine.publish.youtube"):
        result = YouTubePublisher().publish(cut, _credential(), db_session, caption="caption")
    assert result.platform_post_id == UPLOADED_VIDEO_ID
    (event,) = db_session.query(models.StageEvent).all()
    assert (event.stage, event.provider, event.ok) == ("captions_upload", "youtube", False)
    assert event.detail["error"].startswith("HTTPStatusError(")
    messages = [r.getMessage() for r in caplog.records if r.name == "engine.publish.youtube"]
    assert messages == [f"caption upload failed for cut {cut.id} (video is live, video_id={UPLOADED_VIDEO_ID})"]


def test_create_container_without_an_id_raises(monkeypatch):
    path = f"/v19.0/{IG_USER}/media"
    Recorder(make_handler({("POST", path): json_resp({})})).install(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        InstagramPublisher()._create_container(IG_USER, IG_TOKEN, IG_VIDEO_URL, "caption")
    assert str(excinfo.value) == "Instagram did not return a media container id"


def test_publish_container_without_an_id_raises(monkeypatch):
    path = f"/v19.0/{IG_USER}/media_publish"
    Recorder(make_handler({("POST", path): json_resp({})})).install(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        InstagramPublisher()._publish_container(IG_USER, IG_TOKEN, CREATION_ID)
    assert str(excinfo.value) == "Instagram did not return a published media id"


def test_wait_until_ready_reports_a_processing_error(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    path = f"/v19.0/{CREATION_ID}"
    rec = Recorder(make_handler({("GET", path): json_resp({"status_code": "ERROR"})})).install(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        InstagramPublisher()._wait_until_ready(CREATION_ID, IG_TOKEN)
    assert str(excinfo.value) == "Instagram failed to process the uploaded video (status_code=ERROR)"
    assert len(rec.calls) == 1 and sleeps == []


def test_wait_until_ready_polls_until_finished_then_gives_up(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    path = f"/v19.0/{CREATION_ID}"
    statuses = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    rec = Recorder(make_handler({("GET", path): lambda r: httpx.Response(200, json={"status_code": next(statuses)})}))
    rec.install(monkeypatch)
    InstagramPublisher()._wait_until_ready(CREATION_ID, IG_TOKEN)
    assert len(rec.calls) == 3 and sleeps == [5.0, 5.0]

    sleeps.clear()
    rec2 = Recorder(make_handler({("GET", path): json_resp({"status_code": "IN_PROGRESS"})})).install(monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        InstagramPublisher()._wait_until_ready(CREATION_ID, IG_TOKEN)
    assert str(excinfo.value) == "Instagram video processing did not finish within 180s — try publishing again"
    assert len(rec2.calls) == 36 and sleeps == [5.0] * 36


def test_youtube_metrics_without_items_or_with_partial_statistics(monkeypatch):
    cut = MagicMock()
    cut.platform_post_id = YT_METRICS_VIDEO
    credential = _metrics_credential(YT_METRICS_TOKEN)
    path = ("GET", "/youtube/v3/videos")

    Recorder(make_handler({path: json_resp({"items": []})})).install(monkeypatch)
    assert YouTubeMetricsFetcher().fetch(cut, credential, MagicMock()) is None
    Recorder(make_handler({path: json_resp({})})).install(monkeypatch)
    assert YouTubeMetricsFetcher().fetch(cut, credential, MagicMock()) is None

    Recorder(make_handler({path: json_resp({"items": [{"statistics": {"viewCount": "10"}}]})})).install(monkeypatch)
    r = YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())
    assert (r.views, r.likes, r.comments) == (10, None, None)
    Recorder(make_handler({path: json_resp({"items": [{"statistics": {"likeCount": "11"}}]})})).install(monkeypatch)
    r = YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())
    assert (r.views, r.likes, r.comments) == (None, 11, None)
    Recorder(make_handler({path: json_resp({"items": [{"statistics": {"commentCount": "12"}}]})})).install(monkeypatch)
    r = YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())
    assert (r.views, r.likes, r.comments) == (None, None, 12)
    Recorder(make_handler({path: json_resp({"items": [{}]})})).install(monkeypatch)
    r = YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())
    assert (r.views, r.likes, r.comments) == (None, None, None)


def test_youtube_metrics_refresh_an_expired_token_first(monkeypatch):
    """The token used on the wire is the refreshed one, not the stored one."""
    credential = MagicMock()
    credential.token_blob = "stale-token-3232"
    credential.refresh_token_blob = YT_REFRESH_TOKEN
    credential.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    cut = MagicMock()
    cut.platform_post_id = YT_METRICS_VIDEO
    routes = {
        ("POST", "/token"): json_resp({"access_token": "fresh-token-3333", "expires_in": 3600}),
        ("GET", "/youtube/v3/videos"): json_resp({"items": []}),
    }
    rec = Recorder(make_handler(routes)).install(monkeypatch)
    YouTubeMetricsFetcher().fetch(cut, credential, MagicMock())
    assert [(c.method, c.url) for c in rec.calls] == [
        ("POST", "https://oauth2.googleapis.com/token"),
        ("GET", "https://www.googleapis.com/youtube/v3/videos"),
    ]
    assert rec.calls[1].kwargs["headers"] == {"Authorization": "Bearer fresh-token-3333"}


def _instagram_fetch(monkeypatch, payload):
    db, reel, cut = _db_with_cut()
    path = f"/v19.0/{IG_METRICS_POST}/insights"
    Recorder(make_handler({("GET", path): json_resp(payload)})).install(monkeypatch)
    result = InstagramMetricsFetcher().fetch(cut, SimpleNamespace(token_blob=IG_METRICS_TOKEN), db)
    return result, db.query(models.StageEvent).all()


def test_instagram_metrics_records_a_missing_metric_without_raising(monkeypatch):
    result, events = _instagram_fetch(monkeypatch, {"data": [IG_INSIGHTS[0], IG_INSIGHTS[2]]})
    assert (result.views, result.likes, result.comments) == (500, None, 4)
    (event,) = events
    assert event.ok is False and event.detail == {"missing_metrics": ["likes"]}


def test_instagram_metrics_does_not_flag_a_present_but_valueless_metric(monkeypatch):
    payload = {"data": [IG_INSIGHTS[0], {"name": "likes", "values": []}, IG_INSIGHTS[2]]}
    result, events = _instagram_fetch(monkeypatch, payload)
    assert (result.views, result.likes, result.comments) == (500, None, 4)
    (event,) = events
    assert event.ok is True and event.detail == {}


def test_instagram_metrics_with_no_data_yet_is_not_drift(monkeypatch):
    for payload in ({"data": []}, {}):
        result, events = _instagram_fetch(monkeypatch, payload)
        assert result is None
        (event,) = events
        assert event.ok is True and event.detail == {}


def test_instagram_metrics_each_metric_lands_in_its_own_field(monkeypatch):
    """Distinct values per metric, so a name mix-up in the result is visible."""
    payload = {"data": [
        {"name": "comments", "values": [{"value": 7}]},
        {"name": "plays", "values": [{"value": 9}]},
        {"name": "likes", "values": [{"value": 8}]},
    ]}
    result, events = _instagram_fetch(monkeypatch, payload)
    assert (result.views, result.likes, result.comments) == (9, 8, 7)
    (event,) = events
    assert event.ok is True


def test_the_secret_name_list_is_the_documented_37():
    assert len(SECRET_NAMES) == 37
    assert all(re.fullmatch(r"[a-z0-9]+", n) for n in SECRET_NAMES)
