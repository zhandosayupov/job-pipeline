"""Text helpers for postings: HTML to text, fingerprints, language, salary and visa/remote signals.

Signals never guess: no match means None (CLAUDE.md "unknown stays null"). Visa detection is
negation-aware: "visa sponsorship is not available" is a refusal, not an offer.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from html.parser import HTMLParser

# --- HTML to text ----------------------------------------------------------------------

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None, *, unescape_first: bool = False) -> str:
    """Plain text from HTML. `unescape_first` for feeds that HTML-escape their HTML (Greenhouse)."""
    if not raw:
        return ""
    if unescape_first:
        raw = html.unescape(raw)
    p = _TextExtractor()
    p.feed(raw)
    p.close()
    text = "".join(p.parts).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def clip(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- identity ----------------------------------------------------------------------------

_COMPANY_SUFFIX = re.compile(r"\b(inc|llc|ltd|gmbh|bv|b\.v|ag|sa|plc|corp|corporation|co|limited|technologies)\.?$")


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", (s or "").lower())
    s = re.sub(r"[^\w+#]+", " ", s)
    return " ".join(s.split())


def norm_company(name: str | None) -> str:
    s = _norm((name or "").replace(".", ""))  # "B.V." -> "bv" so the suffix rule sees it
    prev = None
    while prev != s:
        prev, s = s, _COMPANY_SUFFIX.sub("", s).strip()
    return s


def city_of(location: str | None) -> str:
    """First place name of a location string: 'Amsterdam, Netherlands' -> 'amsterdam'."""
    if not location:
        return ""
    first = re.split(r"[,;/|(]| - ", location)[0]
    return _norm(first)


def fingerprint(company: str | None, title: str | None, location: str | None) -> str:
    """Cross-source identity of a vacancy: company|title|city (normalised)."""
    return f"{norm_company(company)}|{_norm(title)}|{city_of(location)}"


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def detect_lang(text: str) -> str:
    letters = [c for c in text[:3000] if c.isalpha()]
    if not letters:
        return "en"
    cyr = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters)
    return "ru" if cyr / len(letters) > 0.3 else "en"


# --- signals ------------------------------------------------------------------------------

_SALARY = re.compile(
    r"(?:(?:€|\$|£|₸|EUR|USD|GBP|CHF|KZT|RUB|руб)\s?\d[\d\s,.]*(?:k|K|000)?(?:\s?(?:-|–|to|до)\s?(?:€|\$|£|₸)?\s?\d[\d\s,.]*(?:k|K)?)?"
    r"(?:\s?(?:€|EUR|USD|GBP|CHF|KZT|₸|руб\.?))?"
    r"|\d[\d\s,.]*(?:k|K)?\s?(?:-|–|to|до)\s?\d[\d\s,.]*(?:k|K)?\s?(?:€|EUR|USD|\$|GBP|£|CHF|KZT|₸|руб\.?))"
)


def salary_text(text: str) -> str | None:
    """A salary-looking amount: a range, or a single figure in thousands ("€90k", "120 000 ₸").
    A lone small number with a currency sign ("$17.5", often funding) is not a salary."""
    for m in _SALARY.finditer(text or ""):
        found = " ".join(m.group(0).split())
        is_range = re.search(r"\d\s?(?:-|–|to|до)\s?\D{0,3}\d", found) is not None
        thousands = re.search(r"\d\s?[kK](?![a-zA-Z])|\d{2,3}[\s,.]\d{3}|\d{5,}", found) is not None
        if is_range or thousands:
            return found
    return None


_NEG = r"(?:no|not|unable|cannot|can't|won't|don't|do not|does not|without|unfortunately|не|нет|без)"
_VISA_WORDS = r"(?:visa|sponsorship|sponsor|work permit|визов\w*|виз[ауы]?|спонсор\w*)"
_VISA_NEG = [
    re.compile(rf"\b{_NEG}\b[^.\n]{{0,40}}{_VISA_WORDS}", re.I),
    re.compile(
        rf"{_VISA_WORDS}[^.\n]{{0,40}}\b(?:is |are )?(?:not available|not provided|not possible|not offered|"
        r"не предоставля\w*|не оформля\w*)",
        re.I,
    ),
    re.compile(
        r"\b(?:must|need to|required to) (?:already )?(?:have|hold|possess) (?:the )?(?:right|authori[sz]ation) "
        r"to work",
        re.I,
    ),
    re.compile(r"\bright to work in [a-z ]+ (?:is )?required\b", re.I),
]
_VISA_POS = re.compile(
    r"\b(?:visa sponsorship(?! is not| not)|we (?:will |can |do )?sponsor|"
    r"sponsor(?:ship)? (?:is )?(?:available|provided|offered)|"
    r"(?:blue card|skilled worker visa|highly skilled migrant|kennismigrant|h-?1b) (?:sponsorship|support)|"
    r"помо\w+ с визой|оформ\w+ виз\w*|визовая поддержка)",
    re.I,
)
_RELOC = re.compile(
    r"\b(?:relocation (?:package|support|assistance|bonus)|relocate you|help (?:you )?relocate|"
    r"релокаци\w*|помощь с переездом)",
    re.I,
)


def visa_signal(text: str) -> tuple[str | None, str | None]:
    """('explicit' | 'no' | 'relocation_only' | None, evidence of at most ~15 words)."""
    t = text or ""
    for rx in _VISA_NEG:
        for m in rx.finditer(t):
            # Export-control boilerplate ("...under U.S. export laws without sponsorship for an export
            # license") talks about licences, not visas: found as a false positive on live Cloudflare data.
            window = t[max(0, m.start() - 160): m.end() + 80].lower()
            if "export" in window or "licen" in window:
                continue
            return "no", _evidence(t, m)
    m = _VISA_POS.search(t)
    if m:
        return "explicit", _evidence(t, m)
    m = _RELOC.search(t)
    if m:
        return "relocation_only", _evidence(t, m)
    return None, None


_REMOTE = re.compile(
    r"\b(?:remote|fully remote|work from anywhere|distributed team|удал[её]нн\w*|удаленка|remote-first)\b", re.I
)
_ONSITE = re.compile(r"\b(?:on-?site only|office only|no remote|в офисе|офис)\b", re.I)


def remote_signal(text: str) -> str | None:
    t = text or ""
    if _ONSITE.search(t) and not _REMOTE.search(t):
        return "onsite"
    if _REMOTE.search(t):
        return "remote"
    return None


def _evidence(text: str, m: re.Match, words: int = 15) -> str:
    start = max(0, text.rfind("\n", 0, m.start()) + 1, m.start() - 80)
    snippet = " ".join(text[start : m.end() + 80].split())
    return " ".join(snippet.split()[:words])


# --- excerpt -------------------------------------------------------------------------------

_PRIORITY = [
    re.compile(r"visa|sponsor|relocat|work permit|виз|релокац|переезд", re.I),
    re.compile(r"remote|anywhere|timezone|time zone|удал[её]н|часов\w* пояс", re.I),
    re.compile(r"\b\d+\+?\s*(?:years|yrs|лет|года)\b|senior|junior|middle|mid-level|сеньор|мидл|джун", re.I),
    re.compile(r"c\+\+|rust|golang|\bgo\b|java\b|python|linux|low[- ]latency|distributed|kubernetes", re.I),
    re.compile(r"salary|compensation|€|\$|£|₸|зарплат|оклад|вилка", re.I),
]


def excerpt(text: str, limit: int = 700) -> str:
    """≤ limit chars, sentences picked by priority: visa/relocation → remote → level → stack → salary."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if len(s.strip()) > 2]
    chosen: list[int] = []
    for rx in _PRIORITY:
        for i, s in enumerate(sentences):
            if i not in chosen and rx.search(s):
                chosen.append(i)
                break
    for i in range(len(sentences)):
        if i not in chosen:
            chosen.append(i)
    out, size = [], 0
    for i in chosen:
        s = sentences[i]
        if size + len(s) + 1 > limit:
            continue
        out.append((i, s))
        size += len(s) + 1
    return " ".join(s for _, s in sorted(out))
