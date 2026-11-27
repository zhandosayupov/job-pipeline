"""Truth gate (spec §2.1): every claim in generated text must match the knowledge base.

Deterministic extraction of the claims that matter in a CV summary or a cover letter:
  number      35%, 2 billion, 2B, 10x, 3 years ...   must occur in a fact (normalised)
  year        2019, 2023 ...                          must be a role date, or occur in a fact/education
  tech        C++, Kubernetes, Rust ...               must be in the KB's technologies, unless the
                                                      sentence is an honest gap ("I have not used Rust")
  employer    "worked at X", "while at X", "my time at X"   X must be one of the owner's employers
  title       "as a Senior Engineer", "I am a Staff ..."      must match a role title or the headline
  degree      B.Sc, Master, PhD, бакалавр ...                 must occur in the profile's education

Anything unmatched blocks the artifact and goes to the owner as a question. The CV body is built
from facts (ADR 0008), so this mainly guards the free text: summary and letter.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .kb import KnowledgeBase

TECH = [
    "C++", "C#", "C", "Rust", "Go", "Golang", "Java", "Kotlin", "Scala", "Python", "JavaScript", "TypeScript",
    "Haskell", "OCaml", "Erlang", "Elixir", "Ruby", "PHP", "Swift", "Lua", "Zig", "Julia", "R", "SQL",
    "Linux", "Kubernetes", "Docker", "Terraform", "Ansible", "AWS", "GCP", "Azure", "Kafka", "RabbitMQ",
    "Redis", "PostgreSQL", "MySQL", "MongoDB", "Cassandra", "ClickHouse", "Elasticsearch", "Spark", "Hadoop",
    "Flink", "Airflow", "gRPC", "Protobuf", "GraphQL", "REST", "CUDA", "OpenMP", "MPI", "FPGA", "Verilog",
    "DPDK", "RDMA", "eBPF", "LLVM", "CMake", "Bazel", "Git", "Grafana", "Prometheus", "PyTorch", "TensorFlow",
    "Pandas", "NumPy", "React", "Angular", "Vue", "Django", "Flask", "FastAPI", "Spring", "Node.js", "perf",
    "Valgrind", "GDB", "Boost", "STL", "Qt", "Solidity", "Nginx",
]
_TECH_RX = [(t, re.compile(rf"(?<![\w+#.]){re.escape(t)}(?![\w+#])", re.I if len(t) > 2 else 0)) for t in TECH]
# An honest gap is a negation OF USING the technology, right before it ("I have not used Rust",
# "no production experience with Go"). A negation elsewhere in the sentence ("never doubted my Rust",
# "in Rust, not Python") exempts nothing (S3 review).
_GAP_PHRASE = (r"(?:(?:have|has|had|did)\s?n[o']t\s+(?:yet\s+)?(?:used|worked\s+(?:with|in)|written|shipped)|"
               r"not\s+(?:yet\s+)?(?:used|worked\s+(?:with|in))|never\s+(?:used|worked\s+(?:with|in)|written)|"
               r"no\s+(?:production\s+|commercial\s+|hands-on\s+)?experience\s+(?:with|in)|yet\s+to\s+(?:use|learn)|"
               r"(?:currently\s+)?learning|new\s+to|не\s+работал[аи]?\s+с|нет\s+(?:коммерческого\s+)?опыта"
               r"(?:\s+работы)?\s+(?:с|в)|изучаю)")


def _gap_before(sent: str, tech_rx: re.Pattern) -> bool:
    for m in tech_rx.finditer(sent):
        head = sent[: m.start()]
        if re.search(_GAP_PHRASE + r"\W+(?:[\w+#.-]+\W+){0,3}$", head, re.I):
            return True
    return False


_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s?(%|x|×|k|K|M|B|bn|billion|million|thousand|млрд|млн|тыс)?"
                     r"(?![\w.]*\d)")
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
_EMPLOYER = re.compile(r"(?:worked at|working at|work at|while at|my time at|years at|interned at|"
                       r"работал[аи]? в|работаю в)\s+([A-ZА-Я][\w&.\-]*(?:\s+[A-ZА-Я][\w&.\-]*){0,3})")
_TITLE = re.compile(r"(?:\bas an?|\bI am an?|\bI'm an?|\bcurrently an?)\s+((?:Senior|Lead|Staff|Principal|Junior|"
                    r"Middle|Head|Chief|Senior Staff)\b[\w+#/ ()-]{0,40})")
_DEGREE = re.compile(r"\b(B\.?Sc|BSc|B\.?S\.|Bachelor|M\.?Sc|MSc|M\.?S\.|Master|Ph\.?D|PhD|MBA|бакалавр\w*|"
                     r"магистр\w*|кандидат наук)\b", re.I)
_SCALE = {"%": "%", "x": "x", "×": "x", "k": "e3", "thousand": "e3", "тыс": "e3", "m": "e6", "million": "e6",
          "млн": "e6", "b": "e9", "bn": "e9", "billion": "e9", "млрд": "e9"}


@dataclass
class Claim:
    kind: str
    text: str
    ok: bool
    fact_id: str | None = None


@dataclass
class TruthReport:
    claims: list[Claim] = field(default_factory=list)

    @property
    def unmatched(self) -> list[Claim]:
        return [c for c in self.claims if not c.ok]

    @property
    def ok(self) -> bool:
        return not self.unmatched


_EXPERIENCE = re.compile(r"(\d+(?:[.,]5)?)\+?\s*(?:years?|yrs?|лет|года?)\b(?:\s+(?:of\s+)?(?:experience|опыта))?",
                         re.I)


def _experience_years(kb: KnowledgeBase, today: str | None = None) -> float:
    """Total months across roles (overlaps counted once), in years, rounded to the half year."""
    from datetime import date

    now = today or date.today().strftime("%Y-%m")
    months: set[tuple[int, int]] = set()
    for r in kb.roles:
        y, m = map(int, r.start[:7].split("-"))
        ey, em = map(int, (r.end or now)[:7].split("-"))
        while (y, m) <= (ey, em):
            months.add((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return round(len(months) / 12 * 2) / 2


def _num_key(value: str, unit: str | None) -> str:
    v = value.replace(",", ".")
    try:
        f = float(v)
    except ValueError:
        return value
    v = str(int(f)) if f.is_integer() else str(f)
    return v + (_SCALE.get((unit or "").lower(), "") if unit else "")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


class TruthGate:
    def __init__(self, kb: KnowledgeBase, allowed_extra: list[str] | None = None):
        """allowed_extra: text the generated item may also quote freely, e.g. the target company's
        name and the job title (a letter says "the C++ role at IMC" without claiming anything)."""
        self.kb = kb
        self.fact_texts = [(f.id, w) for f in kb.facts for w in f.wordings()]
        self.numbers: dict[str, str] = {}
        for fid, w in self.fact_texts:
            for m in _NUMBER.finditer(w):
                self.numbers.setdefault(_num_key(m.group(1), m.group(2)), fid)
        edu = " ".join(kb.profile.education)
        for m in _NUMBER.finditer(edu):
            self.numbers.setdefault(_num_key(m.group(1), m.group(2)), "profile.education")
        self.years = {r.start[:4] for r in kb.roles} | {(r.end or "")[:4] for r in kb.roles}
        self.years |= set(_YEAR.findall(edu)) | {y for _, w in self.fact_texts for y in _YEAR.findall(w)}
        self.tech = {t.lower() for t in kb.technologies()}
        self.employers = {e.lower() for e in kb.employers()}
        self.titles = [r.title.lower() for r in kb.roles] + [kb.profile.headline.lower()]
        self.education = edu.lower()
        self.extra = " ".join(allowed_extra or []).lower()
        self.experience_years = _experience_years(kb)

    def check(self, text: str) -> TruthReport:
        rep = TruthReport()
        for sent in _sentences(text):
            for m in _EXPERIENCE.finditer(sent):
                n = float(m.group(1).replace(",", "."))
                rep.claims.append(Claim("experience", m.group(0), n <= self.experience_years + 0.01))
            for m in _YEAR.finditer(sent):
                y = m.group(1)
                rep.claims.append(Claim("year", y, y in self.years))
            sent_wo_years = _YEAR.sub(" ", _EXPERIENCE.sub(" ", sent))  # checked above, not as numbers
            for m in _NUMBER.finditer(sent_wo_years):
                key = _num_key(m.group(1), m.group(2))
                if not m.group(2) and len(m.group(1)) == 1:
                    continue  # "one", "2 teams": bare single digits are too noisy to gate on
                fid = self.numbers.get(key)
                ok = fid is not None or m.group(0).strip().lower() in self.extra
                rep.claims.append(Claim("number", m.group(0).strip(), ok, fid))
            for name, rx in _TECH_RX:
                if rx.search(sent):
                    known = name.lower() in self.tech or (name.lower() == "go" and "golang" in self.tech)
                    rep.claims.append(Claim("tech", name, known or _gap_before(sent, rx)))
            for m in _EMPLOYER.finditer(sent):
                org = m.group(1).strip().rstrip(".,")
                ok = any(org.lower().startswith(e) or e.startswith(org.lower()) for e in self.employers)
                rep.claims.append(Claim("employer", org, ok))
            for m in _TITLE.finditer(sent):
                title = m.group(1).strip().lower()
                ok = any(title.split()[0] in t and (len(title.split()) < 2 or title.split()[1] in t)
                         for t in self.titles if t)
                rep.claims.append(Claim("title", m.group(1).strip(), ok))
            for m in _DEGREE.finditer(sent):
                d = m.group(1).lower().replace(".", "")
                rep.claims.append(Claim("degree", m.group(1), d[:3] in self.education.replace(".", "")))
        # de-duplicate identical claims
        seen, uniq = set(), []
        for c in rep.claims:
            k = (c.kind, c.text.lower(), c.ok)
            if k not in seen:
                seen.add(k)
                uniq.append(c)
        rep.claims = uniq
        return rep
