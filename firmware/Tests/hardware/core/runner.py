"""
Executing a selection of tests, and refusing to execute most of them.

THE ORDER OF THE GATES, WHICH IS THE ORDER OF THEIR CONSEQUENCES

    1. mode          nothing physical happens outside EXECUTE
    2. profile       a bench that cannot be described cannot be tested
    3. hardware      the operator confirms, once, for the whole run
    4. prerequisite  a layer whose foundation has not passed is not
                     evidence; the expert override is here
    5. safety        motion, illumination, disconnect, reset, power
                     cycle, fault injection, endurance and full-system
                     each ask their own question
    6. operator      a test needing a human is BLOCKED without one
    7. capability    a missing production interface is BLOCKED, named,
                     with the change that would unblock it

Only after all seven does a test body run.

CLEANUP IS NOT OPTIONAL AND IS NOT ALLOWED TO LIE

Every test with a cleanup handler gets it run - after a pass, after a
failure, after an exception and after Ctrl+C. If cleanup itself fails,
the failure is recorded in the result and the test is NOT reported as
having been cleaned up. "Illumination off" after the link has already
died is a claim nobody can make, and the honest record is
`cleanup: {"confirmed": false}` with the reason.

ABORT

Ctrl+C anywhere - inside a test body, inside a prompt, between tests -
produces an ABORTED result for the test that was running, runs its
cleanup, writes the summary, and stops. Partial evidence is the only
evidence an interrupted endurance run can leave, and it is kept.
"""

import json
import traceback
from pathlib import Path

from .model import (Aborted, Blocked, Evidence, EVIDENCE_FOR_MODE, Failure,
                    Mode, Safety, Skip, Status, TestResult, now)
from .context import HardwareNotPermitted


LEDGER_NAME = "ledger.json"


class Ledger:
    """
    What each test's last REAL hardware result was.

    Only EXECUTE runs write to it, which is what makes it usable as a
    gate: a dry run cannot open a layer gate, and neither can a
    framework self-test.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}

        if self.path.is_file():
            try:
                self.data = json.loads(
                    self.path.read_text(encoding="utf-8"))

            except (OSError, ValueError):
                self.data = {}

    def status_of(self, test_id):
        entry = self.data.get(test_id) or {}

        return entry.get("status", Status.NOT_RUN)

    def record(self, result, run_id):
        if not result.hardware_evidence:
            return

        self.data[result.test_id] = {
            "status": result.status,
            "run_id": run_id,
            "reason": result.reason,
        }

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, indent=2, sort_keys=True),
                encoding="utf-8")

        except OSError:                                # pragma: no cover
            pass


class Runner:
    """Runs a selection through every gate, in order, and records it."""

    def __init__(self, registry, context, ledger=None,
                 expert_override=False, override_reason="",
                 stop_on_failure=False):
        self.registry = registry
        self.context = context
        self.ledger = ledger

        self.expert_override = bool(expert_override)
        self.override_reason = str(override_reason or "")

        self.stop_on_failure = bool(stop_on_failure)

        self.results = []
        self.aborted = False

        self._safety_confirmed = set()

    # ------------------------------------------------------------------

    @property
    def evidence_class(self):
        return EVIDENCE_FOR_MODE[self.context.mode]

    def run(self, definitions):
        """Every definition, in order, until finished or aborted."""
        for definition in definitions:
            if self.aborted:
                self.results.append(self._unrun(
                    definition, Status.NOT_RUN,
                    "the run was aborted before this test started"))

                continue

            try:
                result = self.run_one(definition)

            except KeyboardInterrupt:
                self.aborted = True

                result = self._make(definition, Status.ABORTED,
                                    "interrupted between tests")

                self.results.append(result)

                continue

            self.results.append(result)

            if (self.stop_on_failure
                    and result.status in Status.BAD):
                self.context.event(
                    "stopping", reason="stop-on-failure",
                    test_id=definition.test_id, status=result.status)

                break

        return self.results

    # ------------------------------------------------------------------

    def run_one(self, definition):
        """One test, through the gates, with cleanup guaranteed."""
        context = self.context

        context.definition = definition

        result = self._make(definition, Status.NOT_RUN, "")
        context.result = result

        self._announce(definition)

        context.event("test_start", test_id=definition.test_id,
                      campaign=definition.campaign,
                      safety=definition.safety,
                      automation=definition.automation,
                      mode=context.mode)

        result.started_at = now()

        try:
            self._gate(definition, result)

            if result.status != Status.NOT_RUN:
                return self._finish(result)

            if context.mode != Mode.EXECUTE and (
                    context.mode != Mode.SELFTEST):
                result.status = Status.READY_FOR_HARDWARE
                result.reason = (
                    "every gate this test can be checked against "
                    "offline is satisfied; it will run when hardware is "
                    "connected and confirmed")

                return self._finish(result)

            definition.run(context)

        except Blocked as blocked:
            result.status = Status.BLOCKED
            result.reason = blocked.reason

            result.notes.append("recommendation: {}".format(
                blocked.recommendation or "none recorded"))

            context.event("blocked", test_id=definition.test_id,
                          reason=blocked.reason,
                          capability=blocked.capability,
                          recommendation=blocked.recommendation)

        except Skip as skip:
            result.status = Status.SKIPPED
            result.reason = skip.reason

        except Failure as failure:
            result.status = Status.FAIL
            result.reason = failure.reason

            if failure.evidence:
                result.notes.append(json.dumps(
                    failure.evidence, default=str, sort_keys=True))

        except (Aborted, KeyboardInterrupt) as stop:
            self.aborted = True

            result.status = Status.ABORTED
            result.reason = getattr(stop, "reason", "interrupted")

        except HardwareNotPermitted as bug:
            # A framework bug, never a hardware verdict. ERROR, and the
            # message says which operation tried to escape the gate.
            result.status = Status.ERROR
            result.reason = "framework gate violation: {}".format(bug)
            result.error_type = type(bug).__name__
            result.error_message = str(bug)

        except Exception as error:
            result.status = Status.ERROR
            result.reason = "{}: {}".format(type(error).__name__, error)
            result.error_type = type(error).__name__
            result.error_message = str(error)

            context.event("exception", test_id=definition.test_id,
                          error_type=type(error).__name__,
                          error=str(error),
                          traceback=traceback.format_exc())

        else:
            result.status = self._verdict(result)

        finally:
            self._cleanup(definition, result)

        return self._finish(result)

    # ------------------------------------------------------------------
    # the verdict
    # ------------------------------------------------------------------

    def _verdict(self, result):
        """
        PASS needs three things, and the third is the one people forget.

        The body must have completed, every check must have passed, and
        THERE MUST HAVE BEEN AT LEAST ONE CHECK. A test that ran, made
        no observation and returned is not a pass - it is a test that
        forgot to look.
        """
        if not result.checks:
            result.reason = (
                "the procedure completed without making a single check. "
                "That is not a pass; it is a test that did not look.")

            return Status.ERROR

        failed = result.failed_checks()

        if failed:
            result.reason = "{} of {} checks failed: {}".format(
                len(failed), len(result.checks),
                "; ".join(c.description for c in failed[:3]))

            return Status.FAIL

        if not result.hardware_evidence:
            # Reachable only in SELFTEST. The status is computed for the
            # framework's own assertions; `reported_status` renders it
            # as SELFTEST_PASS and `TestResult` refuses to hold PASS
            # with a non-hardware evidence class, so this is the branch
            # that keeps that invariant true.
            result.reason = "{} checks passed against a fake transport; " \
                            "this says nothing about the hardware".format(
                                len(result.checks))

            return Status.SKIPPED

        result.reason = "{} checks passed".format(len(result.checks))

        return Status.PASS

    # ------------------------------------------------------------------
    # gates
    # ------------------------------------------------------------------

    def _gate(self, definition, result):
        """Set result.status if the test must not run. Otherwise leave it."""
        context = self.context

        # 2. the profile
        if context.mode == Mode.EXECUTE and not context.profile.valid:
            result.status = Status.BLOCKED
            result.reason = (
                "the bench profile is not usable: {}".format(
                    "; ".join(context.profile.problems)))

            return

        # 4. prerequisites
        gate = self._prerequisite_gate(definition)

        if gate is not None:
            if not self._allow_override(definition, gate, result):
                result.status = Status.BLOCKED
                result.reason = gate

                return

        # 6. an operator, if the test needs one
        if definition.needs_operator and not context.operator.interactive:
            result.status = Status.BLOCKED
            result.reason = (
                "this test needs a human observation and the run is "
                "--non-interactive. Nothing here may be assumed.")

            return

        # 7. capabilities
        missing = [name for name in definition.requires
                   if not context.has(name)]

        if missing:
            first = context.capability(missing[0])

            result.status = Status.BLOCKED
            result.reason = "{} is not available: {}".format(
                missing[0], first.reason if first else "unknown capability")

            result.notes.append("recommendation: {}".format(
                first.recommendation if first else "none recorded"))

            if len(missing) > 1:
                result.notes.append("also missing: {}".format(
                    ", ".join(missing[1:])))

            return

        # 5. safety - asked last, so the operator is not asked to
        # confirm a movement for a test that was going to be blocked.
        if context.mode == Mode.EXECUTE and (
                definition.needs_extra_confirmation):
            if not self._safety_confirmation(definition, result):
                return

    def _prerequisite_gate(self, definition):
        """
        The sentence explaining why this test is gated, or None.

        Only hardware results open a gate. A prerequisite that is
        READY_FOR_HARDWARE has been prepared, not passed.
        """
        if self.ledger is None:
            return None

        if self.context.mode != Mode.EXECUTE:
            return None

        unsatisfied = []

        for test_id in self.registry.resolve_prerequisites(definition):
            status = self.ledger.status_of(test_id)

            if status != Status.PASS:
                unsatisfied.append((test_id, status))

        if not unsatisfied:
            return None

        return (
            "a lower layer has not passed on hardware, so a result here "
            "would not be evidence: {}. Run those first, or use "
            "--expert-override with --override-reason if you are "
            "deliberately diagnosing out of order.".format(
                ", ".join("{} is {}".format(t, s)
                          for t, s in unsatisfied[:6]))
        )

    def _allow_override(self, definition, gate, result):
        """
        The expert override: a flag, a written reason, and a yes.

        All three are required and all three are recorded in the
        evidence. An override that is not visible in the result would
        turn a diagnostic detour into a claim about the hardware.
        """
        if not self.expert_override:
            return False

        if not self.override_reason.strip():
            result.notes.append(
                "--expert-override was given without "
                "--override-reason; the override was refused")

            return False

        question = (
            "EXPERT OVERRIDE: {} is gated because {} Override the gate "
            "for this test?".format(definition.test_id, gate)
        )

        try:
            agreed = self.context.operator.confirm(
                definition.test_id, question)

        except Blocked:
            # Non-interactive: an override cannot be confirmed, so it
            # does not happen.
            result.notes.append(
                "an expert override cannot be confirmed in "
                "--non-interactive mode")

            return False

        if not agreed:
            return False

        record = {
            "overridden_gate": gate,
            "reason": self.override_reason,
            "confirmed": True,
        }

        result.override = record

        self.context.event("expert_override", test_id=definition.test_id,
                           **record)

        result.notes.append(
            "EXPERT OVERRIDE: a layer gate was bypassed. Reason: "
            "{}".format(self.override_reason))

        return True

    def _safety_confirmation(self, definition, result):
        """
        The class-specific question, asked once per safety class per run.

        Once per class rather than once per test: an operator who has
        confirmed the mechanism is clear should not be asked forty times
        during a repeatability campaign, and asking forty times is how
        confirmations become reflexive.
        """
        if definition.safety in self._safety_confirmed:
            return True

        question = Safety.CONFIRMATION_QUESTION.get(
            definition.safety,
            "This test is classified {}. Confirm you accept what it "
            "does".format(definition.safety))

        try:
            agreed = self.context.operator.confirm(
                definition.test_id, question)

        except Blocked as blocked:
            result.status = Status.BLOCKED
            result.reason = blocked.reason

            return False

        if not agreed:
            result.status = Status.SKIPPED
            result.reason = "the operator declined the {} safety " \
                            "confirmation".format(definition.safety)

            return False

        self._safety_confirmed.add(definition.safety)

        self.context.event("safety_confirmed",
                           safety=definition.safety,
                           test_id=definition.test_id)

        return True

    # ------------------------------------------------------------------
    # cleanup
    # ------------------------------------------------------------------

    def _cleanup(self, definition, result):
        """
        Run the test's cleanup, and record honestly whether it worked.

        Never raises. A cleanup that throws replaces its own record with
        the failure - it does not replace the test's verdict, because a
        test that failed and then cleaned up badly failed for the reason
        it failed.
        """
        if definition.cleanup is None:
            result.cleanup = {"handler": False, "confirmed": None}

            return

        if self.context.mode not in (Mode.EXECUTE, Mode.SELFTEST):
            result.cleanup = {
                "handler": True,
                "confirmed": None,
                "note": "not run: no hardware was touched",
            }

            return

        try:
            record = definition.cleanup(self.context)

            if not isinstance(record, dict):
                record = {"confirmed": bool(record)}

            record.setdefault("confirmed", True)
            record["handler"] = True

        except KeyboardInterrupt:
            record = {
                "handler": True,
                "confirmed": False,
                "error": "cleanup was itself interrupted",
            }

            self.aborted = True

        except Exception as error:
            record = {
                "handler": True,
                "confirmed": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }

        result.cleanup = record

        self.context.event("cleanup", test_id=definition.test_id, **record)

        if not record.get("confirmed"):
            result.notes.append(
                "CLEANUP NOT CONFIRMED: {}. The physical state after "
                "this test is not known.".format(
                    record.get("error", "no reason recorded")))

    # ------------------------------------------------------------------

    def _make(self, definition, status, reason):
        return TestResult(definition, status, self.evidence_class, reason)

    def _unrun(self, definition, status, reason):
        result = self._make(definition, status, reason)

        self.results_note(result)

        return result

    def results_note(self, result):
        self.context.event("test_not_run", test_id=result.test_id,
                           status=result.status, reason=result.reason)

    def _finish(self, result):
        result.finished_at = now()

        if self.ledger is not None:
            self.ledger.record(
                result,
                self.context.evidence.run_id
                if self.context.evidence else None)

        self.context.event("test_end", test_id=result.test_id,
                           status=result.status,
                           reported_status=result.reported_status(),
                           reason=result.reason,
                           duration_s=result.duration)

        print("   {:<20} {:<8} {}".format(
            result.test_id, result.reported_status(),
            result.reason[:90]), flush=True)

        self.context.definition = None
        self.context.result = None

        return result

    def _announce(self, definition):
        print()
        print("== {}  {}".format(definition.test_id, definition.title))
        print("   layer {}   safety {}   {}".format(
            definition.layer, definition.safety, definition.automation),
            flush=True)
