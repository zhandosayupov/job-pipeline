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
    "career.habr.com": "habr_cpp.html",
    "api.hh.ru": "hh_vacancies.json",
    "apply.workable.com": "workable_huggingface.json",
    "job.kaspi.kz": None,                       # sitemap or vacancy page, by path (careers)
    "nvidia.wd5.myworkdayjobs.com": None,       # search or job detail, by path (workday)
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
        elif host == "job.kaspi.kz":
            name = "careers_kaspi_sitemap.xml" if req.url.path.endswith(".xml") else "careers_kaspi_job.html"
        elif host == "nvidia.wd5.myworkdayjobs.com":
            name = "workday_jobs.json" if req.url.path.endswith("/jobs") else "workday_job.json"
        else:
            name = ROUTES[host]
        types = {"html": "text/html", "rss": "application/rss+xml", "xml": "application/xml"}
        ctype = types.get(name.rsplit(".", 1)[1], "application/json")
        return httpx.Response(200, content=(F / name).read_bytes(), headers={"content-type": ctype})

    return Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def company(name, token):
    return SimpleNamespace(name=name, token=token)


CONTEXTS = {
    "greenhouse": lambda: SourceContext(companies=[company("IMC", "imc")]),
    "ashby": lambda: SourceContext(companies=[company("ClickHouse", "clickhouse")]),
    "lever": lambda: SourceContext(companies=[company("Spotify", "spotify")]),
    "workable": lambda: SourceContext(companies=[company("Hugging Face", "huggingface")]),
    "amazon": lambda: SourceContext(params={"countries": ["NLD"], "max_pages": 1}),
    "telegram": lambda: SourceContext(params={"channels": ["forcpp"]}),
    "hh": lambda: SourceContext(params={"contact": "jobpipe-test/0.1 (owner)", "areas": ["kz"], "queries": ["c++"]}),
    "habr": lambda: SourceContext(params={"queries": ["c++"], "max_pages": 1}),
    "careers": lambda: SourceContext(params={"sites": [{"key": "kaspi", "company": "Kaspi.kz",
                                                        "sitemap": "https://job.kaspi.kz/sitemap/vacancies.xml"}]}),
    "workday": lambda: SourceContext(params={"tenants": [{"company": "NVIDIA", "host": "nvidia.wd5.myworkdayjobs.com",
                                                          "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"}]}),
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


def test_career_site_sitemap_gives_closures_and_remembers_seen_pages():
    seen = []
    ctx = CONTEXTS["careers"]()
    ctx.params["sites"][0]["max_new"] = 2
    out = SOURCES["careers"]().fetch(fixture_http(seen=seen), ctx)
    assert len(out.complete_ids["careers:kaspi"]) == 3          # every vacancy in the sitemap
    assert len(out.postings) == 2 and out.postings[0].company and out.postings[0].country == "KZ"
    assert out.postings[0].salary_text is None                  # no published salary: stays None
    first = [u for u in seen if "/vacancy/" in u]
    out2 = SOURCES["careers"]().fetch(fixture_http(seen=seen), ctx)
    assert len(out2.postings) == 1                              # only the page not fetched before
    assert len([u for u in seen if "/vacancy/" in u]) == len(first) + 1


def test_workday_search_and_details_use_post_json():
    seen = []
    out = SOURCES["workday"]().fetch(fixture_http(seen=seen), CONTEXTS["workday"]())
    assert [p.source_job_id for p in out.postings] == ["nvidia:JR2029620", "nvidia:JR2030001"]
    assert out.postings[0].remote == "Poland, Remote" and "low-latency" in out.postings[0].description
    assert any(u.endswith("/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs") for u in seen)


def test_post_json_respects_blocks():
    import httpx

    from jobpipe.adapters.http import Blocked

    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(429)

    http = Http(httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    with pytest.raises(Blocked):
        http.post_json("https://x.example/wday/cxs/a/b/jobs", {"searchText": "c++"})


def test_habr_ignores_the_predicted_salary():
    out = SOURCES["habr"]().fetch(fixture_http(), CONTEXTS["habr"]())
    assert out.postings[0].salary_text is None          # only Habr's estimate exists for it: not a salary
    assert out.postings[0].company == "ИнфоТеКС" and out.postings[0].url.startswith("https://career.habr.com/")
    assert "C++" in out.postings[0].description


def test_hh_sends_the_contact_header_and_needs_it():
    seen = []

    def hh(req):
        seen.append(req.headers.get("HH-User-Agent"))
        return httpx.Response(200, content=(F / "hh_vacancies.json").read_bytes(),
                              headers={"content-type": "application/json"})

    out = SOURCES["hh"]().fetch(fixture_http({"api.hh.ru": hh}), CONTEXTS["hh"]())
    assert seen == ["jobpipe-test/0.1 (owner)"]
    p = out.postings[0]
    assert p.salary_text == "500000–800000 KZT" and p.remote == "удалённо" and p.country == "KZ"
    assert out.postings[1].salary_text is None
    missing = SOURCES["hh"]().fetch(fixture_http(), SourceContext(params={}))
    assert missing.status == "error" and "contact" in missing.errors[0]


def test_hh_follows_pages_up_to_max_pages():
    import json

    pages = []

    def hh(req):
        page = int(req.url.params.get("page"))
        pages.append(page)
        body = json.loads((F / "hh_vacancies.json").read_text(encoding="utf-8"))
        for it in body["items"]:
            it["id"] = f"{it['id']}{page}"
        body.update(pages=5, page=page)
        return httpx.Response(200, json=body)

    ctx = CONTEXTS["hh"]()
    ctx.params["max_pages"] = 2
    out = SOURCES["hh"]().fetch(fixture_http({"api.hh.ru": hh}), ctx)
    assert pages == [0, 1] and len(out.postings) == 4
    assert out.postings[0].posted_at == "2026-11-26T05:00:00Z"          # hh's "+0500" offset


def test_long_descriptions_keep_head_and_tail():
    from jobpipe.adapters.sources.base import trim_description

    text = "A" * 20000 + " Visa sponsorship is available."
    out = trim_description(text)
    assert len(out) < 12100 and out.startswith("A" * 100) and out.endswith("Visa sponsorship is available.")
    assert trim_description("short") == "short"
