"""
What a test body is handed, and the gate every hardware call goes through.

`ctx` is the whole API a test sees:

    ctx.link, ctx.servo, ctx.sensor, ctx.carousel, ctx.workflow
                            the adapters
    ctx.check(...)          one observation; any failure fails the test
    ctx.require(...)        a capability, or BLOCKED with the reason
    ctx.observe / ctx.ask   the operator
    ctx.record(...)         an event
    ctx.measure(...)        a row of numbers
    ctx.iterations(...)     a bounded, validated repeat count
    ctx.defect(...)         a persistent HW-xxx failure record

THE GATE

`require_hardware_mode` is called by every adapter method that would
touch a device. It raises unless the run is EXECUTE and the operator
confirmed the hardware. There is no flag that turns it off and no way
for a test to reach a port around it - which is what makes "--list,
--describe and --dry-run cannot touch hardware" a property of the code
rather than a promise in a README.
"""

from ..adapters import (BenchAdapter, CarouselAdapter,
                        DiagnosticAdapter, LinkAdapter, SensorAdapter,
                        ServoAdapter, WorkflowAdapter)
from ..configuration import ports as ports_module
from .model import (Blocked, Characterization, Check, Failure,
                    Inconclusive, Mode, Requirement, Skip, now)


class HardwareNotPermitted(RuntimeError):
    """
    A test tried to touch hardware in a mode that forbids it.

    Deliberately a RuntimeError and not one of the control-flow
    exceptions: this is a programming error in the framework, not an
    outcome of a test, and it must not be swallowed into a tidy BLOCKED.
    """


class RunContext:
    """One run's shared state, plus the per-test recording surface."""

    def __init__(self, mode, profile, evidence, operator,
                 hardware_confirmed=False, device=None,
                 iteration_overrides=None, verbose=False,
                 fake_transport=False):
        if mode not in Mode.ALL:
            raise ValueError("unknown mode {!r}".format(mode))

        if fake_transport and mode != Mode.SELFTEST:
            raise ValueError(
                "a fake transport may only be installed in SELFTEST "
                "mode; {} was requested. A real run must never be able "
                "to reach a fake device.".format(mode))

        self.mode = mode

        # SELFTEST only, and only when `offline_tests/fake_link.py` has
        # replaced the transport. It is what lets the framework's own
        # tests drive real test bodies without a board, and it is
        # stamped onto every result as FRAMEWORK_SELFTEST evidence.
        self.fake_transport = bool(fake_transport)
        self.profile = profile
        self.evidence = evidence
        self.operator = operator
        self.hardware_confirmed = bool(hardware_confirmed)
        self.verbose = bool(verbose)

        self._device = device
        self._device_detail = None

        self.iteration_overrides = dict(iteration_overrides or {})

        # Per-test state, reset by the runner before each definition.
        self.definition = None
        self.result = None

        self.link = LinkAdapter(self)
        self.servo = ServoAdapter(self, self.link)
        self.sensor = SensorAdapter(self, self.link)
        self.carousel = CarouselAdapter(self, self.link)
        self.workflow = WorkflowAdapter(self, self.link)
        self.diagnostic = DiagnosticAdapter(self, self.link)
        self.bench = BenchAdapter(self)

        self.adapters = {
            "link": self.link,
            "servo": self.servo,
            "sensor": self.sensor,
            "carousel": self.carousel,
            "workflow": self.workflow,
            "diagnostic": self.diagnostic,
            "bench": self.bench,
        }

        self._capabilities = None

    # ------------------------------------------------------------------
    # the gate
    # ------------------------------------------------------------------

    @property
    def touches_hardware(self):
        return self.mode in Mode.TOUCHES_HARDWARE

    def require_hardware_mode(self, what):
        """
        Refuse anything physical outside a confirmed EXECUTE run.

        The message names the operation, because a framework bug that
        reaches here during a dry run has to be findable.
        """
        if self.mode == Mode.SELFTEST and self.fake_transport:
            # The framework's own tests, against a deterministic fake.
            # No device exists, nothing can move, and every result
            # produced this way carries FRAMEWORK_SELFTEST evidence.
            return True

        if self.mode != Mode.EXECUTE:
            raise HardwareNotPermitted(
                "a test tried to {} in {} mode. Only --run / "
                "--run-campaign with --confirm-hardware may touch a "
                "device.".format(what, self.mode))

        if not self.hardware_confirmed:
            raise HardwareNotPermitted(
                "a test tried to {} before the operator confirmed the "
                "hardware. This is a framework bug: the runner must "
                "obtain confirmation before the first test "
                "runs.".format(what))

        return True

    # ------------------------------------------------------------------
    # the device
    # ------------------------------------------------------------------

    def device(self):
        """
        The one device this run talks to, resolved once.

        Resolution happens on first use and only in EXECUTE mode, which
        keeps port enumeration out of every other path.
        """
        if self._device:
            return self._device

        self.require_hardware_mode("choose a serial device")

        found = ports_module.resolve(
            self.profile.selector(),
            self.link.enumerate_ports(),
            ports_module.by_id_entries(),
        )

        self._device = found["device"]
        self._device_detail = found

        self.event("device_resolved", **found)

        return self._device

    def device_detail(self):
        return self._device_detail

    # ------------------------------------------------------------------
    # capabilities
    # ------------------------------------------------------------------

    def capabilities(self):
        """Every capability of every adapter, detected without hardware."""
        if self._capabilities is None:
            found = {}

            for adapter in self.adapters.values():
                found.update(adapter.capabilities())

            self._capabilities = found

        return self._capabilities

    def capability(self, name):
        return self.capabilities().get(name)

    def has(self, name):
        found = self.capability(name)

        return bool(found and found.available)

    def require(self, *names):
        """
        Every named capability, or BLOCKED naming the first missing one.

        The Blocked carries the adapter's own recommendation, so the
        route out of the block travels with the result instead of living
        only in somebody's memory.
        """
        for name in names:
            found = self.capability(name)

            if found is None:
                raise Blocked(
                    "the framework has no capability called {!r} - this "
                    "is a framework bug, not a hardware "
                    "limitation".format(name),
                    capability=name,
                    recommendation="Fix the test's `requires` list or "
                                   "add the capability to its adapter.",
                )

            if not found.available:
                raise Blocked(
                    "{} is not available: {}".format(name, found.reason),
                    capability=name,
                    recommendation=found.recommendation,
                )

        return True

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def event(self, kind, /, **fields):
        """
        One event, tagged with the test that produced it.

        `kind` is positional-only because a caller's fields are
        arbitrary and one of them is genuinely called `kind` - a Check
        records whether it was AUTOMATIC or OPERATOR. See the note on
        `EvidenceWriter.event`.
        """
        if self.evidence is None:
            return None

        if self.definition is not None:
            fields.setdefault("test_id", self.definition.test_id)
            fields.setdefault("campaign", self.definition.campaign)

        return self.evidence.event(kind, **fields)

    def record(self, stage, /, **fields):
        """A stage of a test, for the event log."""
        fields.setdefault("stage", stage)

        return self.event("stage", **fields)

    def say(self, text):
        """Progress for the operator. Never the evidence."""
        print("      {}".format(text), flush=True)

    def check(self, condition, description, evidence=None,
              kind="AUTOMATIC"):
        """
        One observation. Returns the condition so it can be used inline.

        A FAILED CHECK DOES NOT RAISE. The test continues and collects
        the rest of its evidence, because the second and third failures
        are usually what identify the fault. The runner turns any failed
        check into FAIL at the end.
        """
        entry = Check(description, bool(condition), evidence, kind)

        self.result.checks.append(entry)

        # `check_kind`, not `kind`: the event's own kind is "check", and
        # AUTOMATIC / OPERATOR is a property of the observation. The
        # evidence writer would keep both safely either way, but a log
        # that reads `"kind": "check", "check_kind": "OPERATOR"` needs no
        # explaining.
        fields = entry.as_dict()
        fields["check_kind"] = fields.pop("kind")

        self.event("check", **fields)

        if self.verbose or not entry.ok:
            print("      {:<4} {}".format(
                "ok" if entry.ok else "FAIL", description), flush=True)

        return entry.ok

    def measure(self, **row):
        """One row of numbers, into measurements.csv and the event log."""
        if self.definition is not None:
            row.setdefault("test_id", self.definition.test_id)

        self.result.measurements.append(row)

        if self.evidence is not None:
            self.evidence.measurement(row)

        return row

    def note(self, text):
        self.result.notes.append(str(text))
        self.event("note", text=str(text))

    # ------------------------------------------------------------------
    # outcomes
    # ------------------------------------------------------------------

    def fail(self, reason, defect=None, evidence=None):
        raise Failure(reason, defect=defect, evidence=evidence)

    def inconclusive(self, reason, missing=(), evidence=None):
        """
        The procedure ran and cannot support a verdict.

        Not a failure. Nothing was observed to be wrong; the thing the
        test exists to establish was simply not established.
        """
        raise Inconclusive(reason, missing=missing, evidence=evidence)

    def characterize(self, reason, measurements=None):
        """
        Measurements were collected and no requirement judges them.

        Raised by the tests whose whole job is to produce numbers. It
        ends the body, so it goes last - anything after it would not
        run.
        """
        raise Characterization(reason, measurements=measurements)

    # ------------------------------------------------------------------
    # observations, classified
    # ------------------------------------------------------------------

    def observed(self, description, value, expected=None,
                 requirement=Requirement.REQUIRED, matches=None,
                 evidence=None):
        """
        Record one observation, and treat a MISSING one honestly.

        THE DEFECT THIS REPLACES, which was in eight test bodies:

            ctx.check(reported is None or reported == expected, ...)

        which reads "the device identifies correctly OR does not
        identify at all" and passes both. A device that returned no
        firmware name passed the firmware-identity check.

        Here, for a REQUIRED observation:

            value is None      -> recorded as missing. The runner turns
                                  the result INCONCLUSIVE: the
                                  procedure ran, the evidence is not
                                  there.
            value != expected  -> a failed check. FAIL: the contract was
                                  violated.
            value == expected  -> a passed check.

        An OPTIONAL_DIAGNOSTIC observation may be absent, is recorded
        either way, and never decides anything.
        """
        detail = dict(evidence or {})
        detail.update({"observed": value, "expected": expected,
                       "requirement": requirement})

        entry = {"description": description, "value": value,
                 "expected": expected, "requirement": requirement}

        self.result.observations.append(entry)

        if value is None:
            if requirement == Requirement.REQUIRED:
                self.result.record_missing_required(description, detail)

                self.check(False,
                           "{} (REQUIRED, and it was not "
                           "reported)".format(description),
                           evidence=detail)

                return False

            # `detail` already carries `requirement`; passing it again
            # explicitly is a duplicate keyword and a TypeError.
            self.event("observation_absent", description=description,
                       **detail)

            self.note(
                "{} was not reported. It is {}, so nothing depends on "
                "it.".format(description, requirement))

            return None

        if requirement == Requirement.NOT_AVAILABLE:
            self.note(
                "{} arrived from a system that is not supposed to be "
                "able to produce it; recording it, deciding "
                "nothing.".format(description))

            return None

        if matches is None:
            ok = value == expected if expected is not None else True

        else:
            ok = bool(matches(value))

        if requirement == Requirement.OPTIONAL_DIAGNOSTIC:
            self.event("observation", description=description,
                       satisfied=ok, **detail)

            return ok

        self.check(ok, description, evidence=detail)

        return ok

    def require_observation(self, description, value, evidence=None):
        """
        A REQUIRED observation whose mere presence is what matters.

        Used where the objective needs a field to EXIST - a measurement
        id, a slot association, a raw spectrum - rather than to equal
        something.
        """
        present = value is not None and value != "" and value != []

        if not present:
            self.result.record_missing_required(description, evidence)

        self.check(present,
                   "{} is present".format(description),
                   evidence=dict(evidence or {}, observed=value))

        return present

    def block(self, reason, capability=None, recommendation=""):
        raise Blocked(reason, capability=capability,
                      recommendation=recommendation)

    def skip(self, reason):
        raise Skip(reason)

    def defect(self, title, observed, expected, reproduction=(),
               suspected_layer=None, evidence=None, sequence=None):
        """
        Raise a persistent HW-xxx failure record.

        The ID comes from the test's own `defect_prefix`, so a serial
        failure is always HW-SER-nnn and a carousel failure is always
        HW-CAR-nnn no matter which test found it - which is what lets a
        defect be referred to across runs and across campaigns.
        """
        prefix = (self.definition.defect_prefix
                  if self.definition is not None else "HW-GEN")

        number = sequence if sequence is not None else (
            len(self.result.defects) + 1)

        record = {
            "defect_id": "{}-{:03d}".format(prefix, number),
            "title": title,
            "observed": observed,
            "expected": expected,
            "reproduction": list(reproduction),
            "suspected_layer": suspected_layer,
            "evidence": evidence if evidence is not None else {},
            "status": "OPEN",
            "test_id": self.definition.test_id,
            "campaign": self.definition.campaign,
            "assumption": self.definition.assumption,
            "run_id": (self.evidence.run_id
                       if self.evidence is not None else None),
            "raised_utc": None,
        }

        if self.evidence is not None:
            from .evidence import iso

            record["raised_utc"] = iso()
            self.evidence.write_defect(record)

        self.result.defects.append(record)
        self.event("defect", **record)

        return record

    # ------------------------------------------------------------------
    # the operator
    # ------------------------------------------------------------------

    def instruct(self, instruction):
        return self.operator.instruct(self.definition.test_id, instruction)

    def ask(self, question, default=None):
        return self.operator.confirm(
            self.definition.test_id, question, default)

    def ask_number(self, question, minimum=None, maximum=None, unit=""):
        value = self.operator.number(
            self.definition.test_id, question, minimum, maximum, unit)

        self.result.observations.append(
            {"question": question, "answer": value, "kind": "MEASUREMENT"})

        return value

    def observe(self, question, options):
        answer = self.operator.choice(
            self.definition.test_id, question, options)

        self.result.observations.append(
            {"question": question, "answer": answer, "kind": "OBSERVATION"})

        return answer

    def observe_direction(self, question="Observed direction of travel"):
        return self.observe(question,
                            ("CW", "CCW", "NO_MOTION", "UNKNOWN"))

    def operator_note(self, question="Operator notes (optional)"):
        answer = self.operator.text(self.definition.test_id, question)

        self.result.observations.append(
            {"question": question, "answer": answer, "kind": "NOTE"})

        return answer

    def confirm_observation(self, question, expected=True):
        """
        Ask the operator, and record the answer AS A CHECK.

        Marked OPERATOR so that a reader of the evidence can always tell
        which checks were measured and which were seen by a human.
        """
        answer = self.operator.confirm(self.definition.test_id, question)

        self.check(answer == expected, question,
                   evidence={"operator_answer": answer,
                             "expected": expected},
                   kind="OPERATOR")

        return answer

    # ------------------------------------------------------------------
    # bounded repetition
    # ------------------------------------------------------------------

    def iterations(self, requested=None):
        """
        A validated iteration count for the current test.

        Rejects zero, negatives, non-integers and anything above the
        test's declared ceiling or the profile's limit. An endurance
        campaign with an unbounded loop is not a test, it is a way to
        leave a servo running overnight by accident.
        """
        definition = self.definition

        if definition.default_iterations is None:
            raise HardwareNotPermitted(
                "{} asked for an iteration count but declares none. Add "
                "default_iterations and max_iterations to its "
                "definition.".format(definition.test_id))

        value = requested

        if value is None:
            value = self.iteration_overrides.get(definition.test_id)

        if value is None:
            value = self.iteration_overrides.get("*")

        if value is None:
            value = definition.default_iterations

        if isinstance(value, bool) or not isinstance(value, int):
            raise Failure(
                "iterations must be a whole number, got {!r}".format(value))

        if value < 1:
            raise Failure(
                "iterations must be at least 1, got {}. A zero-iteration "
                "endurance run would report PASS having done "
                "nothing.".format(value))

        if value > definition.max_iterations:
            raise Failure(
                "{} allows at most {} iterations and {} were requested. "
                "The ceiling is a safety limit; raise it in the test "
                "definition deliberately, with a reason, or lower the "
                "request.".format(definition.test_id,
                                  definition.max_iterations, value))

        ceiling = self._profile_ceiling()

        if ceiling is not None and value > ceiling:
            raise Failure(
                "the bench profile limits this to {} iterations and {} "
                "were requested.".format(ceiling, value))

        self.result.iterations = value

        self.event("iterations", requested=requested, resolved=value,
                   maximum=definition.max_iterations)

        return value

    def _profile_ceiling(self):
        """The profile limit that applies to this test's safety class."""
        limits = self.profile.limits
        campaign = self.definition.campaign

        mapping = {
            "B1": "max_open_cycles",
            "B4": "max_movements",
            "B5": "max_movements",
            "B7": "max_measurements",
            "B11": "max_requests",
            "B12": "max_missions",
        }

        key = mapping.get(campaign)

        return limits.get(key) if key else None

    # ------------------------------------------------------------------

    def elapsed(self):
        return now()
