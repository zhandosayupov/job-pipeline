import httpx
import pytest

from jobpipe.adapters.http import Blocked, FetchError, Http


def make(handler, **kw):
    sleeps = []
    clock = {"t": 0.0}
    http = Http(
        httpx.Client(transport=httpx.MockTransport(handler)), sleep=sleeps.append, monotonic=lambda: clock["t"], **kw
    )
    return http, sleeps


def robots(body, status=200):
    return httpx.Response(status, text=body)


def test_robots_disallow_blocks_and_is_cached():
    calls = []

    def h(req):
        calls.append(req.url.path)
        if req.url.path == "/robots.txt":
            return robots("User-agent: *\nDisallow: /private/\n")
        return httpx.Response(200, json={"ok": True})

    http, _ = make(h)
    assert http.get_json("https://x.test/public") == {"ok": True}
    with pytest.raises(Blocked, match="robots"):
        http.get_json("https://x.test/private/a")
    assert calls.count("/robots.txt") == 1


@pytest.mark.parametrize("status,allowed", [(404, True), (401, True), (503, False)])
def test_robots_status_rules(status, allowed):
    def h(req):
        if req.url.path == "/robots.txt":
            return robots("", status)
        return httpx.Response(200, json={})

    http, _ = make(h)
    if allowed:
        assert http.get_json("https://x.test/a") == {}
    else:
        with pytest.raises(Blocked):
            http.get_json("https://x.test/a")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_refusals_are_blocked_not_retried(status):
    calls = []

    def h(req):
        if req.url.path == "/robots.txt":
            return robots("", 404)
        calls.append(1)
        return httpx.Response(status)

    http, _ = make(h)
    with pytest.raises(Blocked):
        http.get_text("https://x.test/a")
    assert len(calls) == 1


def test_server_errors_retry_then_fail():
    calls = []

    def h(req):
        if req.url.path == "/robots.txt":
            return robots("", 404)
        calls.append(1)
        return httpx.Response(502)

    http, _ = make(h, retries=2)
    with pytest.raises(FetchError, match="502"):
        http.get_text("https://x.test/a")
    assert len(calls) == 3


def test_captcha_page_is_blocked():
    def h(req):
        if req.url.path == "/robots.txt":
            return robots("", 404)
        return httpx.Response(200, text='<div class="g-recaptcha"></div>', headers={"content-type": "text/html"})

    http, _ = make(h)
    with pytest.raises(Blocked, match="captcha"):
        http.get_text("https://x.test/a")


def test_per_host_pacing():
    def h(req):
        if req.url.path == "/robots.txt":
            return robots("", 404)
        return httpx.Response(200, text="ok")

    http, sleeps = make(h, min_interval_s=1.0)
    http.get_text("https://x.test/a")
    http.get_text("https://x.test/b")
    assert sleeps and all(0 < s <= 1.0 for s in sleeps)
