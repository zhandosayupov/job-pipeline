"""State machines: the single source of truth for allowed transitions (docs/architecture.md §6).

Pure data and checks, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping

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
        "ready": {"filling"},
        "filling": {"awaiting_approval", "apply_failed"},
        "awaiting_approval": {"submitting", "tailoring"},
        "submitting": {"applied", "apply_failed", "submit_unknown"},
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
