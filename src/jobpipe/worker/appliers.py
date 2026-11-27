"""Platform appliers driven by selectors in appliers.yaml (spec §9: selectors in config, not code).

One generic flow covers the hosted ATS forms and hh.kz:
  open -> optional clicks to reveal the form -> scan -> fill -> screenshot -> (on Approve) submit -> confirm.
A missing selector is a failure with a screenshot; nothing is retried blindly.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..services.forms import FormField
from .page import Page
from .scan import scan_form

DEFAULT_CAPTCHA = ["g-recaptcha", "h-captcha", "cf-challenge", "challenge-platform", "captcha"]


class ApplierCfg(BaseModel):
    name: str
    url_suffix: str = ""                                          # appended to the job URL to reach the form
    reveal_clicks: list[str] = Field(default_factory=list)       # e.g. hh: the "Откликнуться" button
    form_html_selector: str | None = None                        # reserved: scan a sub-tree only
    submit: str                                                   # submit button selector
    confirm_patterns: list[str] = Field(default_factory=list)    # regexes on the page HTML after submit
    confirm_selectors: list[str] = Field(default_factory=list)
    captcha_markers: list[str] = Field(default_factory=lambda: list(DEFAULT_CAPTCHA))
    settle_s: float = 2.0


class AppliersCfg(BaseModel):
    platforms: dict[str, ApplierCfg]


def load_appliers(path: Path) -> AppliersCfg:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppliersCfg(platforms={k: ApplierCfg(name=k, **v) for k, v in (data.get("platforms") or {}).items()})


class ApplyError(Exception):
    pass


class Applier:
    def __init__(self, cfg: ApplierCfg, page: Page):
        self.cfg = cfg
        self.page = page

    def open(self, url: str) -> None:
        self.page.goto(url.rstrip("/") + self.cfg.url_suffix if self.cfg.url_suffix else url)
        self.page.wait(self.cfg.settle_s)
        for sel in self.cfg.reveal_clicks:
            if not self.page.exists(sel):
                raise ApplyError(f"reveal selector not found: {sel}")
            self.page.click(sel)
            self.page.wait(self.cfg.settle_s)

    def captcha(self) -> bool:
        html = self.page.html().lower()
        return any(m.lower() in html for m in self.cfg.captcha_markers)

    def scan(self) -> list[FormField]:
        fields = scan_form(self.page.html())
        if not fields:
            raise ApplyError("no form fields found on the page")
        return fields

    def fill(self, fields: list[FormField], answers: list[dict], files: dict[str, str]) -> None:
        """answers: [{"key", "value"}] from the VM. `files` maps artifact values to local paths."""
        kinds = {f.key: f.kind for f in fields}
        for a in answers:
            key, value = a["key"], a["value"]
            kind = kinds.get(key, "text")
            if kind == "file":
                local = files.get(value)
                if local is None:
                    raise ApplyError(f"no local file for {value}")
                self.page.upload(key, local)
            elif kind in ("select", "radio", "checkbox"):
                self.page.choose(key, value, kind)
            else:
                self.page.fill(key, value)

    def submit(self) -> bool:
        """Click submit. Returns True if a confirmation is seen afterwards."""
        if not self.page.exists(self.cfg.submit):
            raise ApplyError(f"submit selector not found: {self.cfg.submit}")
        self.clicked = True                     # from here on the outcome is unknown, whatever raises
        self.page.click(self.cfg.submit)
        self.page.wait(self.cfg.settle_s * 2)
        return self.confirmed()

    def confirmed(self) -> bool:
        html = self.page.html()
        if any(re.search(p, html, re.I) for p in self.cfg.confirm_patterns):
            return True
        return any(self.page.exists(s) for s in self.cfg.confirm_selectors)
