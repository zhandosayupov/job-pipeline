"""Contract tests: every source parses its recorded live fixture (tests/fixtures/sources/)."""

from types import SimpleNamespace

import httpx
import pytest

from jobpipe.adapters.http import Http
from jobpipe.adapters.sources import SOURCES, SourceContext
from jobpipe.adapters.sources.community import parse_hn_comment, parse_tg_page

from .conftest import REPO

F = REPO / "tests" / "fixtures" / "sources"
ROUTES = {
    "boards-api.greenhouse.io": "greenhouse_imc.json",
    "api.ashbyhq.com": "ashby_clickhouse.json",
    "api.lever.co": "lever_spotify.json",
    "himalayas.app": "himalayas.json",
    "jobicy.com": "jobicy.json",
    "weworkremotely.com": "wwr_backend.rss",
    "www.arbeitnow.com": "arbeitnow.json",
    "t.me": "tg_forcpp.html",
    "www.amazon.jobs": "amazon_nl.json",
}


def fixture_http(overrides=None, seen=None):
    def handler(req: httpx.Request):
        if seen is not None:
            seen.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if overrides and req.url.host in overrides:
            return overrides[req.url.host](req)
        host = req.url.host
        if host == "hn.algolia.com":
            name = "hn_stories.json" if "search_by_date" in req.url.path else "hn_comments_cpp.json"
        else:
            name = ROUTES[host]
        ctype = {"html": "text/html", "rss": "application/rss+xml"}.get(name.rsplit(".", 1)[1], "application/json")
        return httpx.Response(200, content=(F / name).read_bytes(), headers={"content-type": ctype})

    return Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def company(name, token):
    return SimpleNamespace(name=name, token=token)


CONTEXTS = {
    "greenhouse": lambda: SourceContext(companies=[company("IMC", "imc")]),
    "ashby": lambda: SourceContext(companies=[company("ClickHouse", "clickhouse")]),
    "lever": lambda: SourceContext(companies=[company("Spotify", "spotify")]),
    "amazon": lambda: SourceContext(params={"countries": ["NLD"], "max_pages": 1}),
    "telegram": lambda: SourceContext(params={"channels": ["forcpp"]}),
}


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_source_parses_its_fixture(name):
    out = SOURCES[name]().fetch(fixture_http(), CONTEXTS.get(name, SourceContext)())
    assert out.status == "ok", out.errors
    assert out.postings, name
    for p in out.postings:
        assert p.source == name and p.id.startswith(f"{name}:")
        assert p.url.startswith("https://"), p.url
        assert p.title and p.description
        assert p.posted_at is None or p.posted_at.endswith("Z")
        assert p.country is None or len(p.country) == 2


def test_ats_boards_report_complete_id_sets():
    out = SOURCES["greenhouse"]().fetch(fixture_http(), CONTEXTS["greenhouse"]())
    assert out.complete_ids["greenhouse:imc"] == {p.id for p in out.postings}


def test_failing_unit_makes_source_partial():
    def boom(req):
        return httpx.Response(500)

    http = fixture_http()
    ctx = SourceContext(companies=[company("IMC", "imc"), company("Bad", "bad")])
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if "/bad/" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200, content=(F / "greenhouse_imc.json").read_bytes(), headers={"content-type": "application/json"}
        )

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    out = SOURCES["greenhouse"]().fetch(http, ctx)
    assert out.status == "partial" and out.units_ok == 1 and "bad" in out.errors[0]


def test_hn_keeps_only_top_level_comments_and_parses_pipe_header():
    c = {
        "objectID": "1",
        "parent_id": 9,
        "created_at": "2026-11-01T10:00:00Z",
        "comment_text": "Acme Trading | Senior C++ Engineer | Amsterdam, Netherlands | ONSITE | Visa"
        "<p>We sponsor visas.",
    }
    p = parse_hn_comment(c)
    assert (p.company, p.title, p.location, p.country) == (
        "Acme Trading",
        "Senior C++ Engineer",
        "Amsterdam, Netherlands",
        "NL",
    )
    seen = []
    out = SOURCES["hn"]().fetch(fixture_http(seen=seen), SourceContext(params={"queries": ["c++"]}))
    assert all("story_" in u or "search_by_date" in u or "robots" in u for u in seen)
    assert out.postings  # fixture comments are top-level replies to the story


def test_telegram_cursor_skips_seen_posts():
    ctx = SourceContext(params={"channels": ["forcpp"]})
    src = SOURCES["telegram"]()
    first = src.fetch(fixture_http(), ctx)
    assert first.postings and int(ctx.cursors["last_id:forcpp"]) >= 3744
    again = src.fetch(fixture_http(), ctx)
    assert again.postings == []


def test_tg_parser_reads_text_and_time():
    html = (
        '<div class="tgme_widget_message js-widget_message" data-post="ch/5"><div class="x">'
        '<div class="tgme_widget_message_text js-message_text">Hiring <b>C++</b> dev<br/>Remote</div></div>'
        '<time datetime="2026-11-20T10:00:00+00:00"></time></div>'
    )
    assert parse_tg_page(html) == [
        {"post": "ch/5", "datetime": "2026-11-20T10:00:00+00:00", "text": "Hiring C++ dev\nRemote"}
    ]
