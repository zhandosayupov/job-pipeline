class JobpipeError(Exception):
    """Base class for domain errors."""


class InvalidTransition(JobpipeError):
    """A state change not allowed by the state machine."""


class StaleState(JobpipeError):
    """The row was not in the expected state (someone else moved it first)."""


class Paused(JobpipeError):
    """The kill switch is on; no side effects may start."""


class Permanent(JobpipeError):
    """A task failure that retrying will not fix: dead-letter immediately."""
