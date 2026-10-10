"""Remote ids that become local file names (engine/render/asset_sourcer.py).

`pexels_{video id}.mp4` and `wiki_{page id}.{ext}` are built from remote JSON, and the Wikipedia id
falls back to the page title. Unvalidated, an over-long title raised ENAMETOOLONG from
`Path.exists()` (outside the try), and a `/` or `..` in an id relied on the write failing rather
than on the name being safe. Now: a Pexels id is an integer (or a string of ASCII digits, at most
20); a Wikipedia id is such a page id, else the underscored title when that is `[A-Za-z0-9_-]{1,64}`,
else `t` + a 16-hex digest of the title. Anything else is skipped (Pexels) or replaced (Wikipedia),
never interpolated.
"""
import hashlib

import pytest

from engine.render import asset_sourcer as AS
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, WIKI_THUMB, _Resp, _Stream, _pexels, _wiki,
)
from tests.test_sourcer_selection import _vf


def _hit(vid_id, link=PEXELS_OK):
    return {"id": vid_id, "duration": 10, "video_files": [_vf(1080, 1920, link=link)]}


# ── Pexels ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("vid_id,expected", [
    (1, "1"), (856973, "856973"), ("856973", "856973"), (0, "0"), (10**19, str(10**19)),
    ("0" * 20, "0" * 20),
])
def test_pexels_accepts_integer_ids_and_ascii_digit_strings(tmp_path, vid_id, expected):
    result = _pexels(tmp_path, [_hit(vid_id)], _Stream({PEXELS_OK: _Resp()}))
    assert result.source_ref == expected
    assert result.local_path == tmp_path / f"pexels_{expected}.mp4"


@pytest.mark.parametrize("bad", [
    True, False, None, 1.5, 2.0, -5, "", " ", "abc", "../x", "a/b", "a\\b", "1 2", "12\n", "\n12",
    "1.5", "-1", "+1", "1e3", "٣٢",              # Arabic-Indic digits are not [0-9]
    "1" * 21, 10**25, [1], {"a": 1}, "../../etc/passwd", "x" * 5000, "\x00", "12\x00",
], ids=lambda b: repr(b)[:40])
def test_pexels_skips_a_hit_with_an_unsafe_id_and_uses_the_next_one(tmp_path, bad):
    stream = _Stream({PEXELS_OK: _Resp()})
    result = _pexels(tmp_path, [_hit(bad), _hit(7)], stream)
    assert result.source_ref == "7"
    assert stream.urls == [PEXELS_OK]                          # only the good hit downloaded
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_7.mp4"]


def test_pexels_an_unsafe_id_is_skipped_even_when_it_would_be_cached(tmp_path):
    (tmp_path / "pexels_x.mp4").write_bytes(b"cached")
    stream = _Stream({})
    assert _pexels(tmp_path, [_hit("x")], stream) is None and stream.requests == []


def test_pexels_an_over_long_id_cannot_reach_the_filesystem(tmp_path):
    stream = _Stream({PEXELS_OK: _Resp()})
    assert _pexels(tmp_path, [_hit("1" * 4000)], stream) is None
    assert stream.requests == [] and list(tmp_path.iterdir()) == []


# ── Wikipedia: the page id ───────────────────────────────────────────────────

@pytest.mark.parametrize("pageid,expected", [(123, "123"), ("123", "123"), (9, "9"), (10**19, str(10**19))])
def test_wikipedia_numeric_page_ids_name_the_file(tmp_path, pageid, expected):
    summary = {"pageid": pageid, "originalimage": {"source": WIKI_OK}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp()}))
    assert result.source_ref == expected and result.local_path.name == f"wiki_{expected}.jpg"


@pytest.mark.parametrize("bad", [
    True, 1.5, -1, "", "abc", "../x", "a/b", "12\n", "1" * 21, 10**25, [1], {"a": 1}, "٣",
], ids=lambda b: repr(b)[:40])
def test_wikipedia_an_unsafe_page_id_falls_back_to_the_title_not_the_junk(tmp_path, bad):
    summary = {"pageid": bad, "originalimage": {"source": WIKI_OK}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp()}))
    assert result.source_ref == "Lionel_Messi"
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_Lionel_Messi.jpg"]


def test_wikipedia_a_null_page_id_falls_back_to_the_title(tmp_path):
    summary = {"pageid": None, "originalimage": {"source": WIKI_OK}}
    assert _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp()})).source_ref == "Lionel_Messi"


# ── Wikipedia: the title fallback ────────────────────────────────────────────

def _digest_id(title):
    return "t" + hashlib.sha256(title.encode()).hexdigest()[:16]


@pytest.mark.parametrize("title,expected", [
    ("Lionel Messi", "Lionel_Messi"),
    ("Kylian Mbappe-Lottin", "Kylian_Mbappe-Lottin"),
    ("A" * 64, "A" * 64),                                      # exactly the bound
    ("Already_Underscored_1", "Already_Underscored_1"),
])
def test_wikipedia_a_plain_title_is_used_unchanged_as_the_id(tmp_path, title, expected):
    summary = {"originalimage": {"source": WIKI_OK}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp()}), title=title)
    assert result.source_ref == expected and result.local_path.name == f"wiki_{expected}.jpg"


@pytest.mark.parametrize("title", [
    "AC/DC", "Mesé Fernández", "St. Louis", "..", "a/../b", "Lionel Messi?x=1", "A" * 65,
    "x" * 5000, "name\x00null", "back\\slash", "tab\tname", "emoji \U0001f600", "C++", "100%",
])
def test_wikipedia_a_title_that_is_not_a_safe_name_gets_a_digest_id(tmp_path, title):
    summary = {"originalimage": {"source": WIKI_OK}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp()}), title=title)
    expected = _digest_id(title.replace(" ", "_"))
    assert result.source_ref == expected
    assert [p.name for p in tmp_path.iterdir()] == [f"wiki_{expected}.jpg"]   # only inside store_dir


def test_wikipedia_a_title_with_a_lone_surrogate_degrades_to_none(tmp_path):
    """Not reachable as an id: the summary URL can't encode it, so search() already gives up."""
    summary = {"originalimage": {"source": WIKI_OK}}
    assert _wiki(tmp_path, summary, _Stream({}), title="\ud800x") is None


def test_wikipedia_the_digest_id_is_deterministic_and_distinguishes_titles(tmp_path):
    ids = {}
    for i, title in enumerate(["AC/DC", "AC\\DC", "A" * 100, "A" * 101]):
        summary = {"originalimage": {"source": WIKI_OK}}
        r = _wiki(tmp_path / str(i), summary, _Stream({WIKI_OK: _Resp()}), title=title)
        ids[title] = r.source_ref
    assert len(set(ids.values())) == 4
    again = _wiki(tmp_path / "again", {"originalimage": {"source": WIKI_OK}},
                  _Stream({WIKI_OK: _Resp()}), title="AC/DC")
    assert again.source_ref == ids["AC/DC"]


def test_wikipedia_an_over_long_title_no_longer_raises_enametoolong(tmp_path):
    """The reported bug: `lp.exists()` on a 5000-char name raised OSError out of search()."""
    summary = {"originalimage": {"source": WIKI_OK}, "thumbnail": {"source": WIKI_THUMB}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp(), WIKI_THUMB: _Resp()}), title="x" * 5000)
    assert result is not None and len(result.local_path.name) < 255


def test_wikipedia_a_digest_id_file_is_reused_on_the_next_search(tmp_path):
    summary = {"originalimage": {"source": WIKI_OK}}
    first = _wiki(tmp_path, summary, _Stream({WIKI_OK: _Resp(chunks=(b"one",))}), title="AC/DC")
    second_stream = _Stream({})
    second = _wiki(tmp_path, summary, second_stream, title="AC/DC")
    assert second.local_path == first.local_path and second_stream.requests == []


def test_safe_id_constants_are_what_the_docs_say():
    assert AS._SAFE_ID_RE.fullmatch("A" * 64) and not AS._SAFE_ID_RE.fullmatch("A" * 65)
    assert not AS._SAFE_ID_RE.fullmatch("") and not AS._SAFE_ID_RE.fullmatch("a.b")
    assert not AS._SAFE_ID_RE.fullmatch("ab\n")
