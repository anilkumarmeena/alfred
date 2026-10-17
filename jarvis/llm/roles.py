"""The jobs JARVIS gives a model. Code names a role; config/models.toml says which model plays it."""

from enum import StrEnum

from jarvis import config


class Role(StrEnum):
    VOICE = "voice"  # the spoken conversation
    VOICE_QUICK = "voice_quick"  # voice turns judged to need no tools ("" = none)
    LOOKUP = "lookup"  # read-only file lookups during a spoken turn
    SIDE = "side"  # background lookups handed off by the voice brain
    CONSENT = "consent"  # a spoken yes/no the word lists can't read
    INJECTION = "injection"  # screening tool results (its model is also a preference)
    TASK = "task"  # background tasks
    TASK_SEARCH = "task_search"  # a task's code-search subagent
    TASK_HELPER = "task_helper"  # a task's test-runner and web-research subagents
    GOAL = "goal"  # a finished task checked against its request
    ANNOUNCE = "announce"  # a task's spoken ending
    PLACEMENT = "placement"  # which machine runs a job
    SCHEDULE = "schedule"  # routine scheduled runs and watches
    VERDICT = "verdict"  # yes/no verdicts on a scheduled run
    COMMAND_SUMMARY = "command_summary"  # a command that worked, summed up
    COMMAND_TRIAGE = "command_triage"  # why a command failed, and what to do
    LEARNING = "learning"  # memory edits suggested after a conversation
    TODO_MATCH = "todo_match"  # whether a new to-do repeats one
    SKILL_REVIEW = "skill_review"  # an installed skill's scripts reviewed
    SIGN_IN_CHECK = "sign_in_check"  # a Claude login checked
    MOMENT = "moment"  # when to speak an update unasked
    ROUTE = "route"  # whether a voice turn can go to the quick model


def model(name: Role | str) -> str:
    """The model id for a role (its variable, else models.toml), a tier ("fast"), or a model id as it is."""
    return config.MODELS.resolve(str(name))
