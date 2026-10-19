"""Risk kinds that a remembered approval rule may cover (alfred.safety.remembered). Commands and scripts name these
kinds (alfred.safety.commands, alfred.safety.scripts), and a remembered rule matches on them, so each kind is written
once, here. Being a StrEnum, each equals its words, which are what the approval_rules table keeps."""

from enum import StrEnum


class Risk(StrEnum):
    INSTALLS = "installs software"
    STOPS = "stops a process"
    PERMISSIONS = "changes permissions"
    IPHONE = "changes the iPhone"
    SIMULATOR = "changes a simulator"
