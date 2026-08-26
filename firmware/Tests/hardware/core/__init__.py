"""
The hardware-test framework's machinery.

    model        statuses, safety classes, the definition of a test,
                 and the rule that PASS needs hardware evidence
    registry     the catalogue, its unique ids and its layer gates
    context      what a test body is handed, and the hardware gate
    runner       the seven gates, cleanup and abort handling
    evidence     one directory per run, stamped with what kind of run
    operator     asking a human, and recording that a human answered
    analysis     the numbers that turn "it moved" into a measurement
    defects      persistent HW-xxx identifiers

Importing any of this touches no hardware and opens no port.
"""

from .model import (Aborted, Automation, Blocked, Campaign, Check,
                    Evidence, Failure, Mode, Safety, Skip, Status,
                    TestDefinition, TestResult)
from .registry import REGISTRY, Registry, RegistryError, load

__all__ = [
    "Aborted", "Automation", "Blocked", "Campaign", "Check", "Evidence",
    "Failure", "Mode", "Safety", "Skip", "Status", "TestDefinition",
    "TestResult", "REGISTRY", "Registry", "RegistryError", "load",
]
