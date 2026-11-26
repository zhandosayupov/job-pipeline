"""State machines: the single source of truth for allowed transitions (docs/architecture.md §6).

Pure data and checks, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .errors import InvalidTransition

Table = Mapping[str, frozenset[str]]


def _table(edges: dict[str, set[str]], *, also_from_nonterminal: set[str] = frozenset()) -> Table:
    """Build a transition table. `also_from_nonterminal` targets are reachable from every
    state that has outgoing edges (e.g. `closed`, `withdrawn`)."""
    states = set(edges) | {t for ts in edges.values() for t in ts} | set(also_from_nonterminal)
    out: dict[str, set[str]] = {s: set(edges.get(s, set())) for s in states}
    for s, targets in list(out.items()):
        if targets and s not in also_from_nonterminal:
            targets |= set(also_from_nonterminal)
    return {s: frozenset(t) for s, t in out.items()}


JOB = _table(
    {
        "discovered": {"prefiltered", "dropped"},
        "prefiltered": {"labeled", "dropped"},
        "labeled": {"shortlisted", "skipped"},
        "shortlisted": {"skipped"},
        "skipped": {"closed"},
    },
    also_from_nonterminal={"closed"},
)
JOB_INITIAL = "discovered"

APPLICATION = _table(
    {
        "draft": {"tailoring"},
        "tailoring": {"ready", "draft"},
        "ready": {"filling", "tailoring"},  # tailoring: the owner asked to redo the CV/letter
        # filling -> submitting is auto-apply (L2, ADR 0016); -> awaiting_approval is the L1 path.
        "filling": {"submitting", "awaiting_approval", "apply_failed", "ready"},  # ready: deferred (cap, pause)
        "awaiting_approval": {"submitting", "tailoring", "ready"},  # ready: dry-run report acknowledged
        # awaiting_approval: the form changed between fill and submit (manifest hash differs)
        "submitting": {"applied", "apply_failed", "submit_unknown", "awaiting_approval"},
        "submit_unknown": {"applied", "apply_failed"},
        "apply_failed": {"filling"},
        "applied": {"replied", "rejected", "ghosted"},
        "ghosted": {"replied"},
        "replied": {"screening", "interview", "rejected"},
        "screening": {"interview", "rejected", "ghosted"},
        "interview": {"offer", "rejected", "ghosted"},
    },
    also_from_nonterminal={"withdrawn"},
)
APPLICATION_INITIAL = "draft"

CONVERSATION = _table(
    {
        "new": {"triaged"},
        "triaged": {"draft_ready", "waiting"},
        "draft_ready": {"triaged", "approved"},
        "approved": {"sent"},
        "sent": {"waiting"},
        "waiting": {"new"},
    }
)
CONVERSATION_INITIAL = "new"

MACHINES: dict[str, tuple[Table, str]] = {
    "job": (JOB, JOB_INITIAL),
    "application": (APPLICATION, APPLICATION_INITIAL),
    "conversation": (CONVERSATION, CONVERSATION_INITIAL),
}


# --- attention tags (ADR 0017) ---------------------------------------------------------
#
# Every state carries one tag, so the bot and the Mini App can filter "what needs me":
#   ok           moving along, nothing to do
#   needs_human  waiting for the owner: approve, answer, decide, show up
#   not_ok       ended badly or was filtered out; the system already handled it, nothing to do


class Attention(StrEnum):
    OK = "ok"
    NEEDS_HUMAN = "needs_human"
    NOT_OK = "not_ok"


_A = Attention
ATTENTION: dict[str, dict[str, Attention]] = {
    "job": {
        "discovered": _A.OK, "prefiltered": _A.OK, "labeled": _A.OK, "shortlisted": _A.OK,
        "dropped": _A.NOT_OK, "skipped": _A.NOT_OK, "closed": _A.NOT_OK,
    },
    "application": {
        "draft": _A.OK, "tailoring": _A.OK, "ready": _A.OK, "filling": _A.OK, "submitting": _A.OK,
        "applied": _A.OK,
        "awaiting_approval": _A.NEEDS_HUMAN,  # L1 card waiting for Approve
        "apply_failed": _A.NEEDS_HUMAN,       # form changed / selector broke: look at the screenshot
        "submit_unknown": _A.NEEDS_HUMAN,     # did it go through? only the owner can tell
        "replied": _A.NEEDS_HUMAN, "screening": _A.NEEDS_HUMAN, "interview": _A.NEEDS_HUMAN,
        "offer": _A.NEEDS_HUMAN,
        "rejected": _A.NOT_OK, "ghosted": _A.NOT_OK, "withdrawn": _A.NOT_OK,
    },
    "conversation": {
        "new": _A.OK, "triaged": _A.OK, "approved": _A.OK, "sent": _A.OK, "waiting": _A.OK,
        "draft_ready": _A.NEEDS_HUMAN,
    },
    # Not state machines, but tagged the same way so one filter covers everything.
    "task": {"pending": _A.OK, "running": _A.OK, "done": _A.OK, "dead": _A.NEEDS_HUMAN},
    "side_effect": {"planned": _A.OK, "done": _A.OK, "failed": _A.NOT_OK, "unknown": _A.NEEDS_HUMAN},
}


def attention(entity: str, state: str, *, protected: bool = False) -> Attention:
    """Tag of a state. A protected job on the shortlist needs the owner: it is never auto-applied."""
    if entity == "job" and protected and state == "shortlisted":
        return Attention.NEEDS_HUMAN
    return ATTENTION[entity][state]


def states_tagged(entity: str, tag: Attention) -> frozenset[str]:
    """For SQL filters: `WHERE state IN (...)`."""
    return frozenset(s for s, t in ATTENTION[entity].items() if t is tag)


def can_transition(table: Table, src: str, dst: str) -> bool:
    return dst in table.get(src, frozenset())


def check_transition(entity: str, src: str, dst: str) -> None:
    table, _ = MACHINES[entity]
    if src not in table:
        raise InvalidTransition(f"{entity}: unknown state {src!r}")
    if not can_transition(table, src, dst):
        raise InvalidTransition(f"{entity}: {src} -> {dst} is not allowed")


def terminal_states(table: Table) -> set[str]:
    return {s for s, t in table.items() if not t}
