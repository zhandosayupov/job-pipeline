"""Community sources: Hacker News "Who is hiring" and public Telegram channels (t.me/s, no login)."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ...domain.job import Posting
from ...services.geo import country_from_location
from ...services.text import clip, html_to_text
from ..http import Http
from .base import FetchOutcome, SourceContext, iso_utc

# --- Hacker News ------------------------------------------------------------------------


class HackerNews:
    name = "hn"
    kind = "community"
    search = "https://hn.algolia.com/api/v1/search"
    by_date = "https://hn.algolia.com/api/v1/search_by_date"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        story: dict = {}

        def find_story():
            data = http.get_json(self.by_date, {"tags": "story,author_whoishiring", "hitsPerPage": 5})
            hits = [h for h in data.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
            if not hits:
                raise ValueError("no 'Who is hiring' story found")
            story.update(hits[0])

        out.run_unit("story", find_story)
        if not story:
            return out
        sid = story["objectID"]
        seen: set[str] = set()
        for q in ctx.params.get("queries", ["c++", "visa", "relocation", "remote"]):

            def unit(q=q):
                data = http.get_json(self.search, {"tags": f"comment,story_{sid}", "query": q, "hitsPerPage": 100})
                for c in data.get("hits", []):
                    if str(c.get("parent_id")) != str(sid) or c["objectID"] in seen:
                        continue  # top-level comments only: replies are not job posts
                    seen.add(c["objectID"])
                    out.postings.append(parse_hn_comment(c))

            out.run_unit(f"q={q}", unit)
        return out


def parse_hn_comment(c: dict) -> Posting:
    text = html_to_text(c.get("comment_text"))
    first = text.split("\n", 1)[0]
    parts = [p.strip() for p in re.split(r"\s+\|\s+", first) if p.strip()]
    company = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else clip(first, 120) or ""
    location = next(
        (p for p in parts[2:] if not re.search(r"remote|onsite|on-site|full[- ]time|\$|€|visa", p, re.I)), None
    )
    remote = next((p for p in parts if re.search(r"remote", p, re.I)), None)
    return Posting(
        source="hn",
        kind="community",
        source_job_id=str(c["objectID"]),
        company=clip(company, 80) or "",
        title=clip(title, 160) or "",
        url=f"https://news.ycombinator.com/item?id={c['objectID']}",
        description=text,
        location=location,
        country=country_from_location(location),
        remote=remote,
        posted_at=iso_utc(c.get("created_at")),
    )


# --- Telegram public channels -----------------------------------------------------------


class _TgParser(HTMLParser):
    """Collects (post_id, datetime, text) from the t.me/s/<channel> web preview."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._cur: dict | None = None
        self._text_depth = 0
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            if "data-post" in a and "tgme_widget_message" in cls.split():
                self._cur = {"post": a["data-post"], "text": [], "datetime": None}
                self.posts.append(self._cur)
            if self._text_depth:
                self._div_depth += 1
            elif self._cur is not None and "tgme_widget_message_text" in cls.split():
                self._text_depth, self._div_depth = 1, 1
        elif tag == "br" and self._text_depth:
            self._cur["text"].append("\n")
        elif tag == "time" and self._cur is not None and a.get("datetime") and not self._cur["datetime"]:
            self._cur["datetime"] = a["datetime"]

    def handle_endtag(self, tag):
        if tag == "div" and self._text_depth:
            self._div_depth -= 1
            if self._div_depth == 0:
                self._text_depth = 0

    def handle_data(self, data):
        if self._text_depth and self._cur is not None:
            self._cur["text"].append(data)


def parse_tg_page(html: str) -> list[dict]:
    p = _TgParser()
    p.feed(html)
    out = []
    for post in p.posts:
        text = "\n".join(" ".join(line.split()) for line in "".join(post["text"]).splitlines()).strip()
        if text:
            out.append({"post": post["post"], "datetime": post["datetime"], "text": text})
    return out


class TelegramChannels:
    """Public channels via the web preview; remembers the last post id per channel."""

    name = "telegram"
    kind = "community"
    url = "https://t.me/s/{channel}"

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome:
        out = FetchOutcome()
        for ch in ctx.params.get("channels", []):

            def unit(ch=ch):
                last = int(ctx.cursors.get(f"last_id:{ch}", "0"))
                newest = last
                for post in parse_tg_page(http.get_text(self.url.format(channel=ch))):
                    pid = int(post["post"].rsplit("/", 1)[-1])
                    if pid <= last:
                        continue
                    newest = max(newest, pid)
                    first = post["text"].split("\n", 1)[0]
                    out.postings.append(
                        Posting(
                            source=self.name,
                            kind="community",
                            source_job_id=f"{ch}:{pid}",
                            company="",
                            title=clip(first, 160) or "",
                            url=f"https://t.me/{ch}/{pid}",
                            description=post["text"],
                            posted_at=iso_utc(post["datetime"]),
                            extra={"channel": ch},
                        )
                    )
                ctx.cursors[f"last_id:{ch}"] = str(newest)

            out.run_unit(ch, unit)
        return out
