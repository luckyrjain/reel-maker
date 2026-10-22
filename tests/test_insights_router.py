"""Tests for api/routers/insights.py — GET /api/insights and PerformanceNote CRUD."""
import gc
import re
import weakref

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import models
from api.db import get_db
from api.main import app
from engine.generation.guide_schema import Beat, PlatformGuide


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c._session_factory = TestingSessionLocal
        c._engine = engine
        yield c
    app.dependency_overrides.clear()


def _make_reel_with_metrics(session_factory, *, quality_score, views, niche="football"):
    db = session_factory()
    try:
        reel = models.Reel(context="A test reel", niche=niche, status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        db.add(models.Job(
            type=models.JobType.generate, reel_id=reel.id,
            status=models.JobStatus.done, meta={"quality_score": quality_score},
        ))
        db.add(models.Cut(
            reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
            status=models.CutStatus.published, views=views,
        ))
        db.commit()
        return reel.id
    finally:
        db.close()


# ── page rendering ───────────────────────────────────────────────────────────

def test_insights_page_renders_with_no_data(client):
    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "Not enough published, measured reels yet" in resp.text


def test_insights_page_renders_with_some_but_insufficient_data(client):
    for i in range(3):
        _make_reel_with_metrics(client._session_factory, quality_score=50 + i, views=100 * (i + 1))

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "n = 3 of 5 needed" in resp.text


def test_insights_page_renders_with_enough_data(client):
    qualities = [10, 20, 30, 40, 50]
    views = [2, 4, 5, 4, 5]
    for q, v in zip(qualities, views):
        _make_reel_with_metrics(client._session_factory, quality_score=q, views=v)

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "(n = 5)" in resp.text


def _selects_from(client, table, *, path="/api/insights"):
    """Number of SELECT statements the request issues that read `table`."""
    seen: list[str] = []

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(client._engine, "before_cursor_execute", record)
    try:
        assert client.get(path).status_code == 200
    finally:
        event.remove(client._engine, "before_cursor_execute", record)
    pattern = re.compile(rf"\bFROM {table}\b", re.IGNORECASE)
    return sum(1 for q in seen if q.lstrip().upper().startswith("SELECT") and pattern.search(q))


def test_insights_page_reads_reels_and_jobs_once_each(client):
    """The page needs two analyses over the same reels/jobs; it must load them once.
    (Reels are read through the reels table, cuts through a join -- count the reels table.)"""
    for i in range(6):
        _make_reel_with_metrics(client._session_factory, quality_score=50 + i, views=100 * (i + 1))

    assert _selects_from(client, "reels") == 1
    assert _selects_from(client, "jobs") == 1
    # Cuts come in on the reels SELECT (a joinedload: `FROM reels LEFT OUTER JOIN cuts`). A
    # standalone `FROM cuts` would be a lazy per-reel load (N+1) or an extra query, each
    # re-reading every cut's guide JSON.
    assert _selects_from(client, "cuts") == 0


# ── PerformanceNote CRUD round-trip ──────────────────────────────────────────

def test_create_note_round_trips_through_db(client):
    resp = client.post("/api/insights/notes", data={"text": "Direct-question hooks win."})
    assert resp.status_code == 200
    assert "Direct-question hooks win." in resp.text

    db = client._session_factory()
    try:
        notes = db.query(models.PerformanceNote).all()
        assert len(notes) == 1
        assert notes[0].text == "Direct-question hooks win."
        assert notes[0].active is True
    finally:
        db.close()


def test_create_note_with_blank_text_is_not_saved(client):
    resp = client.post("/api/insights/notes", data={"text": "   "})
    assert resp.status_code == 200

    db = client._session_factory()
    try:
        assert db.query(models.PerformanceNote).count() == 0
    finally:
        db.close()


def test_toggle_note_flips_active(client):
    db = client._session_factory()
    try:
        note = models.PerformanceNote(text="Some note", active=True)
        db.add(note)
        db.commit()
        note_id = note.id
    finally:
        db.close()

    resp = client.post(f"/api/insights/notes/{note_id}/toggle")
    assert resp.status_code == 200

    db = client._session_factory()
    try:
        refreshed = db.get(models.PerformanceNote, note_id)
        assert refreshed.active is False
    finally:
        db.close()

    # Toggle back
    resp = client.post(f"/api/insights/notes/{note_id}/toggle")
    assert resp.status_code == 200
    db = client._session_factory()
    try:
        refreshed = db.get(models.PerformanceNote, note_id)
        assert refreshed.active is True
    finally:
        db.close()


def test_toggle_unknown_note_404s(client):
    resp = client.post("/api/insights/notes/999/toggle")
    assert resp.status_code == 404


def test_delete_note_removes_it_and_no_longer_appears_in_fresh_get(client):
    db = client._session_factory()
    try:
        note = models.PerformanceNote(text="Delete me", active=True)
        db.add(note)
        db.commit()
        note_id = note.id
    finally:
        db.close()

    resp = client.get("/api/insights")
    assert "Delete me" in resp.text

    resp = client.request("DELETE", f"/api/insights/notes/{note_id}")
    assert resp.status_code == 200
    assert "Delete me" not in resp.text

    db = client._session_factory()
    try:
        assert db.get(models.PerformanceNote, note_id) is None
    finally:
        db.close()

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "Delete me" not in resp.text


def test_delete_unknown_note_is_a_no_op_not_an_error(client):
    resp = client.request("DELETE", "/api/insights/notes/999")
    assert resp.status_code == 200


# ── free text from the DB is cleaned before it is shown ──────────────────────

_NUL = "\x00"
_RLO = "\u202e"
_HOSTILE = f"bad{_NUL}text{_RLO}reversed"


def _guide_with_hook(hook_vo):
    return PlatformGuide(
        platform="youtube_shorts", target_length_s=30.0, caption="c", hashtags=["t"] * 6,
        beats=[
            Beat(index=0, type="hook", duration_s=3, visual_direction="v", on_screen_text=[], vo_script=hook_vo),
            Beat(index=1, type="body", duration_s=8, visual_direction="v", on_screen_text=[], vo_script="b"),
            Beat(index=2, type="cta", duration_s=5, visual_direction="v", on_screen_text=[], vo_script="c"),
        ],
    ).model_dump()


def _add_reel(session_factory, *, niche, hook_vo, quality=60, views=100):
    db = session_factory()
    try:
        reel = models.Reel(context="x", niche=niche, status=models.ReelStatus.guide_ready)
        db.add(reel)
        db.flush()
        db.add(models.Job(type=models.JobType.generate, reel_id=reel.id,
                          status=models.JobStatus.done, meta={"quality_score": quality}))
        db.add(models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts,
                          status=models.CutStatus.published, views=views,
                          guide=_guide_with_hook(hook_vo)))
        db.commit()
        return reel.id
    finally:
        db.close()


def test_insights_page_strips_nul_and_bidi_overrides_from_niche_and_hook(client):
    _add_reel(client._session_factory, niche=_HOSTILE, hook_vo=_HOSTILE)

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert _NUL not in resp.text and _RLO not in resp.text
    assert resp.text.count("badtextreversed") == 2      # once in the niche cell, once in the hook cell


def test_insights_page_still_escapes_html_in_hook_text(client):
    _add_reel(client._session_factory, niche="n", hook_vo="<script>alert(1)</script>")

    resp = client.get("/api/insights")
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text


def test_performance_note_text_is_cleaned_on_create_and_on_the_page(client):
    created = client.post("/api/insights/notes", data={"text": f"note{_NUL}one{_RLO}two"})
    assert created.status_code == 200
    assert _NUL not in created.text and _RLO not in created.text
    assert "noteonetwo" in created.text

    page = client.get("/api/insights")
    assert _NUL not in page.text and _RLO not in page.text
    assert "noteonetwo" in page.text

    # display-only: the stored note is not rewritten
    db = client._session_factory()
    try:
        assert db.query(models.PerformanceNote).one().text == f"note{_NUL}one{_RLO}two"
    finally:
        db.close()


def test_insights_page_ordinary_text_is_unchanged(client):
    _add_reel(client._session_factory, niche="Premier League football", hook_vo="Who is the GOAT? ⚽ مرحبا")

    resp = client.get("/api/insights")
    assert "Premier League football" in resp.text
    assert "Who is the GOAT? ⚽ مرحبا" in resp.text


# ── the loaded reels are released before the page is rendered ────────────────

def test_loaded_reels_are_released_before_the_template_renders(client, monkeypatch):
    """`load_reel_stats` holds every reel with every cut's guide JSON. Nothing may keep them alive
    into the (slow) template render: the handler builds its context in a function that returns
    first."""
    import api.routers.insights as insights

    for i in range(4):
        _add_reel(client._session_factory, niche=f"n{i}", hook_vo="hook?", views=10 * (i + 1))

    refs: list[weakref.ref] = []
    real_load = insights.load_reel_stats

    def spy_load(db):
        stats = real_load(db)
        refs.extend(weakref.ref(s.reel) for s in stats)
        return stats

    seen: dict = {}
    real_render = insights.templates.TemplateResponse

    def spy_render(*args, **kwargs):
        gc.collect()
        seen["alive_at_render"] = sum(1 for r in refs if r() is not None)
        seen["tracked"] = len(refs)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(insights, "load_reel_stats", spy_load)
    monkeypatch.setattr(insights.templates, "TemplateResponse", spy_render)

    assert client.get("/api/insights").status_code == 200
    assert seen["tracked"] == 4
    assert seen["alive_at_render"] == 0


# ── what the performer tables actually show (round-2 review) ─────────────────

def _table_ids(html, caption):
    """The reel ids (`#123` cells) in the table with this caption, in page order."""
    m = re.search(rf"<caption[^>]*>\s*{re.escape(caption)}\s*</caption>(.*?)</table>", html, re.S)
    assert m, f"no table captioned {caption!r}"
    return [int(i) for i in re.findall(r"#(\d+)</td>", m.group(1))]


def test_top_and_bottom_tables_list_the_right_reels_in_the_right_order(client):
    ids = [
        _make_reel_with_metrics(client._session_factory, quality_score=50, views=100 * (i + 1))
        for i in range(7)
    ]

    html = client.get("/api/insights").text
    assert _table_ids(html, "Top performers") == [ids[6], ids[5], ids[4]]       # most views first
    assert _table_ids(html, "Bottom performers") == [ids[0], ids[1], ids[2]]    # fewest views first


def test_below_two_k_reels_the_page_shows_one_combined_table_by_views(client):
    ids = [
        _make_reel_with_metrics(client._session_factory, quality_score=50, views=100 * (i + 1))
        for i in range(4)
    ]

    html = client.get("/api/insights").text
    assert "Top performers" not in html and "Bottom performers" not in html
    combined = "All measured reels"
    m = re.search(rf"<caption[^>]*>\s*{combined}[^<]*</caption>(.*?)</table>", html, re.S)
    assert m
    assert [int(i) for i in re.findall(r"#(\d+)</td>", m.group(1))] == [ids[3], ids[2], ids[1], ids[0]]


def test_a_lone_surrogate_in_a_hook_does_not_break_the_page(client):
    """A JSON guide can carry a lone-surrogate escape; encoding it as UTF-8 used to 500 the page."""
    _add_reel(client._session_factory, niche="n", hook_vo="hello \ud800 world")

    resp = client.get("/api/insights")
    assert resp.status_code == 200
    assert "hello  world" in resp.text


@pytest.mark.parametrize("blank", ["\n", "\t", "   ", "\x00", "\u202e"])
def test_a_niche_with_nothing_visible_shows_the_dash_fallback(client, blank):
    _add_reel(client._session_factory, niche=blank, hook_vo="hook?")

    cells = re.findall(r"<td>(.*?)</td>", client.get("/api/insights").text, re.S)
    assert "—" in cells


def test_html_in_the_niche_cell_and_in_a_note_is_escaped_too(client):
    """Escaping was only asserted for the hook cell: a `|safe` on the niche or the note text
    would have passed every test."""
    _add_reel(client._session_factory, niche="<b>bold</b>", hook_vo="hook?")
    created = client.post("/api/insights/notes", data={"text": "<i>note</i>"})

    page = client.get("/api/insights").text
    assert "<b>bold</b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page
    for html in (page, created.text):
        assert "<i>note</i>" not in html and "&lt;i&gt;note&lt;/i&gt;" in html


def test_a_hook_and_niche_with_nothing_visible_show_both_fallbacks(client):
    """`|safe_text` must run BEFORE `or`: a value that is only controls has to reach the fallback
    ("—" / "no hook text available") instead of rendering an empty cell."""
    _add_reel(client._session_factory, niche="\x00", hook_vo="\x00")

    html = client.get("/api/insights").text
    assert "no hook text available" in html
    assert "<td>—</td>" in html


def test_performance_notes_are_listed_once_each_newest_first(client):
    from datetime import datetime, timedelta, timezone

    db = client._session_factory()
    try:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for age, text in enumerate(["oldest note", "middle note", "newest note"]):
            db.add(models.PerformanceNote(text=text, active=True, created_at=base + timedelta(hours=age)))
        db.commit()
    finally:
        db.close()

    html = client.get("/api/insights").text
    assert [html.count(t) for t in ("oldest note", "middle note", "newest note")] == [1, 1, 1]
    assert html.index("newest note") < html.index("middle note") < html.index("oldest note")


# ── what the page shows (round-5 review): numbers, links, branches, empty states, note markup ──

def _performer_rows(html):
    """[(reel_id_in_link, [cell text, ...])] for every performer row, in page order."""
    out = []
    for m in re.finditer(r"<tr onclick=\"window.location='/api/reels/(\d+)'\"[^>]*>(.*?)</tr>", html, re.S):
        cells = [c.strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        out.append((int(m.group(1)), cells))
    return out


def test_correlation_line_shows_the_computed_r_to_two_decimals_with_sign_and_the_real_n(client):
    import numpy as np

    qualities = [10, 20, 30, 40, 50, 60]
    views = [900, 700, 800, 300, 400, 100]            # falls as quality rises: r is negative
    for q, v in zip(qualities, views):
        _make_reel_with_metrics(client._session_factory, quality_score=q, views=v)
    r = float(np.corrcoef(qualities, views)[0, 1])
    assert r < -0.5                                    # negative, so the sign is part of what is checked

    html = client.get("/api/insights").text
    assert f"r = {r:.2f}</strong>" in html                # exact: not 1 or 3 decimals, sign kept
    assert "(n = 6)" in html                              # the real sample size, not MIN_SAMPLE


def test_performer_rows_link_to_their_reel_and_show_quality_views_with_separators(client):
    first = _add_reel(client._session_factory, niche="n-a", hook_vo="h", quality=11, views=10)
    second = _add_reel(client._session_factory, niche="n-b", hook_vo="h", quality=22, views=12345)
    third = _add_reel(client._session_factory, niche="n-c", hook_vo="h", quality=33, views=100)

    rows = _performer_rows(client.get("/api/insights").text)
    # combined table (3 < 2k), most views first: so ids are NOT in 1,2,3 order (a loop.index link would show)
    assert [rid for rid, _ in rows] == [second, third, first]
    by_id = dict(rows)
    assert by_id[second] == [f"#{second}", "n-b", "22", "12,345", "h"]
    assert by_id[third][2:4] == ["33", "100"]
    assert by_id[first][2:4] == ["11", "10"]


def test_five_reels_with_identical_quality_show_the_same_score_message(client):
    for views in (100, 200, 300, 400, 500):
        _make_reel_with_metrics(client._session_factory, quality_score=50, views=views)

    html = client.get("/api/insights").text
    assert "Every measured reel scored the same" in html
    assert "r = " not in html and "Not enough published" not in html


def test_an_empty_database_shows_all_three_empty_states(client):
    html = client.get("/api/insights").text
    assert "Not enough published, measured reels yet" in html
    assert "(n = 0 of 5 needed)" in html
    assert "No published, measured reels yet." in html
    assert "No performance notes yet." in html


def test_note_rows_link_toggle_and_delete_to_their_own_id_and_mark_inactive_ones(client):
    from datetime import datetime, timezone

    db = client._session_factory()
    try:
        # ids deliberately unlike their list position
        db.add(models.PerformanceNote(id=7, text="inactive note", active=False,
                                      created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))
        db.add(models.PerformanceNote(id=3, text="active note", active=True,
                                      created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        db.commit()
    finally:
        db.close()

    html = client.get("/api/insights").text
    items = {m.group(1): m.group(0) for m in re.finditer(r"<li\b[^>]*>.*?(inactive note|active note).*?</li>", html, re.S)}
    inactive, active = items["inactive note"], items["active note"]

    assert 'hx-post="/api/insights/notes/7/toggle"' in inactive
    assert 'hx-delete="/api/insights/notes/7"' in inactive
    assert 'hx-post="/api/insights/notes/3/toggle"' in active
    assert 'hx-delete="/api/insights/notes/3"' in active
    # `checked` is the attribute right after type= (the title text also contains the word "checked")
    assert re.search(r'<input type="checkbox"\s+checked\s', active)
    assert not re.search(r'<input type="checkbox"\s+checked\s', inactive)
    assert "line-through" in inactive and "line-through" not in active


def test_notes_are_ordered_by_creation_time_not_by_id(client):
    """The newest note has the LOWEST id here, so ordering by id would reverse the list."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = client._session_factory()
    try:
        db.add(models.PerformanceNote(id=1, text="newest note", active=True, created_at=base + timedelta(hours=2)))
        db.add(models.PerformanceNote(id=2, text="middle note", active=True, created_at=base + timedelta(hours=1)))
        db.add(models.PerformanceNote(id=3, text="oldest note", active=True, created_at=base))
        db.commit()
    finally:
        db.close()

    html = client.get("/api/insights").text
    assert html.index("newest note") < html.index("middle note") < html.index("oldest note")


# ── page wiring and route responses (round-6 test-quality review) ─────────────────────────────
# Each of these pins something a mutant could change with every earlier test still green.

def _seed_notes(session_factory, *texts):
    db = session_factory()
    try:
        for text in texts:
            db.add(models.PerformanceNote(text=text, active=True))
        db.commit()
    finally:
        db.close()


def test_page_chrome_and_note_form_wiring(client):
    html = client.get("/api/insights").text

    assert "<title>Insights — Reel Maker</title>" in html
    assert 'href="/"' in html and 'href="/api/reels"' in html
    assert "/static/htmx-errors.js" in html and "htmx.org@2" in html
    form = re.search(r"<form([^>]*)>(.*?)</form>", html, re.S)
    attrs, body = form.group(1), form.group(2)
    assert 'hx-post="/api/insights/notes"' in attrs
    assert 'hx-swap="outerHTML"' in attrs and 'hx-target="#performance-notes"' in attrs
    assert "this.reset()" in attrs
    assert re.search(r'<textarea[^>]*name="text"[^>]*required', body, re.S)
    assert html.count('id="performance-notes"') == 1


def test_note_fragment_swaps_onto_its_own_root_id(client):
    _seed_notes(client._session_factory, "a note")

    fragment = client.post("/api/insights/notes", data={"text": "b"}).text

    assert fragment.lstrip().startswith('<div id="performance-notes"')
    assert set(re.findall(r'hx-target="([^"]+)"', fragment)) == {"#performance-notes"}
    assert fragment.count('hx-swap="outerHTML"') == 2 * 2  # toggle + delete, for each of the 2 notes
    assert "hx-confirm=" in fragment


def test_table_header_order(client):
    for i in range(7):
        _make_reel_with_metrics(client._session_factory, quality_score=50, views=100 * (i + 1))

    headings = re.findall(r"<th>(.*?)</th>", client.get("/api/insights").text)

    assert headings[:5] == ["Reel", "Niche", "Quality", "Views", "Hook line"]


def test_create_toggle_and_delete_all_return_the_whole_note_list(client):
    _seed_notes(client._session_factory, "keep one", "keep two", "drop me")
    db = client._session_factory()
    try:
        drop = db.query(models.PerformanceNote).filter_by(text="drop me").one().id
        keep = db.query(models.PerformanceNote).filter_by(text="keep one").one().id
    finally:
        db.close()

    deleted = client.request("DELETE", f"/api/insights/notes/{drop}").text
    toggled = client.post(f"/api/insights/notes/{keep}/toggle").text
    created = client.post("/api/insights/notes", data={"text": "fresh"}).text

    assert "keep one" in deleted and "keep two" in deleted and "drop me" not in deleted
    assert "keep one" in toggled and "keep two" in toggled
    assert "keep one" in created and "keep two" in created and "fresh" in created


def test_creating_a_note_without_the_text_field_is_a_422(client):
    assert client.post("/api/insights/notes", data={}).status_code == 422


def test_an_exactly_zero_correlation_is_shown_as_r_0_00_not_as_missing(client):
    # numpy returns exactly 0.0 for these: a truthiness check on r would show "not enough data".
    for quality, views in zip([1, 2, 3, 4, 5], [1, 3, 5, 3, 1]):
        _make_reel_with_metrics(client._session_factory, quality_score=quality, views=views)

    assert "r = 0.00" in client.get("/api/insights").text


def test_a_positive_correlation_is_shown_without_a_plus_sign(client):
    for quality, views in zip([1, 2, 3, 4, 5], [10, 20, 31, 40, 52]):
        _make_reel_with_metrics(client._session_factory, quality_score=quality, views=views)

    assert "r = 1.00</strong>" in client.get("/api/insights").text

