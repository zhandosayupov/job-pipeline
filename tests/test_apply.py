"""S4 end to end without a browser: real DB, real WorkerAPI logic, real Worker and Applier, fake Page.

FakeCore calls the WorkerAPI's sync methods directly (the HTTP layer is tested separately in
test_worker_api.py), so the full fill -> decide -> submit flow runs against the orchestrator.
"""
import json
import shutil
from pathlib import Path

import pytest

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.api.worker import WorkerAPI
from jobpipe.config.settings import PolicyCfg
from jobpipe.services import apply as ap
from jobpipe.services import control
from jobpipe.services import kb as kbmod
from jobpipe.services.events import create_entity, transition
from jobpipe.services.forms import FormField, resolve
from jobpipe.services.queue import Queue
from jobpipe.worker.agent import Worker, WorkerCfg
from jobpipe.worker.appliers import load_appliers

from .conftest import REPO

FORM = (REPO / "tests" / "fixtures" / "forms" / "greenhouse_like.html").read_text(encoding="utf-8")
SALARY_FIELD = ('<div><label for="q_sal">What are your salary expectations? *</label>'
                '<input type="text" id="q_sal" name="answers[9]" required></div>')
CONFIRM = "<html><body><h1>Thank you for applying!</h1></body></html>"
KB = kbmod.load(REPO / "config.example" / "kb")


class FakePage:
    def __init__(self, html, confirm_html=CONFIRM, captcha=False):
        self.page_html = html
        self.confirm_html = confirm_html
        self.captcha = captcha
        self.actions = []

    def goto(self, url):
        self.actions.append(("goto", url))

    def html(self):
        return ('<div class="g-recaptcha"></div>' if self.captcha else "") + self.page_html

    def url(self):
        return "https://x"

    def fill(self, key, value):
        self.actions.append(("fill", key, value))

    def choose(self, key, option, kind):
        self.actions.append(("choose", key, option))

    def upload(self, key, path):
        assert Path(path).exists()
        self.actions.append(("upload", key))

    def click(self, selector):
        self.actions.append(("click", selector))
        if "submit" in selector:
            self.page_html = self.confirm_html

    def exists(self, selector):
        return any(s.strip().lstrip("#") in self.page_html for s in selector.split(","))

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG fake")

    def wait(self, seconds):
        pass

    def close(self):
        self.actions.append(("close",))


class FakeCore:
    def __init__(self, api: WorkerAPI):
        self.api = api
        self.paused_before_click = False

    def heartbeat(self, info):
        out = self.api.heartbeat("laptop", info)
        if info.get("state") == "about to submit" and self.paused_before_click:
            return {"paused": True}
        return out

    def lease(self, wait=0):
        return self.api.lease_once("laptop")

    def resolve(self, app_id, fields):
        return self.api.resolve(app_id, [f.model_dump() for f in fields])

    def artifact(self, app_id, name, dest):
        shutil.copy(self.api.artifact_path(app_id, name), dest)
        return dest

    def result(self, task, kind, result, screenshot):
        shot = None
        if screenshot is not None and Path(screenshot).exists():
            shot = self.api.screenshot_dir(task["app_id"]) / f"{kind}.png"
            shutil.copy(screenshot, shot)
        return self.api.result({"task_id": task["task_id"], "kind": kind, "app_id": task["app_id"],
                                "result": result}, shot)


@pytest.fixture
def env(app_cfg, clock, tmp_path):
    conn = open_db(app_cfg.paths.db_path, clock)
    app_cfg.policy = PolicyCfg()
    create_entity(conn, clock, "job", "gh:acme:1", columns={
        "source": "greenhouse", "kind": "ats", "company": "Acme", "title": "C++ Engineer", "country": "NL",
        "url": "https://job-boards.greenhouse.io/acme/jobs/1"}, data={"lang": "en"})
    for s in ("prefiltered", "labeled", "shortlisted"):
        transition(conn, clock, "job", "gh:acme:1", s)
    art = tmp_path / "art"
    art.mkdir()
    (art / "cv.tex").write_text("\\documentclass{article}", encoding="utf-8")
    (art / "letter.txt").write_text("I would like to join Acme.", encoding="utf-8")
    create_entity(conn, clock, "application", "app:gh:acme:1",
                  columns={"job_id": "gh:acme:1", "idempotency_key": "apply:acme:gh:acme:1"},
                  data={"approved_cv": True, "artifacts": {"tex": str(art / "cv.tex"),
                                                           "letter": str(art / "letter.txt")}})
    for s in ("tailoring", "ready"):
        transition(conn, clock, "application", "app:gh:acme:1", s)
    notifier = RecordingNotifier()
    api = WorkerAPI(app_cfg, clock, notifier=notifier, kb_loader=lambda: KB)
    q = Queue(conn, clock, app_cfg.settings.queue)
    appliers = load_appliers(REPO / "config.example" / "appliers.yaml").platforms
    return conn, q, api, notifier, appliers


def state(conn):
    return conn.execute("SELECT state FROM applications").fetchone()[0]


def data(conn):
    return json.loads(conn.execute("SELECT data_json FROM applications").fetchone()[0])


def run_next(env, page, core=None):
    _conn, _q, api, _notifier, appliers = env
    core = core or FakeCore(api)
    worker = Worker(WorkerCfg(core_url="x", token="t", captcha_wait_s=20), appliers, core, lambda p: page,
                    sleep=lambda s: None)
    task = core.lease()
    assert task, "a task was expected"
    return worker.handle(task)


# --- forms ------------------------------------------------------------------------------------


def test_resolve_greenhouse_like_form():
    from jobpipe.worker.scan import scan_form

    res = resolve(scan_form(FORM), KB, PolicyCfg(), job_country="NL", cv_path="artifact:a:tex", letter="Hi")
    by_key = {a.key: a.value for a in res.answers}
    assert by_key["#first_name"] == "Alex" and by_key["#email"] == "alex@example.com"
    assert by_key["#q_auth"] == "No" and by_key["#q_spons"] == "Yes"         # NL needs sponsorship
    assert by_key["#q_notice"].startswith("Two weeks") and by_key['[name="relocation"]'] == "Yes"
    assert by_key["#resume"] == "artifact:a:tex" and by_key["#cover_letter_text"] == "Hi"
    assert not res.unanswered and "#q_hear" not in by_key                   # optional, unknown: left empty
    res_kz = resolve(scan_form(FORM), KB, PolicyCfg(), job_country="KZ")
    assert {a.key: a.value for a in res_kz.answers}["#q_spons"] == "No"
    res_unknown = resolve(scan_form(FORM), KB, PolicyCfg(), job_country=None)
    assert {f.key for f in res_unknown.unanswered} == {"#q_auth", "#q_spons", "#resume"}  # no CV given


def test_salary_question_is_unanswered_by_default():
    f = [FormField(key="#s", label="Salary expectations", required=True)]
    res = resolve(f, KB, PolicyCfg(), job_country="NL")
    assert res.unanswered and "compensation" in res.topics_asked and not res.topics_preapproved


# --- schedule -----------------------------------------------------------------------------------


def test_schedule_only_approved_and_supported(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    assert ap.schedule(conn, clock, app_cfg, q, notifier) == ["app:gh:acme:1"]
    assert state(conn) == "filling" and q.counts() == {"pending": 1}
    assert ap.platform_of("https://hh.kz/vacancy/1") == "hh" and ap.platform_of("https://acme.com/jobs") is None


def test_unsupported_platform_notified_once(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    conn.execute("UPDATE jobs SET url = 'https://careers.acme.com/1', source = 'hn'")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    ap.schedule(conn, clock, app_cfg, q, notifier)
    assert sum("Подай вручную" in m for m in notifier.messages) == 1 and state(conn) == "ready"


# --- full flows ---------------------------------------------------------------------------------


def test_dry_run_by_default_fills_but_never_clicks(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    ap.schedule(conn, clock, app_cfg, q, notifier)
    page = FakePage(FORM)
    out = run_next(env, page)
    assert out["outcome"] == "dry_run" and state(conn) == "awaiting_approval"
    assert not any(a[0] == "click" for a in page.actions)
    assert ("upload", "#resume") in page.actions and ("choose", "#q_spons", "Yes") in page.actions
    assert "DRY RUN" in notifier.messages[-1] and notifier.files[-1][0].endswith(".png")
    rowid = conn.execute("SELECT rowid FROM applications").fetchone()[0]
    assert "Понятно" in notifier.buttons[-1][0][0][0]
    assert "dry run" in ap.owner_decision(conn, clock, q, "dry", rowid) and state(conn) == "ready"


def test_auto_submit_when_every_check_passes(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    assert run_next(env, FakePage(FORM))["outcome"] == "submit"
    assert state(conn) == "submitting"
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") == "planned"
    page = FakePage(FORM)
    assert run_next(env, page)["outcome"] == "applied"
    assert ("click", "#submit_app, button[type=submit]") in page.actions
    assert state(conn) == "applied" and "✅ Отправлено" in notifier.messages[-1]
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") == "done"


def test_salary_question_asks_owner_and_waits_for_an_answer(env, app_cfg, clock):
    """Approve with an unanswered required question: the worker re-reads the form and refuses to click;
    once the owner adds the answer to the KB (and the policy allows it), the next Approve submits."""
    conn, q, api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    form = FORM.replace("</form>", SALARY_FIELD + "</form>")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    assert run_next(env, FakePage(form))["outcome"] == "ask"
    assert state(conn) == "awaiting_approval" and "salary" in notifier.messages[-1].lower()
    rowid = conn.execute("SELECT rowid FROM applications").fetchone()[0]
    assert "без ответа" in ap.owner_decision(conn, clock, q, "ok", rowid)
    page = FakePage(form)
    assert run_next(env, page)["outcome"] == "changed"
    assert not any(a[0] == "click" for a in page.actions) and state(conn) == "awaiting_approval"
    # the owner adds a salary answer and lets the policy use it
    kb2 = KB.model_copy(update={"answers": [*KB.answers, kbmod.Answer(topic="compensation",
                                                                     question_patterns=["salary"],
                                                                     answer="Open to discussion")]})
    api.kb_loader = lambda: kb2
    app_cfg.policy.sensitive_answers["compensation"] = "preapproved"
    # the card text still lists the old unanswered question (it is re-read only by the worker)
    assert "очеред" in ap.owner_decision(conn, clock, q, "ok", rowid)
    # the answer changes what would be sent, so the owner sees the final form once more
    assert run_next(env, FakePage(form))["outcome"] == "changed"
    assert "Open to discussion" in notifier.messages[-1]
    assert "очередь" in ap.owner_decision(conn, clock, q, "ok", rowid)
    assert run_next(env, FakePage(form))["outcome"] == "applied"


def test_changed_form_goes_back_to_owner_and_frees_the_key(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))
    page = FakePage(FORM.replace("</form>", SALARY_FIELD + "</form>"))
    assert run_next(env, page)["outcome"] == "changed"
    assert not any(a[0] == "click" for a in page.actions)
    assert state(conn) == "awaiting_approval" and "Форма изменилась" in notifier.messages[-1]
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") is None


def test_pause_right_before_click(env, app_cfg, clock):
    conn, q, api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))
    core = FakeCore(api)
    core.paused_before_click = True
    page = FakePage(FORM)
    assert run_next(env, page, core)["outcome"] == "failed"
    assert not any(a[0] == "click" for a in page.actions) and state(conn) == "apply_failed"


def test_no_confirmation_means_unknown(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))
    assert run_next(env, FakePage(FORM, confirm_html="<html>Something went wrong</html>"))["outcome"] == "unknown"
    assert state(conn) == "submit_unknown"
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") == "unknown"
    assert "effects resolve" in notifier.messages[-1]


def test_captcha_waits_then_fails_without_bypass(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    ap.schedule(conn, clock, app_cfg, q, notifier)
    page = FakePage(FORM, captcha=True)
    assert run_next(env, page)["outcome"] == "failed"
    assert state(conn) == "apply_failed"
    assert any("капчу" in m for m in notifier.messages) and any("Капча" in m for m in notifier.messages)
    assert not any(a[0] in ("fill", "click") for a in page.actions)


def test_already_applied_blocks(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    se = control.SideEffects(conn, clock)
    se.plan("apply:acme:gh:acme:1", "submit")
    se.mark("apply:acme:gh:acme:1", "done")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    assert run_next(env, FakePage(FORM))["outcome"] == "block"
    assert state(conn) == "withdrawn"


def test_daily_cap_defers(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    app_cfg.policy.daily_caps["greenhouse"] = 0
    ap.schedule(conn, clock, app_cfg, q, notifier)
    assert run_next(env, FakePage(FORM))["outcome"] == "defer"
    assert state(conn) == "ready" and data(conn)["deferred_until"]
    assert ap.schedule(conn, clock, app_cfg, q, notifier) == []   # deferred: not picked again yet


# --- S4 review fixes ----------------------------------------------------------------------


def test_dry_run_report_is_sent_once_until_dry_run_is_off(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))
    rowid = conn.execute("SELECT rowid FROM applications").fetchone()[0]
    ap.owner_decision(conn, clock, q, "dry", rowid)
    assert ap.schedule(conn, clock, app_cfg, q, notifier) == []          # no endless dry-run cards
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    assert ap.schedule(conn, clock, app_cfg, q, notifier) == ["app:gh:acme:1"]


def test_reclaimed_submit_is_never_handed_out_again(env, app_cfg, clock):
    conn, q, api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))                                        # fill -> submit queued
    task = api.lease_once("laptop")                                      # worker takes the submit...
    assert task["type"] == ap.SUBMIT
    clock.advance(app_cfg.settings.queue.lease_seconds + 5)              # ...and dies after the click
    assert api.lease_once("laptop") is None                              # not handed out again
    assert state(conn) == "submit_unknown"
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") == "unknown"
    assert any("Воркер пропал" in m for m in notifier.messages)


class Boom(Exception):
    pass


def test_worker_crash_before_click_is_reported_not_left_hanging(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))

    class Slow(FakePage):
        def fill(self, key, value):
            raise Boom("Timeout 30000ms exceeded")

    assert run_next(env, Slow(FORM))["outcome"] == "failed"
    assert state(conn) == "apply_failed"
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") is None   # nothing went out


def test_worker_crash_during_click_is_unknown(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))

    class DiesOnClick(FakePage):
        def click(self, selector):
            raise Boom("Target closed")

    assert run_next(env, DiesOnClick(FORM))["outcome"] == "unknown"
    assert state(conn) == "submit_unknown"


def test_pause_defers_without_a_12h_wait(env, app_cfg, clock):
    conn, q, _api, notifier, _ = env
    ap.schedule(conn, clock, app_cfg, q, notifier)
    control.set_flag(conn, clock, control.PAUSED, "1")
    ap.on_fill_result(conn, clock, app_cfg, q, notifier, "app:gh:acme:1", ap.FillResult(ok=True, manifest_hash="h"))
    assert state(conn) == "ready" and "deferred_until" not in data(conn)



def test_apply_error_after_the_click_is_unknown_not_a_retry(env, app_cfg, clock):
    from jobpipe.worker.appliers import ApplyError

    conn, q, _api, notifier, _ = env
    control.set_flag(conn, clock, "dry_run.greenhouse", "0")
    ap.schedule(conn, clock, app_cfg, q, notifier)
    run_next(env, FakePage(FORM))

    class PostClickCheck(FakePage):
        def wait(self, s):
            if any(a[0] == "click" for a in self.actions):     # a check that runs after the click
                raise ApplyError("confirmation check failed")

    assert run_next(env, PostClickCheck(FORM))["outcome"] == "unknown"
    assert control.SideEffects(conn, clock).status("apply:acme:gh:acme:1") == "unknown"


def test_submit_lost_never_overwrites_a_known_outcome(env, app_cfg, clock):
    conn, _q, _api, _notifier, _ = env
    se = control.SideEffects(conn, clock)
    se.plan("apply:acme:gh:acme:1", "submit")
    se.mark("apply:acme:gh:acme:1", "done")
    ap.submit_lost(conn, clock, None, "app:gh:acme:1")
    assert se.status("apply:acme:gh:acme:1") == "done"


@pytest.mark.parametrize("url,platform", [
    ("https://job-boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/acme/1", "lever"),
    ("https://jobs.ashbyhq.com/acme/1", "ashby"),
    ("https://almaty.hh.kz/vacancy/1", "hh"),
    ("https://greenhouse.io.evil.example/x", None),     # CodeQL: substring match is not a domain match
    ("https://fakeashbyhq.com/x", None),
    ("https://notjobs.lever.co.example/x", None),
    ("https://evilhh.kz/vacancy/1", None),
])
def test_platform_of_matches_whole_domains_only(url, platform):
    assert ap.platform_of(url) == platform
