import pytest

from jobpipe.config.settings import FiltersCfg, load_config
from jobpipe.domain.job import Posting
from jobpipe.services import text as T
from jobpipe.services.geo import country_from_location, to_iso2
from jobpipe.services.prefilter import decide

from .conftest import REPO


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Unfortunately we cannot offer visa sponsorship for this role.", "no"),
        ("Visa sponsorship is not available.", "no"),
        ("You must already have the right to work in the UK.", "no"),
        ("Визовая поддержка не предоставляется.", "no"),
        ("We offer visa sponsorship and a relocation package.", "explicit"),
        ("Мы помогаем с визой и релокацией.", "explicit"),
        ("Relocation package to Amsterdam.", "relocation_only"),
        ("Great team, free lunch.", None),
        (
            "Candidates must be eligible to receive software or technology controlled under these U.S. export laws "
            "without sponsorship for an export license.",
            None,
        ),
    ],
)
def test_visa_signal_handles_negation(text, expected):
    assert T.visa_signal(text)[0] == expected


def test_visa_evidence_is_short():
    _, ev = T.visa_signal("Benefits. We offer visa sponsorship for all hires and a lot of other things too, really.")
    assert ev and len(ev.split()) <= 15


def test_remote_and_salary_and_lang():
    assert T.remote_signal("Fully remote, EU timezones") == "remote"
    assert T.remote_signal("Работа в офисе в Алматы") == "onsite"
    assert T.remote_signal("Amsterdam") is None
    assert T.salary_text("Salary: €90k - €120k per year") == "€90k - €120k"
    assert T.salary_text("No numbers here") is None
    assert T.detect_lang("Ищем разработчика C++") == "ru" and T.detect_lang("We hire") == "en"


def test_fingerprint_normalises():
    a = T.fingerprint("IMC Trading B.V.", "C++ Software Engineer", "Amsterdam, Netherlands")
    b = T.fingerprint("imc trading", "C++  software engineer", "Amsterdam")
    assert a == b == "imc trading|c++ software engineer|amsterdam"


def test_html_to_text_and_excerpt():
    html = "&lt;p&gt;Hello&lt;/p&gt;&lt;ul&gt;&lt;li&gt;C++&lt;/li&gt;&lt;/ul&gt;"
    assert T.html_to_text(html, unescape_first=True) == "Hello\n\n• C++"
    text = ("We build things. " * 30) + "We sponsor visas for this role. " + ("Fun office. " * 30)
    ex = T.excerpt(text, limit=200)
    assert "sponsor visas" in ex and len(ex) <= 200


def test_geo():
    assert to_iso2("NLD") == "NL" and to_iso2("nl") == "NL" and to_iso2("Germany") == "DE"
    assert to_iso2("Казахстан") == "KZ" and to_iso2("Atlantis") is None
    assert country_from_location("Berlin, Germany") == "DE" and country_from_location("Berlin") is None


@pytest.fixture
def filters():
    home = REPO / "config.example"
    import yaml

    return FiltersCfg.model_validate(yaml.safe_load((home / "filters.yaml").read_text(encoding="utf-8")))


def job(
    title="C++ Software Engineer",
    description="Low latency systems.",
    location="Amsterdam, Netherlands",
    country="NL",
    remote=None,
):
    return Posting(
        source="t",
        kind="ats",
        source_job_id="1",
        company="Acme",
        title=title,
        url="https://x",
        description=description,
        location=location,
        country=country,
        remote=remote,
    )


AUTH = ["KZ", "AM", "KG", "BY", "RU"]


@pytest.mark.parametrize(
    "posting,rule",
    [
        (job(), None),
        (job(title="Account Executive"), "title_not_eng"),
        (job(title="Software Engineering Intern"), "title_excluded"),
        (job(title="Staff Software Engineer"), "title_excluded"),
        (job(title="Member of Technical Staff, C++"), None),
        (job(title="Разработчик C++"), None),
        (job(title="Стажер-разработчик"), "title_excluded"),
        (job(description="Requires active security clearance."), "text_excluded"),
        (job(location="Moscow, Russia", country="RU"), "location_ru_by"),
        (job(location="Moscow, Russia", country="RU", remote="Remote"), None),
        (job(description="Visa sponsorship is not available."), "no_sponsorship"),
        (job(description="Visa sponsorship is not available.", remote="Remote"), None),
        (job(description="Visa sponsorship is not available.", location="Almaty", country="KZ"), None),
    ],
)
def test_prefilter_rules(filters, posting, rule):
    d = decide(posting, filters, AUTH)
    assert d.rule == rule and d.keep == (rule is None)
    if rule in ("title_not_eng", "title_excluded"):
        assert d.signals == {}                 # title drops skip the full-text signals (speed)
    else:
        assert "excerpt" in d.signals and "visa" in d.signals


def test_example_configs_load(tmp_path):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    for f in (REPO / "config.example").glob("*.yaml"):
        (home / "config" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(home, env={})
    assert cfg.sources.sources["greenhouse"].enabled
    assert any(c.token == "imc" for c in cfg.companies.companies)
    assert "c++" in cfg.filters.title_include


def test_salary_ignores_lone_small_amounts():
    assert T.salary_text("We raised $17.5M from investors") is None
    assert T.salary_text("Compensation $150,000 per year") == "$150,000"
    assert T.salary_text("Оклад 900 000 - 1 200 000 ₸") == "900 000 - 1 200 000 ₸"
