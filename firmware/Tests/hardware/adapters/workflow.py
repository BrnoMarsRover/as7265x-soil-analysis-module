"""
The production operator application, as far as a test may drive it.

WHY THIS ADAPTER DOES NOT PRESS THE MENU KEYS FOR YOU

`firmware/PC/workflow/prompts.py` is the only module in the project that
calls `input()`, and it would be easy to monkeypatch it and let the
framework walk the menus. That was considered and rejected for two
reasons:

    1. It would produce a "the operator workflow passes" result that no
       operator ever saw. B10 exists to check the thing a human does at
       a keyboard during a competition run - a simulated keystroke
       sequence checks the screens, which `Tests/software` already does
       exhaustively, on fakes, with 40 suites.

    2. It would write to the Sample archive. `firmware/BD/samples/` is
       the one writable protected location and it is not in version
       control, so a framework that saves into it during a test can
       destroy the run's only irreplaceable output.

So B10 is OPERATOR_ASSISTED. The harness holds no link during it, tells
the operator exactly which client to start and which keys to press,
and records what they observed - including the measurement ids and slot
associations, which are checked afterwards through the read-only
`list_saved_samples` / `get_saved_sample` commands on the device's own
retained buffer.

WHAT IS CHECKED AUTOMATICALLY

That the production entry point and its screens exist and import - the
one part of B10 that can be verified without a human, and the part that
would silently rot if the application were refactored between
campaigns.
"""

import ast
from pathlib import Path

from .base import (Adapter, Capability, HARDWARE_DIR, PC_DIR,
                   firmware_commands, pc_command_surface)


CLIENT = PC_DIR / "rover_science_client.py"
WORKFLOW = PC_DIR / "workflow"

# The screens a mission needs, wherever in the workflow package they are
# defined - they are spread across measure.py, carousel.py, records.py
# and screen.py, and which file holds which is not the test's business.
# Named here so that a rename in the production application is caught by
# a readiness check rather than discovered at the bench with a rover on
# the field.
REQUIRED_SCREENS = (
    # the sample loop, [1] to [5] on the main screen
    "menu_choose_slot", "menu_prepare", "menu_confirm", "menu_measure",
    "menu_fine_adjust",

    # the navigation B10-004 checks is still reachable after a fault
    "menu_tools", "menu_help", "menu_sample_database", "menu_resync",

    # carousel setup, which is [0] on the startup screen
    "menu_connect_servo", "menu_initial_calibration",
)


class WorkflowAdapter(Adapter):
    """The shipped operator client: present, importable, and reachable."""

    name = "workflow"

    def __init__(self, context, link):
        super().__init__(context)

        self.link = link

    # ------------------------------------------------------------------

    def _detect(self):
        surface = pc_command_surface()
        commands = firmware_commands()

        found = {}

        found["workflow.client"] = Capability(
            "workflow.client", CLIENT.is_file(),
            reason="{} {}".format(
                CLIENT, "exists" if CLIENT.is_file() else "is MISSING"),
            recommendation="The operator client is the subject of B10; "
                           "without it there is no workflow to verify.",
            detail={"path": str(CLIENT)},
        )

        missing = self._missing_screens()

        found["workflow.screens"] = Capability(
            "workflow.screens", not missing,
            reason=("every required screen is defined in "
                    "workflow/screen.py" if not missing
                    else "workflow/screen.py no longer defines: {}".format(
                        ", ".join(missing))),
            recommendation="If a screen was renamed, update "
                           "REQUIRED_SCREENS here and re-read the B10 "
                           "procedure - the operator instructions name "
                           "menu entries by number and label.",
            detail={"missing": missing,
                    "required": list(REQUIRED_SCREENS)},
        )

        found["workflow.records"] = self.from_commands(
            "workflow.records",
            ["list_saved_samples", "get_saved_sample"],
            "add the retained-acquisition buffer commands to "
            "firmware/ESP32/protocol.py",
            surface, ["list_saved_samples", "get_saved_sample"],
        )

        found["workflow.drive_menus"] = Capability(
            "workflow.drive_menus", False,
            reason="the framework deliberately does not press keys in "
                   "the production client: a simulated operator would "
                   "produce a workflow result no operator observed, and "
                   "it would write into firmware/BD/samples/, which is "
                   "the run's only irreplaceable output",
            recommendation=(
                "Keep it that way. Tests/software already drives every "
                "screen against fakes - 40 suites, including "
                "integration/test_screens.py and test_mission.py. B10's "
                "job is the part those cannot do: a human, the real "
                "board, and the real archive."),
        )

        found["workflow.persistence_readonly"] = Capability(
            "workflow.persistence_readonly", bool(commands),
            reason="saved records are inspected through the device's "
                   "own retained buffer, which is read-only from the "
                   "test side; the PC archive is never written by this "
                   "framework",
        )

        return found

    # ------------------------------------------------------------------

    def _missing_screens(self):
        """
        Which required screens are gone, parsed rather than imported.

        PARSED, NOT IMPORTED, and that is not an optimization. Importing
        `workflow/screen.py` pulls in the whole science stack, which
        loads the protected reference libraries and constructs the
        Sample archive - work a readiness check has no business doing,
        on files this framework must not touch.

        The whole package is scanned because the screens live in four
        modules and which one holds which is the application's business,
        not the test's.
        """
        if not WORKFLOW.is_dir():                      # pragma: no cover
            return list(REQUIRED_SCREENS)

        defined = set()

        for source in sorted(WORKFLOW.glob("*.py")):
            try:
                tree = ast.parse(source.read_text(encoding="utf-8"))

            except (OSError, SyntaxError):             # pragma: no cover
                continue

            defined.update(
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef,
                                     ast.AsyncFunctionDef))
            )

        return [name for name in REQUIRED_SCREENS if name not in defined]

    # ------------------------------------------------------------------
    # read-only inspection of what a workflow run produced
    # ------------------------------------------------------------------

    def saved_samples(self):
        """The device's retained acquisitions. Reads, never writes."""
        return self.link.request("list_saved_samples", retries=1)

    def saved_sample(self, sample_id):
        return self.link.request("get_saved_sample", retries=1,
                                 sample_id=str(sample_id))

    def client_command(self, device):
        """The exact command the operator is told to run."""
        try:
            relative = CLIENT.relative_to(HARDWARE_DIR.parents[2])

        except ValueError:                             # pragma: no cover
            relative = CLIENT

        return "python3 {} --port {}".format(
            Path(relative).as_posix(), device)
