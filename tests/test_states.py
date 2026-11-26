import itertools

import pytest

from jobpipe.domain import states
from jobpipe.domain.errors import InvalidTransition


def reachable(table, start):
    seen, todo = {start}, [start]
    while todo:
        for nxt in table[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


@pytest.mark.parametrize("entity", ["job", "application", "conversation"])
def test_every_state_reachable_from_initial(entity):
    table, initial = states.MACHINES[entity]
    assert reachable(table, initial) == set(table)


def test_job_terminal_states():
    assert states.terminal_states(states.JOB) == {"dropped", "closed"}


def test_application_terminal_states():
    assert states.terminal_states(states.APPLICATION) == {"offer", "rejected", "withdrawn"}


def test_conversation_is_a_cycle_without_terminals():
    assert states.terminal_states(states.CONVERSATION) == set()


def test_closed_reachable_from_every_nonterminal_job_state():
    for s, targets in states.JOB.items():
        if targets:
            assert "closed" in targets, s


def test_withdrawn_reachable_from_every_open_application():
    for s, targets in states.APPLICATION.items():
        if targets:
            assert "withdrawn" in targets, s


def test_two_phase_submit_path():
    path = ["draft", "tailoring", "ready", "filling", "awaiting_approval", "submitting", "applied"]
    for a, b in itertools.pairwise(path):
        states.check_transition("application", a, b)


@pytest.mark.parametrize(
    "entity,src,dst",
    [
        ("job", "discovered", "shortlisted"),       # must be labeled first
        ("job", "dropped", "labeled"),              # terminal
        ("application", "draft", "submitting"),    # no submit without approval
        ("application", "ready", "submitting"),    # phase 1 is mandatory
        ("application", "submit_unknown", "submitting"),  # never auto-retry an unknown submit
        ("conversation", "draft_ready", "sent"),   # must be approved
    ],
)
def test_forbidden_transitions(entity, src, dst):
    with pytest.raises(InvalidTransition):
        states.check_transition(entity, src, dst)


def test_unknown_state():
    with pytest.raises(InvalidTransition):
        states.check_transition("job", "nope", "closed")
