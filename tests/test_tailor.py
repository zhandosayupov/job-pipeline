"""S3: knowledge base, truth gate, rendering, tailoring plan, letter, approval cards."""

import json
from pathlib import Path

import pytest

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.services import cards, render, tailor
from jobpipe.services import kb as kbmod
from jobpipe.services.events import create_entity, transition
from jobpipe.services.llm_router import LLMUnavailable
from jobpipe.services.truth import TruthGate, _experience_years

from .conftest import REPO

KB_DIR = REPO / "config.example" / "kb"


@pytest.fixture
def kb():
    return kbmod.load(KB_DIR)


# --- knowledge base -------------------------------------------------------------------------


def test_example_kb_loads_and_is_consistent(kb):
    assert kb.profile.name == "Alex Example" and len(kb.facts) >= 5
    assert "C++" in kb.technologies() and "SearchCo" in kb.employers()
    assert kb.answer_for("What is your notice period?").topic == "notice_period"
    assert kb.answer_for("Favourite colour?") is None
    assert kb.pii_values()["NAME"][0] == "Alex Example"


def test_kb_rejects_duplicates_and_unknown_roles(kb):
    with pytest.raises(ValueError, match="duplicate"):
        kbmod.KnowledgeBase(profile=kb.profile, facts=[kb.facts[0], kb.facts[0]], roles=kb.roles)
    bad = kb.facts[0].model_copy(update={"role": "nope"})
    with pytest.raises(ValueError, match="unknown roles"):
        kbmod.KnowledgeBase(profile=kb.profile, facts=[bad], roles=kb.roles)


# --- truth gate ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,ok,kinds",
    [
        ("At SearchCo I cut ranking p99 latency by 35% and built an indexer for 2 billion documents a day.", True, []),
        ("I improved throughput by 50% using Rust.", False, ["number", "tech"]),
        ("I have not used Rust in production yet, but I am keen to learn it.", True, []),
        ("I worked at Google as a Senior Engineer.", False, ["employer", "title"]),
        ("I hold a PhD from 2015.", False, ["year", "degree"]),
        ("I hold a B.Sc. in Computer Science (2023).", True, []),
        ("I am excited about the C++ Engineer role at IMC.", True, []),
        ("I bring 12 years of experience.", False, ["experience"]),
    ],
)
def test_truth_gate(kb, text, ok, kinds):
    rep = TruthGate(kb, allowed_extra=["IMC", "C++ Engineer"]).check(text)
    assert rep.ok == ok
    assert sorted({c.kind for c in rep.unmatched}) == sorted(kinds)


def test_experience_years_counts_overlaps_once(kb):
    assert _experience_years(kb, today="2024-01") == 2.0  # 2022-02 .. 2024-01 = 24 months


def test_every_example_fact_passes_its_own_gate(kb):
    gate = TruthGate(kb)
    for f in kb.facts:
        for w in f.wordings():
            assert gate.check(w).ok, (f.id, w)


# --- rendering --------------------------------------------------------------------------------


def test_tex_escape_and_build(kb):
    assert render.tex_escape("50% & C# {x}_$") == r"50\% \& C\# \{x\}\_\$"
    content = render.CvContent(
        "C++ engineer",
        [("searchco-latency", kb.fact("searchco-latency").statement), ("award-icpc", kb.fact("award-icpc").statement)],
        ["Go", "C++"],
    )
    tex = render.build_tex(kb, content)
    assert r"\textbf{Software Engineer (C++)}, SearchCo" in tex
    assert r"by 35\% by redesigning" in tex
    assert "ICPC regional finalist" in tex  # role-less facts go under Education
    assert "Go, C++" in tex.split("Languages:")[1]  # skills reordered by the plan
    assert "$" not in tex.replace(r"\$", "")  # every placeholder filled


def test_pdf_pages_and_no_tectonic(tmp_path, kb, monkeypatch):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.5\n1 0 obj << /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >> endobj")
    assert render.pdf_pages(pdf) == 2
    monkeypatch.setattr(render, "tectonic_available", lambda: False)
    tex, out = render.render_one_page(kb, render.CvContent("", [("lab-sim", "x")], []), tmp_path)
    assert tex.exists() and out is None


def test_one_page_loop_drops_bullets(tmp_path, kb, monkeypatch):
    pages = iter([3, 2, 1])
    monkeypatch.setattr(render, "compile_pdf", lambda tex: tex)
    monkeypatch.setattr(render, "pdf_pages", lambda p: next(pages))
    bullets = [(f.id, f.statement) for f in kb.facts]
    tex, _ = render.render_one_page(kb, render.CvContent("", bullets, []), tmp_path)
    assert tex.read_text(encoding="utf-8").count(r"\item") == len(bullets) - 2


# --- plan -------------------------------------------------------------------------------------


def test_clean_plan_drops_unknown_and_falls_back(kb):
    plan = tailor.TailorPlan(
        bullets=[tailor.PlanBullet(fact_id="nope"), tailor.PlanBullet(fact_id="lab-sim", phrasing=9)],
        skills_order=["Go", "Cobol"],
    )
    cleaned, notes = tailor.clean_plan(kb, plan, "C++ low latency trading search engineer")
    assert len(cleaned.bullets) >= 3 and any("несуществующий" in n for n in notes)
    assert all(kb.fact(b.fact_id) for b in cleaned.bullets)
    assert cleaned.skills_order == ["Go"]


def test_fallback_plan_prefers_overlap(kb):
    plan = tailor.fallback_plan(kb, "Market making C++ low latency, order book, OpenMP", n=2)
    assert plan.bullets[0].fact_id == "lab-sim"


# --- the step --------------------------------------------------------------------------------


class FakeRouter:
    def __init__(self, plan=None, letters=None):
        self.plan = plan
        self.letters = list(letters or [])
        self.calls = []

    def complete(self, task, variables, schema=None, personal=False):
        self.calls.append((task, personal, variables))
        assert personal, "tailoring requests carry personal data"
        if task == "tailor":
            if self.plan is None:
                raise LLMUnavailable("tailor", [])
            return type("C", (), {"data": self.plan})()
        return type("C", (), {"data": tailor.Letter(letter=self.letters.pop(0))})()


GOOD_PLAN = tailor.TailorPlan(
    variant="hft",
    summary="C++ engineer who cut ranking p99 latency by 35%.",
    bullets=[tailor.PlanBullet(fact_id=i) for i in ("searchco-latency", "searchco-index", "lab-sim")],
    skills_order=["C++"],
    requirements=[tailor.Requirement(requirement="low latency C++", fact_ids=["searchco-latency"])],
    gaps=["Rust"],
)
GOOD_LETTER = (
    "I would like to join IMC as a C++ Engineer. At SearchCo I cut ranking p99 latency by 35%. "
    "I have not used Rust in production yet."
)
BAD_LETTER = "I spent 9 years at Google building Rust systems that served 10 billion users."


@pytest.fixture
def env(app_cfg, clock, monkeypatch):
    monkeypatch.setattr(render, "tectonic_available", lambda: False)
    conn = open_db(app_cfg.paths.db_path, clock)
    create_entity(
        conn,
        clock,
        "job",
        "gh:imc:1",
        columns={
            "source": "greenhouse",
            "kind": "ats",
            "company": "IMC",
            "title": "C++ Engineer",
            "url": "https://x/1",
        },
        data={"description": "Low latency C++ for market making. Rust is a plus.", "lang": "en"},
    )
    for s in ("prefiltered", "labeled", "shortlisted"):
        transition(conn, clock, "job", "gh:imc:1", s)
    create_entity(
        conn,
        clock,
        "application",
        "app:gh:imc:1",
        columns={"job_id": "gh:imc:1", "idempotency_key": "apply:imc:gh:imc:1"},
        data={"manual": False},
    )
    return conn


def app_state(conn):
    row = conn.execute("SELECT state, data_json FROM applications").fetchone()
    return row[0], json.loads(row[1])


def test_tailor_ready_with_artifacts_and_card(env, app_cfg, clock, kb):
    n = RecordingNotifier()
    [r] = tailor.run(env, clock, app_cfg, kb, FakeRouter(GOOD_PLAN, [GOOD_LETTER]), n)
    assert r.ok and set(r.artifacts) == {"tex", "letter", "plan"}
    state, data = app_state(env)
    assert state == "ready" and data["artifacts"]["tex"].endswith("cv.tex")
    plan = json.loads(Path(data["artifacts"]["plan"]).read_text(encoding="utf-8"))
    assert plan["summary"] == GOOD_PLAN.summary and plan["diff"]
    assert n.files and n.files[0][0].endswith("cv.tex")
    assert "Письмо:" in n.messages[-1] and n.buttons[-1][0][0][1].startswith("a:ok:")


def test_unbacked_summary_is_replaced_by_headline(env, app_cfg, clock, kb):
    plan = GOOD_PLAN.model_copy(update={"summary": "Ten years of Rust at Google."})
    [r] = tailor.run(env, clock, app_cfg, kb, FakeRouter(plan, [GOOD_LETTER]), RecordingNotifier())
    assert r.ok and any("summary заменён" in x for x in r.notes)
    data = json.loads(Path(r.artifacts["plan"]).read_text(encoding="utf-8"))
    assert data["summary"] == kb.profile.headline


def test_bad_letter_retried_then_blocked_and_not_repicked(env, app_cfg, clock, kb):
    n = RecordingNotifier()
    router = FakeRouter(GOOD_PLAN, [BAD_LETTER, BAD_LETTER])
    [r] = tailor.run(env, clock, app_cfg, kb, router, n)
    assert not r.ok and any("employer" in b for b in r.blocked)
    letter_calls = [c for c in router.calls if c[0] == "cover_letter"]
    assert len(letter_calls) == 2 and "DO NOT mention" in letter_calls[1][2]["gaps"]
    state, data = app_state(env)
    assert state == "draft" and data["blocked"]
    assert "Проверка правды" in n.messages[-1] and n.buttons[-1][0][0][1].startswith("a:redo:")
    assert tailor.run(env, clock, app_cfg, kb, FakeRouter(GOOD_PLAN, [GOOD_LETTER]), n) == []


def test_letter_fixed_on_retry(env, app_cfg, clock, kb):
    [r] = tailor.run(env, clock, app_cfg, kb, FakeRouter(GOOD_PLAN, [BAD_LETTER, GOOD_LETTER]), RecordingNotifier())
    assert r.ok


def test_llm_down_uses_deterministic_plan_but_needs_a_letter(env, app_cfg, clock, kb):
    class Down(FakeRouter):
        def complete(self, task, variables, schema=None, personal=False):
            raise LLMUnavailable(task, [])

    [r] = tailor.run(env, clock, app_cfg, kb, Down(), RecordingNotifier())
    assert not r.ok and r.blocked == ["письмо не получено от LLM"]
    assert any("детерминированный план" in x for x in r.notes)


def test_application_buttons(env, app_cfg, clock, kb):
    tailor.run(env, clock, app_cfg, kb, FakeRouter(GOOD_PLAN, [GOOD_LETTER]), RecordingNotifier())
    rowid = env.execute("SELECT rowid FROM applications").fetchone()[0]
    assert "одобрены" in cards.handle_app_action(env, clock, "ok", rowid)
    assert app_state(env)[1]["approved_cv"] is True
    assert "Переделаю" in cards.handle_app_action(env, clock, "redo", rowid)
    state, data = app_state(env)
    assert state == "draft" and "approved_cv" not in data
    assert "отменена" in cards.handle_app_action(env, clock, "cancel", rowid)
    assert app_state(env)[0] == "withdrawn"
    assert "уже withdrawn" in cards.handle_app_action(env, clock, "cancel", rowid)


def test_kb_check_cli(capsys):
    from jobpipe.cli import main

    assert main(["kb", "check", "--dir", str(KB_DIR)]) == 0
    assert "6 facts" in capsys.readouterr().out


@pytest.mark.parametrize("sentence,ok", [
    ("I have never doubted my Rust expertise.", False),          # S3 review: negation elsewhere
    ("I built low-latency systems in Rust, not Python.", False),
    ("I have not used Rust in production yet.", True),            # honest gap
    ("I have no production experience with Rust.", True),
    ("Я не работал с Rust, но быстро учусь.", True),
])
def test_gap_exemption_only_when_the_negation_is_about_that_technology(kb, sentence, ok):
    from jobpipe.services.truth import TruthGate

    rust = [c for c in TruthGate(kb).check(sentence).claims if c.kind == "tech" and c.text == "Rust"]
    assert rust and rust[0].ok is ok
