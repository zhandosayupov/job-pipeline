"""Remote/relocation aggregators and Amazon's search. None of them lists every job, so closures are
not inferred from them."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ...domain.job import Posting
from ...services.geo import country_from_location, to_iso2
from ...services.text import html_to_text, short_hash
from ..http import Http
from .base import FetchOutcome, SourceContext, iso_utc


class Himalayas:
    """No page parameter: the site's robots.txt asks crawlers not to page through results."""

    name = "himalayas"
    kind = "aggregator"
    url = "https://himalayas.app/jobs/api"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for q in ctx.params.get("queries", ["c++"]):

            def unit(q=q):
                data = http.get_json(self.url, {"q": q, "limit": ctx.params.get("limit", 50)})
                for j in data.get("jobs", []):
                    restr = j.get("locationRestrictions") or []
                    salary = None
                    if j.get("minSalary") and j.get("maxSalary"):
                        salary = f"{j['minSalary']}–{j['maxSalary']} {j.get('currency') or ''}".strip()
                    out.postings.append(
                        Posting(
                            source=self.name,
                            kind="aggregator",
                            source_job_id=short_hash(j.get("guid") or j["title"]),
                            company=j.get("companyName") or "",
                            title=j.get("title") or "",
                            url=j.get("applicationLink") or j.get("guid") or "",
                            description=html_to_text(j.get("description")),
                            location=", ".join(restr) or "Worldwide",
                            country=to_iso2(restr[0]) if len(restr) == 1 else None,
                            remote="remote: " + (", ".join(restr) or "worldwide"),
                            posted_at=iso_utc(j.get("pubDate")),
                            salary_text=salary,
                            extra={"seniority": j.get("seniority")},
                        )
                    )

            out.run_unit(f"q={q}", unit)
        return out


class Jobicy:
    name = "jobicy"
    kind = "aggregator"
    url = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for tag in ctx.params.get("tags", ["c++"]):

            def unit(tag=tag):
                data = http.get_json(self.url, {"count": ctx.params.get("count", 50), "tag": tag})
                for j in data.get("jobs", []):
                    lo, hi = (
                        j.get("salaryMin") or j.get("annualSalaryMin"),
                        j.get("salaryMax") or j.get("annualSalaryMax"),
                    )
                    salary = f"{lo}–{hi} {j.get('salaryCurrency') or ''}".strip() if lo and hi else None
                    out.postings.append(
                        Posting(
                            source=self.name,
                            kind="aggregator",
                            source_job_id=str(j["id"]),
                            company=j.get("companyName") or "",
                            title=j.get("jobTitle") or "",
                            url=j.get("url") or "",
                            description=html_to_text(j.get("jobDescription")),
                            location=j.get("jobGeo"),
                            remote="remote: " + (j.get("jobGeo") or "unspecified"),
                            posted_at=iso_utc(j.get("pubDate")),
                            salary_text=salary,
                            extra={"level": j.get("jobLevel")},
                        )
                    )

            out.run_unit(f"tag={tag}", unit)
        return out


class WeWorkRemotely:
    name = "wwr"
    kind = "aggregator"
    url = "https://weworkremotely.com/categories/{category}.rss"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for cat in ctx.params.get("categories", ["remote-back-end-programming-jobs"]):

            def unit(cat=cat):
                root = ET.fromstring(http.get_text(self.url.format(category=cat)))
                for item in root.iter("item"):
                    raw_title = (item.findtext("title") or "").strip()
                    company, _, title = raw_title.partition(":")
                    if not title:
                        company, title = "", raw_title
                    link = (item.findtext("link") or "").strip()
                    region = (item.findtext("region") or "").strip()
                    out.postings.append(
                        Posting(
                            source=self.name,
                            kind="aggregator",
                            source_job_id=short_hash(item.findtext("guid") or link),
                            company=company.strip(),
                            title=title.strip(),
                            url=link,
                            description=html_to_text(item.findtext("description")),
                            location=region or None,
                            country=to_iso2(item.findtext("country")),
                            remote="remote: " + (region or "unspecified"),
                            posted_at=iso_utc(item.findtext("pubDate")),
                        )
                    )

            out.run_unit(cat, unit)
        return out


class Arbeitnow:
    name = "arbeitnow"
    kind = "aggregator"
    url = "https://www.arbeitnow.com/api/job-board-api"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()

        def unit():
            data = http.get_json(self.url)
            for j in data.get("data", []):
                loc = j.get("location")
                out.postings.append(
                    Posting(
                        source=self.name,
                        kind="aggregator",
                        source_job_id=j.get("slug") or short_hash(j.get("url", "")),
                        company=j.get("company_name") or "",
                        title=j.get("title") or "",
                        url=j.get("url") or "",
                        description=html_to_text(j.get("description")),
                        location=loc,
                        country=country_from_location(loc) or "DE",  # a German job board: unstated country is DE
                        remote="remote" if j.get("remote") else None,
                        posted_at=iso_utc(j.get("created_at")),
                        extra={"tags": j.get("tags")},
                    )
                )

        out.run_unit("page1", unit)
        return out


class Amazon:
    """amazon.jobs search, one unit per country (ISO3), pages of 8 by offset."""

    name = "amazon"
    kind = "ats"
    url = "https://www.amazon.jobs/en/search.json"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for iso3 in ctx.params.get("countries", ["NLD"]):

            def unit(iso3=iso3):
                for page in range(int(ctx.params.get("max_pages", 3))):
                    data = http.get_json(
                        self.url,
                        {
                            "base_query": ctx.params.get("query", "c++"),
                            "result_limit": 8,
                            "offset": page * 8,
                            "normalized_country_code[]": iso3,
                        },
                    )
                    jobs = data.get("jobs", [])
                    for j in jobs:
                        desc = "\n".join(
                            filter(
                                None,
                                [
                                    html_to_text(j.get("description")),
                                    html_to_text(j.get("basic_qualifications")),
                                    html_to_text(j.get("preferred_qualifications")),
                                ],
                            )
                        )
                        out.postings.append(
                            Posting(
                                source=self.name,
                                kind="ats",
                                source_job_id=str(j.get("id_icims") or j.get("id")),
                                company="Amazon",
                                title=j.get("title") or "",
                                url="https://www.amazon.jobs" + (j.get("job_path") or ""),
                                description=desc,
                                location=j.get("normalized_location") or j.get("location"),
                                country=to_iso2(j.get("country_code")),
                                posted_at=iso_utc(j.get("posted_date")),
                            )
                        )
                    if len(jobs) < 8:
                        break

            out.run_unit(iso3, unit)
        return out
