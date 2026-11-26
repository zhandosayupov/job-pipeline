"""CV rendering: LaTeX from the template and the chosen facts; PDF with tectonic when installed.

Content comes only from the knowledge base and the tailoring plan (ADR 0008). The page limit is
enforced by dropping the least relevant bullets until the PDF has one page.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from string import Template

from ..config.settings import REPO_ROOT
from .kb import KnowledgeBase

_TEX_SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
                 "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex_escape(s: str) -> str:
    return "".join(_TEX_SPECIALS.get(ch, ch) for ch in s)


@dataclass
class CvContent:
    summary: str
    bullets: list[tuple[str, str]]          # (fact_id, wording)
    skills_order: list[str]


def _month(ym: str | None) -> str:
    if not ym:
        return "present"
    y, m = ym[:7].split("-")
    return f"{['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][int(m) - 1]} {y}"


def build_tex(kb: KnowledgeBase, content: CvContent, template: Path | None = None) -> str:
    tpl = Template((template or REPO_ROOT / "templates" / "cv.tex").read_text(encoding="utf-8"))
    p = kb.profile
    contact_bits = [x for x in (p.location, p.email, p.phone, *p.links.values()) if x]
    by_role: dict[str | None, list[str]] = {}
    for fid, wording in content.bullets:
        f = kb.fact(fid)
        by_role.setdefault(f.role if f else None, []).append(wording)
    exp = []
    for r in kb.roles:
        items = by_role.get(r.id)
        if not items:
            continue
        head = (rf"\textbf{{{tex_escape(r.title)}}}, {tex_escape(r.employer)}\hfill "
                rf"{_month(r.start)} -- {_month(r.end)}\par")
        exp.append(head + "\n\\begin{itemize}\n" + "\n".join(rf"\item {tex_escape(b)}" for b in items)
                   + "\n\\end{itemize}")
    general = by_role.get(None, [])
    edu = [tex_escape(e) + r"\par" for e in p.education] + [tex_escape(g) + r"\par" for g in general]
    skills = []
    order = {s.lower(): i for i, s in enumerate(content.skills_order)}
    for group, items in p.skills.items():
        items = sorted(items, key=lambda s: order.get(s.lower(), 999))
        skills.append(rf"\textbf{{{tex_escape(group)}:}} " + ", ".join(tex_escape(s) for s in items) + r"\par")
    if p.languages:
        skills.append(r"\textbf{Languages:} " + ", ".join(tex_escape(x) for x in p.languages) + r"\par")
    summary = (r"\vspace{4pt}" + tex_escape(content.summary) + r"\par") if content.summary else ""
    return tpl.safe_substitute(
        name=tex_escape(p.name), contact=r" \textbullet{} ".join(tex_escape(c) for c in contact_bits),
        summary=summary, experience="\n".join(exp), education="\n".join(edu), skills="\n".join(skills),
    )


def tectonic_available() -> bool:
    return shutil.which("tectonic") is not None


def pdf_pages(pdf: Path) -> int:
    """Page count from the PDF page tree (good enough for tectonic output)."""
    data = pdf.read_bytes()
    counts = [int(x) for x in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)]
    return max(counts) if counts else len(re.findall(rb"/Type\s*/Page\b", data))


def compile_pdf(tex_path: Path, timeout_s: int = 180) -> Path | None:
    """Compile with tectonic if installed; None when it is not (the .tex is still delivered)."""
    if not tectonic_available():
        return None
    subprocess.run(["tectonic", "--keep-logs", "--outdir", str(tex_path.parent), str(tex_path)],
                   check=True, timeout=timeout_s, capture_output=True)
    pdf = tex_path.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def render_one_page(kb: KnowledgeBase, content: CvContent, out_dir: Path, name: str = "cv") -> tuple[Path, Path | None]:
    """Write <name>.tex (and .pdf when possible); drop trailing bullets until the PDF is one page."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bullets = list(content.bullets)
    while True:
        tex = out_dir / f"{name}.tex"
        tex.write_text(build_tex(kb, CvContent(content.summary, bullets, content.skills_order)), encoding="utf-8")
        pdf = compile_pdf(tex)
        if pdf is None or pdf_pages(pdf) <= 1 or len(bullets) <= 3:
            return tex, pdf
        bullets = bullets[:-1]
