"""S5 triage rules, the LLM output check, and reply drafts (fixed answers, placeholders, truth gate)."""
import pytest

from jobpipe.adapters.inbox.mail import parse_email
from jobpipe.config.settings import PolicyCfg
from jobpipe.domain.message import InMessage
from jobpipe.services import kb as kbmod
from jobpipe.services import reply as rp
from jobpipe.services import triage as tr
from jobpipe.services.llm_router import LLMUnavailable

from .conftest import REPO

MAIL = REPO / "tests" / "fixtures" / "mail"
KB = kbmod.load(REPO / "config.example" / "kb")
ALERTS = ["hh.kz", "hh.ru"]
ATS = ["greenhouse-mail.io", "greenhouse.io", "lever.co"]


def mail(name):
    return parse_email((MAIL / name).read_bytes())


def msg(text, subject="", addr="x@corp.example", name="", auto=False):
    return InMessage(channel="gmail", ext_id=f"<{hash(text)}@t>", ts="2026-11-26T00:00:00Z", from_addr=addr,
                     from_name=name, subject=subject, text=text, auto=auto)


@pytest.mark.parametrize("fixture,category", [
    ("ack_greenhouse.eml", "ack"), ("rejection.eml", "rejection"), ("invite_ru.eml", "invite"),
    ("hh_alert.eml", "job_alert"),
])
def test_rules_on_fixtures(fixture, category):
    t = tr.by_rules(mail(fixture), alert_senders=ALERTS)
    assert t is not None and t.category == category


def test_screening_mail_is_left_to_the_llm_and_invites_carry_the_scheduling_topic():
    assert tr.by_rules(mail("screening.eml"), alert_senders=ALERTS) is None
    inv = tr.by_rules(mail("invite_ru.eml"), alert_senders=ALERTS)
    assert "interview_scheduling" in inv.topics and inv.questions == ["Когда вам удобно созвониться на этой неделе?"]


def test_rejection_wins_over_thanks_and_ack_needs_no_questions():
    t = tr.by_rules(msg("Thank you for applying. Unfortunately we will not be moving forward."), alert_senders=[])
    assert t.category == "rejection"
    t = tr.by_rules(msg("Thank you for applying! What is your notice period?"), alert_senders=[])
    assert t is None                                   # a question inside: not a plain ack


def test_job_related_filter_keeps_the_mailbox_private():
    kw = dict(ats_domains=ATS, alert_senders=ALERTS, linked=False, ignore=["news.example"])
    assert not tr.is_job_related(mail("personal.eml"), **kw)
    assert tr.is_job_related(mail("ack_greenhouse.eml"), **kw)       # hiring-platform sender
    assert tr.is_job_related(mail("hh_alert.eml"), **kw)
    assert tr.is_job_related(mail("personal.eml"), **{**kw, "linked": True})
    assert tr.is_job_related(msg("We are hiring a C++ engineer, interested?"), **kw)
    assert not tr.is_job_related(msg("Top 10 job tips", auto=True), **kw)            # newsletter
    assert not tr.is_job_related(msg("hiring now", addr="a@news.example"), **kw)    # ignored sender


def test_llm_questions_must_be_verbatim_and_topics_are_unioned():
    m = mail("screening.eml")
    out = tr.TriageOut(category="screening_question",
                       questions=["What is your notice period?", "Are you a Rust expert?"], topics=["visa"])
    t = tr.check_llm(out, m)
    assert t.questions == ["What is your notice period?"]                # invented question dropped
    assert {"compensation", "notice_period", "visa"} <= t.topics          # regex found what the LLM missed


def test_screening_without_real_questions_becomes_info_request():
    m = msg("Please send us your CV and portfolio.")
    t = tr.check_llm(tr.TriageOut(category="screening_question", questions=["Why?"]), m)
    assert t.category == "info_request" and t.questions == []


def test_fallback_without_llm():
    assert tr.fallback(mail("screening.eml")).category == "screening_question"
    hard_wrapped = tr.fallback(msg("Could you tell us your\nnotice period?"))
    assert hard_wrapped.questions == ["Could you tell us your notice period?"]
    assert tr.fallback(msg("Please send us your CV.")).category == "info_request"
    assert tr.fallback(msg("FYI the office moves.")).category == "other"


# --- replies --------------------------------------------------------------------------


QS = ["What are your salary expectations?", "What is your notice period?",
      "Will you now or in the future require visa sponsorship?"]


def test_answers_for_sensitive_topics_never_generated():
    pol = PolicyCfg()
    fixed, holes, topics = rp.answers_for(QS, KB, pol, job_country="DE", lang="en")
    answers = dict(fixed)
    assert answers["What is your notice period?"] == "Two weeks after signing the offer."   # verbatim answers.yaml
    assert answers[QS[2]] == "I would need visa sponsorship to work in this location."      # DE: not authorized
    assert QS[0] not in answers and holes == ["[[ВПИШИ: зарплатные ожидания]]"]
    assert {"compensation", "notice_period", "visa"} <= topics
    fixed_kz, _, _ = rp.answers_for(QS[2:], KB, pol, job_country="KZ", lang="ru")
    assert fixed_kz[0][1] == "Виза или спонсорство для работы в этой стране мне не нужны."
    _, holes_unknown, _ = rp.answers_for(QS[2:], KB, pol, job_country=None, lang="en")
    assert holes_unknown == ["[[ВПИШИ: виза/разрешение на работу]]"]                      # unknown country


class Router:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, task, variables, schema=None, personal=False):
        assert task == "reply" and personal
        self.calls.append(variables)
        if not self.replies:
            raise LLMUnavailable("reply", [])
        return type("C", (), {"data": rp.ReplyOut(reply=self.replies.pop(0))})()


def build(router, **kw):
    m = mail("screening.eml")
    args = dict(message=m.text, subject=m.subject, to_name="Jane Recruiter", category="screening_question",
                questions=QS, topics=set(), company="Acme", job_title="C++ Engineer", job_country="DE")
    args.update(kw)
    return rp.build(KB, PolicyCfg(), router, **args)


def test_llm_draft_keeps_fixed_answers_and_placeholders_even_if_the_model_drops_them():
    r = Router(["Hello Jane,\n\nThank you for reaching out about the C++ Engineer role at Acme.\n\n"
                "Best regards,\nAlex Example"])
    d = build(r)
    assert d.generated and d.ok
    assert "Two weeks after signing the offer." in d.text and "[[ВПИШИ: зарплатные ожидания]]" in d.text
    assert d.text.index("Two weeks") < d.text.index("Best regards")        # inserted above the signature
    assert '"[[ВПИШИ: зарплатные ожидания]]"' in r.calls[0]["placeholders"]


def test_invented_claim_fails_the_truth_gate_after_one_retry():
    bad = "Hello Jane,\n\nI have 9 years of Rust at Google.\n\nBest regards,\nAlex Example"
    r = Router([bad, bad])
    d = build(r)
    assert not d.ok and len(r.calls) == 2 and "DO NOT mention" in r.calls[1]["facts"]
    good = Router([bad, "Hello Jane,\n\nThank you for the questions.\n\nBest regards,\nAlex Example"])
    assert build(good).ok


def test_template_when_no_llm_and_russian_invite():
    d = build(None)
    assert not d.generated and d.ok and d.text.startswith("Hello Jane,")
    inv = mail("invite_ru.eml")
    d = rp.build(KB, PolicyCfg(), Router([]), message=inv.text, subject=inv.subject, to_name="Мария",
                 category="invite", questions=["Когда вам удобно созвониться на этой неделе?"], topics=set(),
                 company="Gamma", job_title="Backend Developer", job_country="KZ")
    assert d.lang == "ru" and d.text.startswith("Здравствуйте, Мария!")
    assert d.placeholders == ["[[ВПИШИ: удобное время для звонка]]"] and "С уважением,\nAlex Example" in d.text
    assert any("LLM недоступен" in n for n in d.notes)


def test_first_name_skips_team_mailboxes():
    assert rp._first_name("Jane Recruiter") == "Jane"
    assert rp._first_name("Acme Talent Team") == "" and rp._first_name("no-reply") == ""
