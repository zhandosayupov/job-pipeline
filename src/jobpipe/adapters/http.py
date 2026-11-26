"""Polite HTTP for sources: one User-Agent, robots.txt (RFC 9309), per-host pacing, small retries.

A refusal (robots disallow, 401/403, 429, captcha page) raises Blocked and is never worked around:
no proxies, no fingerprint changes, no captcha solving (ADR 0012).
"""

from __future__ import annotations

import time
import urllib.robotparser
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

DEFAULT_UA = "jobpipe/0.1 (+https://github.com/zhandosayupov/job-pipeline)"
_CAPTCHA_MARKERS = ("g-recaptcha", "h-captcha", "cf-challenge", "challenge-platform", "captcha-delivery")


class Blocked(Exception):
    """The site refused us. Stop this source for this run and tell the owner."""


class FetchError(Exception):
    """Network or server error after retries."""


class Http:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        user_agent: str = DEFAULT_UA,
        min_interval_s: float = 1.0,
        retries: int = 2,
        timeout_s: float = 30,
        respect_robots: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.client = client or httpx.Client(follow_redirects=True)
        self.headers = {"User-Agent": user_agent}
        self.user_agent = user_agent
        self.min_interval_s = min_interval_s
        self.retries = retries
        self.timeout_s = timeout_s
        self.respect_robots = respect_robots
        self.sleep = sleep
        self.monotonic = monotonic
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # --- public --------------------------------------------------------------------------

    def get_json(self, url: str, params: dict | None = None):
        resp = self._get(url, params)
        try:
            return resp.json()
        except ValueError as e:
            raise FetchError(f"{url}: not JSON") from e

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self._get(url, params).text

    # --- internals -----------------------------------------------------------------------

    def _pace(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval_s - (self.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = self.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            self._robots[host] = self._load_robots(host)
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def _load_robots(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        """RFC 9309: 4xx = no restrictions (None); 5xx or unreachable = assume full disallow."""
        rp = urllib.robotparser.RobotFileParser()
        try:
            self._pace(urlsplit(host).netloc)
            resp = self.client.get(f"{host}/robots.txt", headers=self.headers, timeout=self.timeout_s)
        except httpx.HTTPError:
            rp.parse(["User-agent: *", "Disallow: /"])
            return rp
        if 400 <= resp.status_code < 500:
            return None
        if resp.status_code >= 500:
            rp.parse(["User-agent: *", "Disallow: /"])
            return rp
        rp.parse(resp.text.splitlines())
        return rp

    def _get(self, url: str, params: dict | None) -> httpx.Response:
        if not self._allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        last_err = ""
        for attempt in range(self.retries + 1):
            self._pace(host)
            try:
                resp = self.client.get(url, params=params, headers=self.headers, timeout=self.timeout_s)
            except httpx.TimeoutException:
                last_err = "timeout"
            except httpx.TransportError as e:
                last_err = type(e).__name__
            else:
                if resp.status_code in (401, 403, 429):
                    raise Blocked(f"HTTP {resp.status_code} from {host}")
                if resp.status_code >= 500:
                    last_err = f"HTTP {resp.status_code}"
                elif resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                else:
                    ctype = resp.headers.get("content-type", "")
                    if "html" in ctype and any(m in resp.text[:20000] for m in _CAPTCHA_MARKERS):
                        raise Blocked(f"captcha or bot check on {host}")
                    return resp
            if attempt < self.retries:
                self.sleep(2.0 * (attempt + 1))
        raise FetchError(f"{url}: {last_err}")
