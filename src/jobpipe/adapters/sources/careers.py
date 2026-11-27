"""Companies' own career sites (S1, owner's request): no ATS in between.

* `careers`: any site that publishes a sitemap of its vacancies and marks each vacancy page with
  schema.org `JobPosting` (JSON-LD), which is what search engines read. Kaspi.kz and Meta do. The
  sitemap lists every open vacancy, so closures are known without fetching every page. Pages are
  fetched at most `max_new` per site per run, newest unseen first; seen URLs are remembered.
* `workday`: Workday-hosted career sites (Nvidia and many large companies) through the same public
  JSON search the site's own page uses (`/wday/cxs/<tenant>/<site>/jobs`), one query per keyword.

Both go through `Http`: robots.txt, one request per second per host, and any 401/403/429 or captcha
stops the site (nothing is worked around). Sites whose robots.txt disallows their job pages (for
example Google's job search) cannot be added here.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ...domain.job import Posting
from ...services.geo import to_iso2
from ...services.text import html_to_text
from ..http import Http
from .base import FetchOutcome, SourceContext, iso_utc

_LD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SEEN_CAP = 4000


def _h(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def sitemap_urls(xml: str) -> list[tuple[str, str | None]]:
    """(loc, lastmod) of a urlset, or of the child sitemaps of a sitemapindex."""
    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    out = []
    for node in root:
        loc = node.find("sm:loc", _NS)
        mod = node.find("sm:lastmod", _NS)
        if loc is not None and loc.text:
            out.append((loc.text.strip(), mod.text.strip() if mod is not None and mod.text else None))
    return out


def _walk(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk(x)
    elif isinstance(obj, dict):
        yield obj
        for k in ("@graph", "itemListElement"):
            if k in obj:
                yield from _walk(obj[k])


def job_posting(html: str) -> dict | None:
    for block in _LD.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            t = node.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return node
    return None


def _name(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or "")
    return str(v or "")


def _locations(jp: dict) -> tuple[str | None, str | None]:
    locs = jp.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    names, countries = [], set()
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            names.append(addr)
            continue
        city = addr.get("addressLocality") or ""
        country = _name(addr.get("addressCountry"))
        names.append(", ".join(dict.fromkeys(x for x in (city, country) if x)))   # "KZ, KZ" -> "KZ"
        iso = to_iso2(country) if country else None
        if iso:
            countries.add(iso)
    return "; ".join(n for n in names if n) or None, (countries.pop() if len(countries) == 1 else None)


def _salary(jp: dict) -> str | None:
    """Only a salary the employer published in the posting itself; never an estimate."""
    bs = jp.get("baseSalary")
    if not isinstance(bs, dict):
        return None
    v = bs.get("value") if isinstance(bs.get("value"), dict) else {}
    lo, hi = v.get("minValue"), v.get("maxValue")
    if lo is None and hi is None and v.get("value") is None:
        return None
    rng = f"{lo}–{hi}" if lo is not None and hi is not None else str(lo or hi or v.get("value"))
    return f"{rng} {bs.get('currency') or ''} {v.get('unitText') or ''}".strip()


def posting_from_ld(jp: dict, *, source: str, site: str, url: str, company: str) -> Posting:
    location, country = _locations(jp)
    remote = None
    if str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        req = jp.get("applicantLocationRequirements") or []
        req = req if isinstance(req, list) else [req]
        remote = "remote: " + (", ".join(_name(r) for r in req if _name(r)) or "location not stated")
    return Posting(
        source=source, kind="ats", source_job_id=f"{site}:{_h(url)}",
        company=_name(jp.get("hiringOrganization")) or company, title=" ".join(str(jp.get("title") or "").split()),
        url=url, description=html_to_text(str(jp.get("description") or ""), unescape_first=True),
        location=location, country=country, remote=remote, posted_at=iso_utc(jp.get("datePosted")),
        salary_text=_salary(jp), extra={"via": "career site", "employment": jp.get("employmentType")},
    )


class CareerSites:
    name = "careers"
    kind = "ats"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome(sink=ctx.sink)
        for site in ctx.params.get("sites", []):
            key = site["key"]

            def unit(site=site, key=key):
                urls = self._job_urls(http, site)
                include = re.compile(site["include"], re.I) if site.get("include") else None
                if include:
                    urls = [(u, m) for u, m in urls if include.search(u)]
                # every open vacancy is in the sitemap: closures without fetching each page
                out.complete_ids[f"{self.name}:{key}"] = {f"{self.name}:{key}:{_h(u)}" for u, _ in urls}
                seen = set(filter(None, ctx.cursors.get(f"seen:{key}", "").split(",")))
                todo = sorted((x for x in urls if _h(x[0]) not in seen), key=lambda x: x[1] or "", reverse=True)
                for u, _ in todo[: int(site.get("max_new", 40))]:
                    seen.add(_h(u))
                    jp = job_posting(http.get_text(u))
                    if jp is None:
                        continue                                  # not a vacancy page (or no markup)
                    out.postings.append(posting_from_ld(jp, source=self.name, site=key, url=u,
                                                        company=site.get("company", key)))
                live = {_h(u) for u, _ in urls}
                ctx.cursors[f"seen:{key}"] = ",".join(sorted(seen & live)[:SEEN_CAP])

            out.run_unit(key, unit)
        return out

    @staticmethod
    def _job_urls(http: Http, site: dict) -> list[tuple[str, str | None]]:
        urls = sitemap_urls(http.get_text(site["sitemap"]))
        if site.get("index"):                     # a sitemap index: follow the children that match
            child = re.compile(site["index"], re.I)
            nested = []
            for loc, _ in urls:
                if child.search(loc):
                    nested += sitemap_urls(http.get_text(loc))
            urls = nested
        return urls


class Workday:
    name = "workday"
    kind = "ats"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome(sink=ctx.sink)
        for t in ctx.params.get("tenants", []):
            base = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}"
            for q in t.get("queries", ctx.params.get("queries", ["c++"])):

                def unit(t=t, base=base, q=q):
                    data = http.post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": int(t.get("limit", 20)),
                                                           "offset": 0, "searchText": q})
                    for j in data.get("jobPostings", []):
                        path = j.get("externalPath") or ""
                        if not path:
                            continue
                        jid = path.rsplit("_", 1)[-1]
                        url = f"https://{t['host']}/{t['site']}{path}"
                        desc, posted, loc = "", None, j.get("locationsText")
                        if t.get("details", True):
                            info = (http.get_json(f"{base}{path}") or {}).get("jobPostingInfo") or {}
                            desc = html_to_text(info.get("jobDescription") or "")
                            posted = iso_utc(info.get("startDate"))
                            loc = info.get("location") or loc
                            if info.get("additionalLocations"):
                                loc = "; ".join([loc or "", *info["additionalLocations"]]).strip("; ")
                        out.postings.append(Posting(
                            source=self.name, kind="ats", source_job_id=f"{t['tenant']}:{jid}",
                            company=t.get("company") or t["tenant"], title=j.get("title") or "", url=url,
                            description=desc or j.get("title") or "", location=loc, country=None,
                            remote=loc if loc and "remote" in loc.lower() else None, posted_at=posted,
                            extra={"via": "workday", "host": urlsplit(url).netloc}))

                out.run_unit(f"{t['tenant']}:{q}", unit)
        return out
