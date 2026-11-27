"""Russian-language job boards (S8): Habr Career (HTML page with its own JSON state) and hh.kz / hh.ru
through hh's official public API.

* Habr Career: the search page embeds its results as JSON (`data-ssr-state`), so no HTML scraping.
  Only the salary the employer published is kept; Habr's "predicted salary" is an estimate and is
  ignored (unknown stays null).
* hh: `api.hh.ru/vacancies` needs no login, but hh requires an `HH-User-Agent` header with a real
  contact address (an example address is rejected). Set `params.contact` in sources.yaml to your
  app name and e-mail, e.g. "jobpipe/0.1 (you@yourmail)"; without it the source reports an error.
  hh may refuse data-centre IPs with 403: that stops the source, nothing is worked around.
"""
from __future__ import annotations

import json
import re

from ...domain.job import Posting
from ...services.text import html_to_text
from ..http import FetchError, Http
from .base import FetchOutcome, SourceContext, iso_utc

_STATE = re.compile(r'<script[^>]*type="application/json"[^>]*data-ssr-state[^>]*>(.*?)</script>|'
                    r'<script[^>]*data-ssr-state[^>]*type="application/json"[^>]*>(.*?)</script>', re.S)
HH_AREAS = {"kz": 40, "ru": 113, "by": 16, "uz": 97}


def _salary(s: dict | None) -> str | None:
    if not s or (s.get("from") is None and s.get("to") is None):
        return None
    lo, hi = s.get("from"), s.get("to")
    rng = f"{lo}–{hi}" if lo and hi else (f"от {lo}" if lo else f"до {hi}")
    return f"{rng} {s.get('currency') or ''}".strip()


class HabrCareer:
    name = "habr"
    kind = "aggregator"
    url = "https://career.habr.com/vacancies"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome(sink=ctx.sink)
        for q in ctx.params.get("queries", ["c++"]):

            def unit(q=q):
                for page in range(1, int(ctx.params.get("max_pages", 2)) + 1):
                    html = http.get_text(self.url, {"q": q, "type": "all", "page": page})
                    m = _STATE.search(html)
                    if not m:
                        raise FetchError("habr: no JSON state in the page (layout changed?)")
                    v = json.loads(m.group(1) or m.group(2)).get("vacancies") or {}
                    for j in v.get("list", []):
                        locs = [x.get("title") for x in j.get("locations") or [] if x.get("title")]
                        skills = [x.get("title") for x in j.get("skills") or [] if x.get("title")]
                        divs = [x.get("title") for x in j.get("divisions") or [] if x.get("title")]
                        out.postings.append(Posting(
                            source=self.name, kind="aggregator", source_job_id=str(j["id"]),
                            company=(j.get("company") or {}).get("title") or "", title=j.get("title") or "",
                            url="https://career.habr.com" + (j.get("href") or f"/vacancies/{j['id']}"),
                            description="; ".join(divs + skills) or (j.get("title") or ""),
                            location=", ".join(locs) or None, country=None,
                            remote="удалённо" if j.get("remoteWork") else None,
                            posted_at=iso_utc((j.get("publishedDate") or {}).get("date")),
                            salary_text=_salary(j.get("salary")),
                            extra={"skills": skills, "qualification": (j.get("salaryQualification") or {}).get("title")
                                   if isinstance(j.get("salaryQualification"), dict) else None}))
                    meta = v.get("meta") or {}
                    if page >= int(meta.get("totalPages") or 1):
                        break

            out.run_unit(f"q={q}", unit)
        return out


class HeadHunter:
    name = "hh"
    kind = "aggregator"
    url = "https://api.hh.ru/vacancies"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome(sink=ctx.sink)
        contact = ctx.params.get("contact")
        if not contact:
            out.errors.append("hh: set params.contact (\"app/1.0 (your e-mail)\"): hh requires HH-User-Agent")
            return out
        headers = {"HH-User-Agent": contact}
        for area in ctx.params.get("areas", ["kz"]):
            for q in ctx.params.get("queries", ["c++"]):

                def unit(area=area, q=q):
                    params = {"text": q, "area": HH_AREAS.get(area, area), "per_page": ctx.params.get("per_page", 50),
                              "page": 0, "order_by": "publication_time"}
                    if ctx.params.get("period_days"):
                        params["period"] = ctx.params["period_days"]
                    items = []
                    for page in range(int(ctx.params.get("max_pages", 3))):     # newest first (PR #11 review)
                        data = http.get_json(self.url, {**params, "page": page}, headers=headers)
                        items += data.get("items", [])
                        if page + 1 >= int(data.get("pages") or 1):
                            break
                    for j in items:
                        snip = j.get("snippet") or {}
                        area_name = (j.get("area") or {}).get("name")
                        sched = (j.get("schedule") or {}).get("id")
                        out.postings.append(Posting(
                            source=self.name, kind="aggregator", source_job_id=str(j["id"]),
                            company=(j.get("employer") or {}).get("name") or "", title=j.get("name") or "",
                            url=j.get("alternate_url") or f"https://hh.kz/vacancy/{j['id']}",
                            description=html_to_text(" ".join(x for x in (snip.get("requirement"),
                                                                             snip.get("responsibility")) if x))
                            or (j.get("name") or ""),
                            location=area_name, country={"kz": "KZ", "ru": "RU", "by": "BY", "uz": "UZ"}.get(area),
                            remote="удалённо" if sched == "remote" else None,
                            posted_at=iso_utc(j.get("published_at")), salary_text=_salary(j.get("salary")),
                            extra={"experience": (j.get("experience") or {}).get("name"), "schedule": sched}))

                out.run_unit(f"{area}:q={q}", unit)
        return out
