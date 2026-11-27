"""Company job boards on hosted ATSes, one unit per company from companies.yaml. These boards list
every open job, so a job missing from a later fetch has closed."""

from __future__ import annotations

from ...domain.job import Posting
from ...services.geo import country_from_location, to_iso2
from ...services.text import html_to_text
from ..http import Http
from .base import FetchOutcome, SourceContext, iso_utc


class Greenhouse:
    name = "greenhouse"
    kind = "ats"
    base = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for c in ctx.companies:

            def unit(c=c):
                data = http.get_json(self.base.format(token=c.token), {"content": "true"})
                ids = set()
                for j in data.get("jobs", []):
                    loc = (j.get("location") or {}).get("name")
                    p = Posting(
                        source=self.name,
                        kind="ats",
                        source_job_id=f"{c.token}:{j['id']}",
                        company=c.name,
                        title=j.get("title") or "",
                        url=j.get("absolute_url") or "",
                        description=html_to_text(j.get("content"), unescape_first=True),
                        location=loc,
                        country=country_from_location(loc),
                        posted_at=iso_utc(j.get("first_published") or j.get("updated_at")),
                    )
                    ids.add(p.id)
                    out.postings.append(p)
                out.complete_ids[f"{self.name}:{c.token}"] = ids

            out.run_unit(c.token, unit)
        return out


class Ashby:
    name = "ashby"
    kind = "ats"
    base = "https://api.ashbyhq.com/posting-api/job-board/{token}"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for c in ctx.companies:

            def unit(c=c):
                data = http.get_json(self.base.format(token=c.token), {"includeCompensation": "true"})
                ids = set()
                for j in data.get("jobs", []):
                    if j.get("isListed") is False:
                        continue
                    addr = (j.get("address") or {}).get("postalAddress") or {}
                    comp = (j.get("compensation") or {}).get("compensationTierSummary")
                    remote = j.get("workplaceType") or ("Remote" if j.get("isRemote") else None)
                    p = Posting(
                        source=self.name,
                        kind="ats",
                        source_job_id=f"{c.token}:{j['id']}",
                        company=c.name,
                        title=j.get("title") or "",
                        url=j.get("jobUrl") or "",
                        description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                        location=j.get("location"),
                        country=to_iso2(addr.get("addressCountry")),
                        remote=remote,
                        posted_at=iso_utc(j.get("publishedAt")),
                        salary_text=comp,
                    )
                    ids.add(p.id)
                    out.postings.append(p)
                out.complete_ids[f"{self.name}:{c.token}"] = ids

            out.run_unit(c.token, unit)
        return out


class Lever:
    name = "lever"
    kind = "ats"
    base = "https://api.lever.co/v0/postings/{token}"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for c in ctx.companies:

            def unit(c=c):
                data = http.get_json(self.base.format(token=c.token), {"mode": "json"})
                ids = set()
                for j in data:
                    cats = j.get("categories") or {}
                    lists = "\n".join(
                        f"{x.get('text', '')}\n{html_to_text(x.get('content'))}" for x in j.get("lists", [])
                    )
                    desc = "\n".join(filter(None, [j.get("descriptionPlain"), lists, j.get("additionalPlain")]))
                    sr = j.get("salaryRange") or {}
                    salary = (
                        f"{sr.get('min')}–{sr.get('max')} {sr.get('currency', '')}".strip()
                        if sr.get("min") and sr.get("max")
                        else None
                    )
                    p = Posting(
                        source=self.name,
                        kind="ats",
                        source_job_id=f"{c.token}:{j['id']}",
                        company=c.name,
                        title=j.get("text") or "",
                        url=j.get("hostedUrl") or "",
                        description=desc,
                        location=cats.get("location"),
                        country=to_iso2(j.get("country")),
                        remote=j.get("workplaceType"),
                        posted_at=iso_utc(j.get("createdAt")),
                        salary_text=salary,
                    )
                    ids.add(p.id)
                    out.postings.append(p)
                out.complete_ids[f"{self.name}:{c.token}"] = ids

            out.run_unit(c.token, unit)
        return out


class Workable:
    """apply.workable.com public widget API (Hugging Face, Freedom24): the full board, so closures too."""

    name = "workable"
    kind = "ats"
    base = "https://apply.workable.com/api/v1/widget/accounts/{token}"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for c in ctx.companies:

            def unit(c=c):
                data = http.get_json(self.base.format(token=c.token), {"details": "true"})
                ids = set()
                for j in data.get("jobs", []):
                    locs = j.get("locations") or []
                    place = ", ".join(x for x in (j.get("city"), j.get("country")) if x) or None
                    remote = str(j.get("telecommuting")).lower() == "true"
                    p = Posting(
                        source=self.name, kind="ats", source_job_id=f"{c.token}:{j['shortcode']}",
                        company=c.name, title=j.get("title") or "", url=j.get("url") or j.get("shortlink") or "",
                        description=html_to_text(j.get("description")) or (j.get("title") or ""),
                        location=place,
                        country=(locs[0].get("countryCode") if len(locs) == 1 else None) or None,
                        remote="remote" if remote else None, posted_at=iso_utc(j.get("published_on")),
                        extra={"department": j.get("department"), "employment": j.get("employment_type")},
                    )
                    ids.add(p.id)
                    out.postings.append(p)
                out.complete_ids[f"{self.name}:{c.token}"] = ids

            out.run_unit(c.token, unit)
        return out
