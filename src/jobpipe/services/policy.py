"""Autonomy decisions (ADR 0016). Pure functions over config: the caller gathers the facts, this decides.

Applications are auto-submitted (L2) by default, but only when every safety check passes;
any failed check falls back to an approval card (L1) or stops. Messages to people default to L1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..config.settings import PolicyCfg


class Route(StrEnum):
    SUBMIT = "submit"          # L2: submit now, report afterwards
    ASK = "ask"                # L1: approval card with screenshot and manifest
    DRY_RUN = "dry_run"        # fill and report, never submit
    DEFER = "defer"            # daily cap reached: try again tomorrow
    BLOCK = "block"            # must not happen at all (truth gate failed, already applied)


@dataclass(frozen=True)
class ApplyFacts:
    platform: str
    protected: bool
    dry_run: bool
    paused: bool
    already_applied: bool          # idempotency key exists for (company, vacancy)
    truth_ok: bool                 # every claim in CV / letter / answers matched a fact
    unanswered_questions: int      # form fields with no answer in kb/answers.yaml
    similar_role_warning: bool     # another application to the same company, same track and level
    applied_today: int
    # Topics of the form's questions (e.g. {"compensation", "visa"}), and the subset answered with
    # a verbatim, owner-approved entry from kb/answers.yaml.
    topics_asked: frozenset[str] = frozenset()
    topics_preapproved: frozenset[str] = frozenset()
    job_country: str | None = None         # ISO 3166-1 alpha-2 of the job's work location; None = unknown
    posting_salary_known: bool = False     # the employer published a salary range in the posting


def needs_sponsorship(policy: PolicyCfg, country: str | None) -> bool | None:
    """Whether to ask for visa sponsorship for a job in `country`. None when the country is unknown
    (then the question goes to the owner: never guess)."""
    if not country:
        return None
    return country.upper() not in {c.upper() for c in policy.work_authorized_countries}


def _sensitive_blockers(policy: PolicyCfg, f: ApplyFacts) -> list[str]:
    """Sensitive form topics this application cannot answer on its own, with the reason."""
    out = []
    for topic in sorted(f.topics_asked & set(policy.sensitive_topics)):
        mode = policy.sensitive_answers.get(topic, policy.sensitive_answers.get("default", "preapproved"))
        if mode == "ask":
            out.append(f"{topic}: always asks you")
        elif mode == "work_authorization" and needs_sponsorship(policy, f.job_country) is None:
            out.append(f"{topic}: job country unknown")
        elif mode == "posting_range" and not f.posting_salary_known:
            out.append(f"{topic}: no salary range in the posting")
        elif mode == "preapproved" and topic not in f.topics_preapproved:
            out.append(f"{topic}: no pre-approved answer in kb/answers.yaml")
    return out


@dataclass(frozen=True)
class Decision:
    route: Route
    reasons: list[str] = field(default_factory=list)


def decide_apply(policy: PolicyCfg, f: ApplyFacts) -> Decision:
    if f.already_applied:
        return Decision(Route.BLOCK, ["already applied to this vacancy"])
    if not f.truth_ok:
        return Decision(Route.BLOCK, ["truth gate: a claim has no matching fact"])
    if f.paused:
        return Decision(Route.DEFER, ["kill switch is on"])
    cap = policy.daily_caps.get(f.platform)
    if cap is not None and f.applied_today >= cap:
        return Decision(Route.DEFER, [f"daily cap {cap} reached on {f.platform}"])
    if f.dry_run:
        return Decision(Route.DRY_RUN, [f"dry run is on for {f.platform}"])

    reasons = []
    if f.protected:
        reasons.append("protected company: never automatic")
    if f.unanswered_questions:
        reasons.append(f"{f.unanswered_questions} question(s) without an answer in the KB")
    blockers = _sensitive_blockers(policy, f)
    if blockers:
        reasons.append("sensitive question(s): " + "; ".join(blockers))
    if f.similar_role_warning:
        reasons.append("already applied to a similar role at this company")
    level = policy.autonomy.get("apply", "L1")
    if level != "L2":
        reasons.append(f"apply autonomy is {level}")
    if reasons:
        return Decision(Route.ASK, reasons)
    return Decision(Route.SUBMIT)


def decide_message(policy: PolicyCfg, action: str, topics: set[str]) -> Decision:
    """Outbound messages (replies, email drafts). Sensitive topics always need Approve."""
    sensitive = sorted(topics & set(policy.sensitive_topics))
    if sensitive:
        return Decision(Route.ASK, [f"sensitive: {', '.join(sensitive)}"])
    if policy.autonomy.get(action, "L1") == "L2":
        return Decision(Route.SUBMIT)
    return Decision(Route.ASK, [f"{action} autonomy is {policy.autonomy.get(action, 'L1')}"])
