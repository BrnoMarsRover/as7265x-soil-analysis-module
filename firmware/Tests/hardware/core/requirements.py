"""
What the hardware campaign is required to establish, and where each
requirement comes from.

WHY REQUIREMENTS ARE A FIRST-CLASS OBJECT AND NOT A COMMENT

A test that cannot name what it is evidence FOR is a test nobody can
retire, re-scope or trust. Three questions have to be answerable from
the catalogue itself:

    which tests establish this requirement?
    which requirements does this test establish?
    on whose authority is the acceptance criterion set?

The third is the one that keeps a campaign honest. An acceptance
criterion with no source is a number somebody invented, and a test that
judges hardware against an invented number produces a verdict that
cannot survive being questioned. So every requirement declares its
`source`, and a requirement whose source is `NONE` may only ever produce
CHARACTERIZATION - measurements, recorded, judged by nobody.

THE SOURCES, AND WHAT EACH ONE MEANS

    PRODUCTION_CONFIG  firmware/ESP32/config.py ships the value. True by
                       definition: it is what the code does.
    PROTOCOL           the PC and firmware agree on it in code, and
                       Tests/software verifies the agreement.
    DESIGN             a stated design intent in the repository's own
                       documentation or architecture.
    DATASHEET          a component datasheet. Needs the part and the
                       figure, not a memory of one.
    MEASURED_BASELINE  a number this campaign itself measured on real
                       hardware and somebody then adopted.
    NONE               nothing authoritative exists yet. CHARACTERIZATION
                       only. This is not a defect in the requirement; it
                       is an accurate statement about the project.
"""


class Source:
    PRODUCTION_CONFIG = "PRODUCTION_CONFIG"
    PROTOCOL = "PROTOCOL"
    DESIGN = "DESIGN"
    DATASHEET = "DATASHEET"
    MEASURED_BASELINE = "MEASURED_BASELINE"
    NONE = "NONE"

    ALL = (PRODUCTION_CONFIG, PROTOCOL, DESIGN, DATASHEET,
           MEASURED_BASELINE, NONE)

    # Sources that can support a PASS/FAIL verdict. Anything else may
    # only characterize.
    AUTHORITATIVE = (PRODUCTION_CONFIG, PROTOCOL, DESIGN, DATASHEET,
                     MEASURED_BASELINE)


class VerifiedBy:
    """
    Who is supposed to establish this requirement.

    Most requirements are about the hardware and are established by a
    registered hardware test. A few are about the FRAMEWORK - that
    evidence is complete, that a stale ledger cannot open a gate, that
    an offline path opens no port - and those are established by the
    offline suite, on no hardware, which is the only place they CAN be
    established. Saying so is more honest than inventing a hardware test
    that would not test them.
    """

    HARDWARE_TEST = "HARDWARE_TEST"
    OFFLINE_SUITE = "OFFLINE_SUITE"

    ALL = (HARDWARE_TEST, OFFLINE_SUITE)


class HardwareRequirement:
    """One thing the campaign must establish about the real hardware."""

    def __init__(self, requirement_id, title, statement, source,
                 rationale="", assumption=None,
                 verified_by=VerifiedBy.HARDWARE_TEST):
        self.requirement_id = str(requirement_id)
        self.title = str(title)
        self.statement = str(statement)
        self.source = str(source)
        self.rationale = str(rationale)
        self.assumption = assumption
        self.verified_by = str(verified_by)

        if self.source not in Source.ALL:
            raise ValueError(
                "{}: unknown source {!r}".format(
                    self.requirement_id, self.source))

        if self.verified_by not in VerifiedBy.ALL:
            raise ValueError(
                "{}: unknown verified_by {!r}".format(
                    self.requirement_id, self.verified_by))

    @property
    def authoritative(self):
        """Whether a PASS/FAIL verdict may be drawn against this."""
        return self.source in Source.AUTHORITATIVE

    def as_dict(self):
        return {
            "requirement_id": self.requirement_id,
            "title": self.title,
            "statement": self.statement,
            "source": self.source,
            "authoritative": self.authoritative,
            "verified_by": self.verified_by,
            "rationale": self.rationale,
            "assumption": self.assumption,
        }


def _requirement(*args, **kwargs):
    requirement = HardwareRequirement(*args, **kwargs)

    if requirement.requirement_id in REQUIREMENTS:
        raise ValueError(
            "duplicate requirement id {}".format(
                requirement.requirement_id))

    REQUIREMENTS[requirement.requirement_id] = requirement

    return requirement


REQUIREMENTS = {}


# ======================================================================
# B0 - the bench
# ======================================================================

_requirement(
    "HW-REQ-ENV-001", "The host can run the production client",
    "The machine driving the campaign runs a Python the production "
    "client supports, with pyserial importable by the one serial owner.",
    Source.DESIGN,
    "firmware/PC/requirements.txt names pyserial>=3.5, and the client "
    "refuses to start without it.")

_requirement(
    "HW-REQ-ENV-002", "The campaign is tied to an exact revision",
    "Every run records the repository commit and whether the tree was "
    "dirty, so a result can be attributed to the code that produced it.",
    Source.DESIGN,
    "A result that cannot be tied to a revision cannot be reproduced.")

_requirement(
    "HW-REQ-ENV-003", "The shipped geometry is self-consistent",
    "The loader/scanner offset is half the slot count, counts-per-slot "
    "times slot count is counts-per-revolution, and the half-turn "
    "constant is half a revolution.",
    Source.PRODUCTION_CONFIG,
    "firmware/ESP32/carousel.py checks the first invariant itself and "
    "refuses to run without it.")

_requirement(
    "HW-REQ-ENV-004", "The bench profile identifies exactly one device",
    "The profile's selector resolves to one serial device, and that "
    "device has an identity that survives a replug.",
    Source.DESIGN,
    "Linux renumbers ttyUSBn on reconnect. A campaign that opens the "
    "wrong board reports a fault in an instrument never under test.")

_requirement(
    "HW-REQ-ENV-005", "The wiring matches the configuration",
    "A human has confirmed the ST3215 pins, the servo supply, the "
    "AS7265x bus pins, mechanical freedom and an empty carousel.",
    Source.PRODUCTION_CONFIG,
    "The ECHO_ONLY fault on this bench was a pin-order problem that no "
    "amount of firmware testing could have found.")

_requirement(
    "HW-REQ-ENV-006", "The physical unit under test is identified",
    "The module, servo, sensor and mechanical assembly carry recorded "
    "identifiers, so a PASS can be attributed to a specific instrument.",
    Source.DESIGN,
    "A prerequisite PASS earned on one module says nothing about "
    "another. Without unit identity the layer gates are unsound.")

_requirement(
    "HW-REQ-ENV-007", "Measurement instruments are identified",
    "Any multimeter, oscilloscope or logic analyzer used is recorded "
    "with its identifier and calibration date.",
    Source.DESIGN,
    "An electrical measurement from an unidentified, uncalibrated "
    "instrument is an anecdote.")

_requirement(
    "HW-REQ-ENV-008", "The power topology is recorded",
    "The actual bench power topology is captured: regulated input, the "
    "3V3 sensor rail, the external servo supply and the common ground.",
    Source.DESIGN,
    "Documentation/ARCHITECTURE.md states the servo has an external "
    "supply at its driver and must share ground, and that servo current "
    "must not pass through the sensor PCB. What the bench actually does "
    "has to be written down before any current measurement means "
    "anything.")

_requirement(
    "HW-REQ-ENV-009", "Power isolation and emergency stop are reachable",
    "The operator can remove power and stop the mechanism without "
    "reaching past anything that moves.",
    Source.DESIGN,
    "Every motion and endurance test in this campaign assumes the "
    "operator can stop it.")

_requirement(
    "HW-REQ-ENV-010", "The repository does not contradict itself",
    "Configuration, README, operations documentation, plans and "
    "campaign definitions describe the same carousel.",
    Source.PRODUCTION_CONFIG,
    "An operator following a document that says 45 degrees per slot on "
    "a mechanism configured for 90 will mis-set the carousel and blame "
    "the firmware.")


# ======================================================================
# B1 - the transport
# ======================================================================

_requirement(
    "HW-REQ-LINK-001", "The device is the science module",
    "The device answers a ping and identifies with the firmware name "
    "and protocol version this repository ships. A device that returns "
    "no identity does not satisfy this.",
    Source.PROTOCOL,
    "Tests/software/contracts verifies the PC and firmware agree on "
    "these fields, so their absence on real hardware is a real defect.",
    assumption=None)

_requirement(
    "HW-REQ-LINK-002", "Opening the port does not reset the board",
    "Uptime never decreases across a close and reopen.",
    Source.DESIGN,
    "SerialLink.open() drives DTR and RTS low before opening precisely "
    "to avoid the auto-reset circuit. H-008.",
    assumption="H-008")

_requirement(
    "HW-REQ-LINK-003", "get_status answers completely and cheaply",
    "The status carries its sensor, servo and carousel sections and "
    "answers without initializing the sensor.",
    Source.PROTOCOL,
    "A status command that can hang is a status command nobody can use "
    "during a fault.")

_requirement(
    "HW-REQ-LINK-004", "A session's answers cannot reach another session",
    "No frame carrying one session's request id is accepted by another.",
    Source.PROTOCOL,
    "The request-id nonce exists for this. A stale frame accepted here "
    "hands one client another's measurement.")

_requirement(
    "HW-REQ-LINK-005", "The bridge survives repeated open cycles",
    "At least the qualification number of open/ping/close cycles "
    "complete with no failure and no corruption.",
    Source.NONE,
    "H-004 and RF-002. No authoritative reliability figure exists for "
    "this bridge, so the campaign measures one.",
    assumption="H-004")

_requirement(
    "HW-REQ-LINK-006", "One open port survives sustained traffic",
    "The qualification number of requests complete on a single open "
    "port with no corrupt, stale or oversized frame.",
    Source.NONE,
    "No authoritative figure exists; the campaign measures the rate.")

_requirement(
    "HW-REQ-LINK-007", "Response latency is characterized",
    "The latency distribution of each command is recorded, including "
    "p50, p95, p99 and worst.",
    Source.NONE,
    "There is no latency requirement. There IS a need to argue about "
    "timeouts from measurements rather than from guesses.")

_requirement(
    "HW-REQ-LINK-008", "The device is identifiable across a replug",
    "After a disconnect and reconnect the same physical module is "
    "identifiable by USB serial number or by-id path.",
    Source.DESIGN,
    "The device path may change; the identity may not.")

_requirement(
    "HW-REQ-LINK-009", "Damaged frames are captured, not just counted",
    "Any corrupt line is retained in a lossless form, and the known "
    "64-leading-byte CP210x syndrome is classified when it appears.",
    Source.MEASURED_BASELINE,
    "Measured on this bench: exactly 64 leading bytes - one CP210x USB "
    "packet - replaced by undecodable rubbish, the rest byte-perfect. "
    "A counter cannot say that; only the line can.")

_requirement(
    "HW-REQ-LINK-010", "Payload size does not break the link",
    "Small, medium and full 54-feature responses all arrive intact.",
    Source.PROTOCOL,
    "The largest legitimate frame is sensor_test_raw at MAX_REPEATS, "
    "measured at 16,454 bytes. The reader's 64 KiB cap is set against "
    "it.")

_requirement(
    "HW-REQ-LINK-011", "Only one client owns the port",
    "A second client attempting the same device is refused rather than "
    "interleaving bytes on one wire.",
    Source.DESIGN,
    "SerialLink asks the OS for exclusive access. The harness adds a "
    "process-level lock so two harness runs cannot fight either.")

_requirement(
    "HW-REQ-LINK-012", "Device memory does not drift under load",
    "ESP32 uptime, heap and reset cause are observed across sustained "
    "traffic and large responses, and the heap does not trend down.",
    Source.NONE,
    "H-007. No authoritative budget exists; the campaign measures it.",
    assumption="H-007")


# ======================================================================
# B2 to B4 - the servo
# ======================================================================

_requirement(
    "HW-REQ-SERVO-001", "The servo driver comes up",
    "connect_servo attaches the driver and the servo answers.",
    Source.PROTOCOL, "Nothing above this layer means anything without it.")

_requirement(
    "HW-REQ-SERVO-002", "The servo is the configured servo",
    "The reported id and baud rate match ST3215_SERVO_ID and "
    "ST3215_BAUD. A servo that reports neither does not satisfy this.",
    Source.PRODUCTION_CONFIG,
    "Four independent things must be right before a servo answers, and "
    "'no answer' names all four while testing none.")

_requirement(
    "HW-REQ-SERVO-003", "The servo is in the mode the driver assumes",
    "The mode register reads ST3215_MODE (STEP).",
    Source.PRODUCTION_CONFIG,
    "In position mode a relative goal is interpreted as an absolute "
    "one, which is hypothesis 1 of H-002 and would explain the whole "
    "contradiction.",
    assumption="H-002")

_requirement(
    "HW-REQ-SERVO-004", "A stationary servo reports a stable position",
    "Repeated position reads with nothing commanded return one value, "
    "or the jitter is characterized before anything depends on it.",
    Source.NONE,
    "Hypothesis 5 of H-002. A read that is not stable at rest cannot "
    "measure a movement.",
    assumption="H-002")

_requirement(
    "HW-REQ-SERVO-005", "The deployed firmware is the shipped firmware",
    "The servo calibration report matches every value in config.py.",
    Source.PRODUCTION_CONFIG,
    "A mismatch means the campaign is attributing results to the wrong "
    "code.")

_requirement(
    "HW-REQ-SERVO-006", "A malformed servo command is refused, not obeyed",
    "servo_test_move without confirmation, with an unknown kind, and "
    "with an out-of-range repeat are each refused with a code, and "
    "nothing moves.",
    Source.PROTOCOL,
    "A confirmation gate that can be skipped is not a gate.")

_requirement(
    "HW-REQ-SERVO-007", "Raw servo bytes are observable",
    "A position read can be captured as the bytes that came off the "
    "bus, so the driver's parsing can be checked against the wire.",
    Source.NONE,
    "Hypothesis 8 of H-002. Needs a diagnostic command the competition "
    "firmware does not have.",
    assumption="H-002")

_requirement(
    "HW-REQ-SERVO-008", "Servo telemetry is observable",
    "Position, speed, load, voltage, current, temperature and the "
    "moving/status bits can be read.",
    Source.NONE,
    "A movement that failed because the supply drooped and one that "
    "failed because the encoder lied look identical without this.")

_requirement(
    "HW-REQ-SERVO-009", "Torque and stop behave as commanded",
    "Torque can be enabled and disabled, and a stop is bounded.",
    Source.PROTOCOL,
    "A carousel whose torque is dropped can be turned by gravity.")

_requirement(
    "HW-REQ-SERVO-010", "The servo mode survives a reset",
    "After an ESP32 reset and a servo power cycle the mode register "
    "still reads the configured mode.",
    Source.PRODUCTION_CONFIG,
    "A servo that returns to position mode on power-up would reproduce "
    "H-002 intermittently and look like a software fault.",
    assumption="H-002")

_requirement(
    "HW-REQ-SERVO-011", "Closing error is characterized",
    "The closing-error distribution of repeated symmetrical movements "
    "is measured, including observations the production verifier "
    "rejected.",
    Source.NONE,
    "H-001. ST3215_POSITION_TOLERANCE = 15 is the one shipped constant "
    "that is a guess, and it cannot be re-derived from movements it "
    "already filtered.",
    assumption="H-001")

_requirement(
    "HW-REQ-SERVO-012", "Movement timing is characterized",
    "Elapsed time per movement is measured and compared with the "
    "configured move timeout.",
    Source.PRODUCTION_CONFIG,
    "A movement whose duration approaches the timeout means the timeout "
    "is doing nothing.")

_requirement(
    "HW-REQ-SERVO-013", "The servo bus is clean under load",
    "No transport or servo-bus error counter rises while movements run.",
    Source.DESIGN,
    "A bus that only misbehaves under load is the hardest fault to find "
    "later.")

_requirement(
    "HW-REQ-SERVO-014", "Direction and angle behave symmetrically",
    "CW and CCW movements of the same magnitude produce comparable "
    "error distributions, and error does not grow with angle.",
    Source.NONE,
    "An error that scales with the angle points at resolution or ratio, "
    "not at repeatability.")

_requirement(
    "HW-REQ-SERVO-015", "Thermal and load effects are characterized",
    "Closing error and timing are measured cold and warm, unloaded and "
    "with a bounded representative load.",
    Source.NONE,
    "A tolerance measured only cold and only unloaded is a tolerance for "
    "a mechanism nobody will operate.")


# ======================================================================
# B3 - H-002
# ======================================================================

_requirement(
    "HW-REQ-H002-001", "Commanded, reported and physical agree",
    "For every commanded segment, the encoder delta matches the "
    "commanded count and the physically measured angle matches the "
    "commanded angle.",
    Source.PRODUCTION_CONFIG,
    "The contradiction that opened H-002: about 180 degrees observed, "
    "about 2 counts reported, 2048 expected.",
    assumption="H-002")

_requirement(
    "HW-REQ-H002-002", "Encoder resolution is measured, not assumed",
    "One observed revolution of the output shaft is measured in counts "
    "and compared with ST3215_COUNTS_PER_REV.",
    Source.NONE,
    "Hypothesis 2. If it differs, every angle in the firmware is wrong "
    "by that ratio and H-005 explains itself.",
    assumption="H-005")

_requirement(
    "HW-REQ-H002-003", "A position read is fresh",
    "A position read immediately after a movement equals one taken "
    "after settling, against a characterized stationary jitter floor.",
    Source.NONE,
    "Hypotheses 5 and 6. A freshness threshold set without knowing the "
    "jitter floor measures the threshold, not the servo.",
    assumption="H-002")

_requirement(
    "HW-REQ-H002-004", "Every segment is judged, not just the endpoint",
    "A long movement is walked in segments no larger than a quarter "
    "turn and each intermediate position is recorded and judged.",
    Source.DESIGN,
    "An endpoint equal modulo 4096 is consistent with a full "
    "revolution, with no movement at all, and with several wrong "
    "answers in between. Only the segments tell them apart.",
    assumption="H-002")

_requirement(
    "HW-REQ-H002-005", "Shaft and carousel are measured separately",
    "The relationship is measured with the carousel detached and with "
    "it assembled, so coupling slip is isolable from encoder error.",
    Source.NONE,
    "Hypotheses 4, 7 and 10 are indistinguishable from the assembled "
    "mechanism alone.",
    assumption="H-005")

_requirement(
    "HW-REQ-H002-006", "Failed movements keep their telemetry",
    "A movement the production driver rejected retains its raw and "
    "parsed response, before/after positions, timing and status.",
    Source.DESIGN,
    "The rejected movements are the interesting ones. Discarding them "
    "is how a campaign measures only its own filter.",
    assumption="H-002")


# ======================================================================
# B5 - the carousel
# ======================================================================

_requirement(
    "HW-REQ-CAR-001", "Every slot transition lands correctly",
    "Each adjacent and non-adjacent transition, in both directions, "
    "completes and leaves the firmware and the plate agreeing.",
    Source.PRODUCTION_CONFIG,
    "Slot addressing is what the whole measurement workflow is built "
    "on.")

_requirement(
    "HW-REQ-CAR-002", "The plate is physically where the slot says",
    "A human or instrument confirms physical centering at the "
    "destination, not merely encoder agreement.",
    Source.DESIGN,
    "Mechanical slip after the encoder is invisible to the encoder. "
    "That is the whole reason H-002 is open.")

_requirement(
    "HW-REQ-CAR-003", "The loader and scanner are 180 degrees apart",
    "The reported load and scan slots match what the operator sees, and "
    "differ by the configured offset.",
    Source.PRODUCTION_CONFIG,
    "Every measurement is taken of whichever slot is actually under the "
    "head.",
    assumption="H-005")

_requirement(
    "HW-REQ-CAR-004", "Backlash is characterized",
    "The resting position of a slot approached from each direction is "
    "measured and the difference recorded.",
    Source.NONE,
    "H-006. A tolerance set from one-directional data hides it.",
    assumption="H-006")

_requirement(
    "HW-REQ-CAR-005", "Drift does not accumulate with rotations",
    "Position after repeated full rotations is compared against an "
    "independent physical reference, with a bound that does not grow "
    "linearly with the rotation count.",
    Source.NONE,
    "A 100-rotation run that accepts 100 times the one-rotation "
    "tolerance has no acceptance criterion at all.",
    assumption="H-006")

_requirement(
    "HW-REQ-CAR-006", "A disturbed carousel can be recovered",
    "After a hand turn, a re-sync restores agreement and the next "
    "movement lands correctly.",
    Source.PROTOCOL,
    "Nothing detects a hand turn. The re-sync is the only way back.")

_requirement(
    "HW-REQ-CAR-007", "Samples are retained through movement",
    "With a bounded representative load, no sample is displaced or "
    "spilled by any commanded transition, and clearance is maintained.",
    Source.DESIGN,
    "An empty carousel is not the carousel that will be operated.")

_requirement(
    "HW-REQ-CAR-008", "The mechanism settles before acquisition",
    "Ringing after a movement decays within the configured settle time.",
    Source.PRODUCTION_CONFIG,
    "SCAN_SETTLE_TIME exists for this; whether it is enough is a "
    "physical question.")

_requirement(
    "HW-REQ-CAR-009", "Sensor-to-sample geometry is recorded",
    "The head-to-sample gap and the centering of the slot under the "
    "head are measured and recorded.",
    Source.NONE,
    "Spectral repeatability is meaningless without knowing the geometry "
    "it was measured at.")

_requirement(
    "HW-REQ-CAR-010", "Fine adjust is bounded and preserves the slot",
    "A fine adjustment stays within MAX_FINE_ADJUST_DEG and does not "
    "change the logical slot.",
    Source.PRODUCTION_CONFIG,
    "Fine adjustment exists for small mechanical corrections only.")


# ======================================================================
# B6 and B7 - the sensor
# ======================================================================

_requirement(
    "HW-REQ-SENSOR-001", "The AS7265x answers at its configured address",
    "The bus scan finds AS7265X_ADDRESS and initialization succeeds.",
    Source.PRODUCTION_CONFIG,
    "0x49, verified on the real board and recorded in config.py.")

_requirement(
    "HW-REQ-SENSOR-002", "The sensor configuration reads back",
    "Integration cycles, gain and measurement mode are written and read "
    "back identical.",
    Source.PRODUCTION_CONFIG,
    "The driver verifies this itself; the campaign confirms it happens "
    "on real silicon.")

_requirement(
    "HW-REQ-SENSOR-003", "Initialization is reliable",
    "Repeated forced initializations all succeed, and the intermittent "
    "AS7265X_NOT_FOUND fault does not appear within the qualification "
    "sample.",
    Source.NONE,
    "The fault has been seen on this bench without a known trigger.")

_requirement(
    "HW-REQ-SENSOR-004", "The sensor recovers without a reboot",
    "After a disconnect and reconnect the sensor re-initializes and "
    "acquires, with no ESP32 reset.",
    Source.DESIGN,
    "The driver never latches a failure. Whether the hardware allows "
    "that is a physical question.")

_requirement(
    "HW-REQ-SENSOR-005", "A cold power-up is distinguished from a reset",
    "Sensor behaviour is observed after genuinely removing sensor power, "
    "not only after resetting the ESP32.",
    Source.DESIGN,
    "An ESP32 reset with the sensor rail still up is not a cold start, "
    "and the post-reset settling the driver waits out exists for the "
    "cold case.")

_requirement(
    "HW-REQ-SENSOR-006", "Every acquisition carries 54 well-formed features",
    "18 channels under each of WHITE, UV and IR; no missing, duplicate "
    "or malformed value; no NaN or infinity; correct channel identity "
    "and order.",
    Source.PROTOCOL,
    "A spectrum of the wrong shape is a communication failure. If it "
    "reaches Science it becomes a scientific mystery instead.")

_requirement(
    "HW-REQ-SENSOR-007", "Malformed shapes are actually detected",
    "The shape validator is exercised against each malformed case, not "
    "only against a healthy device.",
    Source.DESIGN,
    "A valid real acquisition proves the device is well; it proves "
    "nothing about the detector.")

_requirement(
    "HW-REQ-SENSOR-008", "Data-ready latency is inside the driver budget",
    "Every observed data-ready wait is below the wait the driver "
    "computes from the configured integration cycles.",
    Source.PRODUCTION_CONFIG,
    "H-003. A wait at the budget means the timeout is doing nothing.",
    assumption="H-003")

_requirement(
    "HW-REQ-SENSOR-009", "Spectral output is stable and characterized",
    "Per-channel mean, standard deviation, coefficient of variation and "
    "drift are measured on an unchanging target, including warm-up.",
    Source.NONE,
    "Timing stability is not spectral stability. Nothing authoritative "
    "states the allowed spread.")

_requirement(
    "HW-REQ-SENSOR-010", "Saturation and degenerate values are observable",
    "Clipping, zero and negative channel values are detectable and "
    "recorded.",
    Source.DESIGN,
    "A saturated channel that looks like a valid number corrupts every "
    "downstream ratio.")

_requirement(
    "HW-REQ-SENSOR-011", "Ambient light does not reach the sample",
    "A dark acquisition and an ambient-leakage observation bound how "
    "much room light the enclosure admits.",
    Source.NONE,
    "Reflectance from a leaking enclosure is a measurement of the room.")

_requirement(
    "HW-REQ-SENSOR-012", "The named illumination is the one that lights",
    "For each stage, the source that lights is the one the command "
    "named, and no other source is on at the same time.",
    Source.DESIGN,
    "The serial port cannot tell you which bulb lit. The hardware has "
    "WHITE, UV and IR; the requirements say RED, and IR serves it.")

_requirement(
    "HW-REQ-SENSOR-013", "Illumination is off after success AND failure",
    "No source remains lit after a completed acquisition, and none "
    "remains lit after an acquisition that failed.",
    Source.DESIGN,
    "A lamp left on heats the sensor and biases the next measurement, "
    "and after a lost link nobody can switch it off.")

_requirement(
    "HW-REQ-SENSOR-014", "An unconfirmable off-state is reported as such",
    "When the off state cannot be observed, the result says so rather "
    "than assuming it.",
    Source.DESIGN,
    "'We could not check' and 'it was off' are different findings.")

_requirement(
    "HW-REQ-SENSOR-015", "The I2C bus is electrically sane",
    "Idle-high state and clock timing are observed where an instrument "
    "is available.",
    Source.NONE,
    "A bus that works at room temperature and fails warm is an "
    "electrical problem wearing a firmware costume.")

_requirement(
    "HW-REQ-SENSOR-016", "The bus can be enumerated on demand",
    "Every address answering on the I2C bus can be listed without "
    "disturbing a working sensor.",
    Source.NONE,
    "Distinguishes 'the sensor is absent' from 'the bus is dead'.")


# ======================================================================
# B8 - integration
# ======================================================================

_requirement(
    "HW-REQ-INT-001", "The complete measurement transaction works",
    "LOAD to scanner, WHITE, UV, IR, answer, return - every stage "
    "completes, the spectrum is well formed, and the carousel returns "
    "with its position still valid.",
    Source.PROTOCOL,
    "This is what the competition runs.")

_requirement(
    "HW-REQ-INT-002", "RF-001 does not recur",
    "The half-turn transfer reports a travel matching the commanded "
    "count, the acquisition begins, and the return succeeds.",
    Source.PRODUCTION_CONFIG,
    "The exact bench failure that opened H-002.",
    assumption="H-002")

_requirement(
    "HW-REQ-INT-003", "No state leaks between measurements",
    "Each measurement names the slot requested and no two slots return "
    "identical spectral data.",
    Source.DESIGN,
    "Repeated spectra between slots is either leaked state or a "
    "carousel that did not move.")

_requirement(
    "HW-REQ-INT-004", "The integrated path does not damage data",
    "The 54 features returned through measure_raw have the same shape "
    "and channels as a standalone acquisition.",
    Source.PROTOCOL,
    "If the transaction damages data the sensor produced correctly, the "
    "fault is in the transaction.")

_requirement(
    "HW-REQ-INT-005", "Acquisition happens with the slot centred",
    "Physical centering under the head is confirmed at the moment of "
    "acquisition, per slot.",
    Source.DESIGN,
    "A spectrum taken off-centre is a measurement of the carousel.")


# ======================================================================
# B9 - recovery
# ======================================================================

_requirement(
    "HW-REQ-REC-001", "A lost link is named, not crashed on",
    "A disconnect produces a coded transport error, the client "
    "survives, and later commands fail cleanly.",
    Source.PROTOCOL,
    "39 places in the PC layer catch LinkError. A RuntimeError escapes "
    "all of them.")

_requirement(
    "HW-REQ-REC-002", "A fault lands inside the phase it targets",
    "The injected fault is proven to have occurred while the target "
    "operation was in flight; a fault before or after is inconclusive.",
    Source.DESIGN,
    "An instruction that waits for Enter BEFORE starting the operation "
    "tests a disconnect while idle, whatever its title says.")

_requirement(
    "HW-REQ-REC-003", "Position does not survive uncertainty",
    "After an interrupted movement, a reset or a power cycle, the "
    "carousel position is reported invalid and a re-sync is required.",
    Source.PROTOCOL,
    "A board that has just restarted cannot know where a mechanism is.")

_requirement(
    "HW-REQ-REC-004", "Recovery time is measured from the right moment",
    "Recovery timing starts at the physical event, not after the "
    "operator has already restored power.",
    Source.DESIGN,
    "Otherwise the number measures the operator's typing speed.")

_requirement(
    "HW-REQ-REC-005", "The module that returns is the module that left",
    "Reappearance is matched against the selected device's stable "
    "identity, not against any serial device.",
    Source.DESIGN,
    "On a bench with two USB serial devices, 'a port appeared' is not "
    "'the module came back'.")

_requirement(
    "HW-REQ-REC-006", "Science data and mechanical position fail separately",
    "When the return fails after a completed acquisition, the "
    "measurement remains retrievable AND the position is reported "
    "uncertain.",
    Source.DESIGN,
    "Discarding a completed measurement because the mechanism could not "
    "get home loses data that was already earned.")

_requirement(
    "HW-REQ-REC-007", "A failing illumination names itself",
    "An acquisition interrupted mid-triad reports which illumination "
    "failed and which completed.",
    Source.PROTOCOL,
    "Which stage failed decides whether the partial data is usable.")


# ======================================================================
# B10 and B12 - workflow and mission
# ======================================================================

_requirement(
    "HW-REQ-FLOW-001", "The shipped client is the client under test",
    "The production entry point and every screen the procedure names "
    "exist.",
    Source.DESIGN,
    "A procedure that names menu entries which no longer exist wastes "
    "an operator's bench time.")

_requirement(
    "HW-REQ-FLOW-002", "An operator can take the module from cold to ready",
    "Startup, connection and carousel setup complete, and the module "
    "afterwards reports a valid position.",
    Source.DESIGN, "This is the first thing done on the field.")

_requirement(
    "HW-REQ-FLOW-003", "Records are machine-verifiable",
    "Every measurement produced by a workflow run has a non-empty, "
    "unique id, is associated with the slot it was measured in, and "
    "carries its raw 54-feature acquisition.",
    Source.DESIGN,
    "An operator's recollection of an id is not evidence that the id "
    "exists.")

_requirement(
    "HW-REQ-FLOW-004", "Records persist across a client restart",
    "A saved measurement is still present after the client is closed "
    "and reopened.",
    Source.DESIGN,
    "The archive is the run's only irreplaceable output.")

_requirement(
    "HW-REQ-FLOW-005", "The application survives a recoverable fault",
    "After a disconnect and reconnect, navigation remains reachable and "
    "a further measurement completes, with no traceback.",
    Source.DESIGN,
    "Restarting the client on the field loses the session's context.")

_requirement(
    "HW-REQ-FLOW-006", "Protected reference data is never modified",
    "The reference libraries hash identically before and after any "
    "workflow test.",
    Source.DESIGN,
    "BD/ holds the reference libraries. A test that damages the archive "
    "costs more than the bug it was looking for.")

_requirement(
    "HW-REQ-FLOW-007", "Raw data survives an analysis failure",
    "If analysis fails, the raw acquisition remains stored and "
    "retrievable.",
    Source.DESIGN,
    "The acquisition is the expensive part; the analysis can be redone.")

_requirement(
    "HW-REQ-MISSION-001", "A mission measures every required slot",
    "The rehearsal produces exactly the configured expected sample "
    "count, against the configured slots, with unique ids and "
    "persistent records.",
    Source.DESIGN,
    "A mission with zero samples is not a mission.")

_requirement(
    "HW-REQ-MISSION-002", "A mission needs no workarounds",
    "The operator reports no workaround, and no lower-layer failure "
    "remains unresolved.",
    Source.DESIGN,
    "A workaround is a defect that has not been written down yet.")

_requirement(
    "HW-REQ-MISSION-003", "Mission duration is measured",
    "Elapsed mission time is recorded and compared with an "
    "authoritative budget where one exists.",
    Source.NONE,
    "No competition time budget is recorded in this repository. Until "
    "one is, duration is characterization.")

_requirement(
    "HW-REQ-MISSION-004", "The mission is repeatable",
    "Repeated rehearsals each satisfy the absolute requirements, and "
    "their durations are consistent.",
    Source.NONE,
    "One successful rehearsal is an anecdote.")


# ======================================================================
# B11 - endurance
# ======================================================================

_requirement(
    "HW-REQ-END-001", "Sustained operation does not degrade the link",
    "Over the qualification sample, no request fails and no corruption "
    "counter rises.",
    Source.NONE, "No authoritative reliability target exists.")

_requirement(
    "HW-REQ-END-002", "Sustained acquisition does not drift",
    "Acquisition time and spectral output are compared between the "
    "first and last decile of a long run.",
    Source.NONE, "H-007.", assumption="H-007")

_requirement(
    "HW-REQ-END-003", "Sustained movement does not degrade",
    "Closing error does not trend across a long movement run.",
    Source.NONE, "Wear, heating and a loosening coupling all look like "
                 "this.")

_requirement(
    "HW-REQ-END-004", "Rotational drift is bounded over many rotations",
    "Cumulative drift is measured against an independent reference and "
    "the rotation at which a re-sync becomes necessary is reported.",
    Source.NONE,
    "The operational answer to 'how often must this be re-synchronized'.",
    assumption="H-006")

_requirement(
    "HW-REQ-END-005", "The whole system survives sustained operation",
    "Repeated complete measurements succeed with a valid position "
    "throughout.",
    Source.NONE, "The only test that stresses everything at once.")

_requirement(
    "HW-REQ-END-006", "Partial evidence survives an interruption",
    "An endurance run stopped part way leaves every iteration it "
    "completed on disk.",
    Source.DESIGN,
    "Partial evidence is the only evidence an interrupted run can "
    "produce.")

_requirement(
    "HW-REQ-END-007", "A zero-failure run states its confidence",
    "A run with no observed failure reports a binomial upper confidence "
    "bound rather than implying a zero failure rate.",
    Source.DESIGN,
    "'0 failures in 100' is not 'the failure rate is zero'.")


# ======================================================================
# electrical, thermal and framework integrity
# ======================================================================

_requirement(
    "HW-REQ-PWR-001", "Supply rails are within their limits",
    "The regulated input, the 3V3 sensor rail and the servo supply are "
    "measured at idle, during motion, during illumination and during "
    "full-system operation.",
    Source.NONE,
    "No schematic-derived limits are recorded in this repository, so "
    "these are characterization until somebody supplies them.")

_requirement(
    "HW-REQ-PWR-002", "Voltage droop does not reset the board",
    "Droop under motion and illumination is measured and correlated "
    "with any ESP32 reset or brownout.",
    Source.NONE,
    "A brownout during a movement looks exactly like a firmware fault.")

_requirement(
    "HW-REQ-PWR-003", "Illumination draws nothing when off",
    "Leakage with all sources off is measured.",
    Source.NONE, "A lamp that is not fully off heats the sensor.")

_requirement(
    "HW-REQ-PWR-004", "Bus signals are electrically valid",
    "UART idle level, baud, framing and I2C idle state and edge timing "
    "are observed where an instrument is available.",
    Source.NONE, "Marginal signalling fails intermittently and blames "
                 "software.")

_requirement(
    "HW-REQ-THERM-001", "Component temperatures are bounded",
    "ESP32, regulator, servo driver, servo and sensor temperatures are "
    "measured after bounded endurance.",
    Source.NONE,
    "No thermal limits are recorded here; the datasheets have them and "
    "this repository does not.")

_requirement(
    "HW-REQ-FW-001", "Evidence is complete and reproducible",
    "Every run records its fingerprint, schema version, sequence "
    "numbers, monotonic timing and artifact hashes, and no field is "
    "silently discarded.",
    Source.DESIGN,
    "Evidence that loses fields is evidence nobody can re-examine.",
    verified_by=VerifiedBy.OFFLINE_SUITE)

_requirement(
    "HW-REQ-FW-002", "A prerequisite PASS belongs to the same system",
    "A layer gate opens only for a prior PASS whose fingerprint matches "
    "the current code, configuration, profile and physical unit.",
    Source.DESIGN,
    "A PASS earned on other code, another profile or another module is "
    "not evidence about this one.",
    verified_by=VerifiedBy.OFFLINE_SUITE)

_requirement(
    "HW-REQ-FW-003", "Two harness runs cannot share one module",
    "A process-level lock prevents concurrent control of the same "
    "device.",
    Source.DESIGN,
    "Two clients interleaving commands on one carousel is a mechanical "
    "hazard as well as a data hazard.",
    verified_by=VerifiedBy.OFFLINE_SUITE)

_requirement(
    "HW-REQ-FW-004", "Offline paths touch no hardware",
    "Import, help, listing, describing, capability reporting, every "
    "campaign dry run and the whole offline suite perform no serial, "
    "device or deployment operation.",
    Source.DESIGN,
    "This is the property that makes the framework safe to keep in the "
    "repository at all.",
    verified_by=VerifiedBy.OFFLINE_SUITE)

_requirement(
    "HW-REQ-FW-005", "Iteration counts are bounded by what is repeated",
    "The profile ceiling for a repeated test is resolved from the kind "
    "of iteration, not from the campaign it belongs to.",
    Source.DESIGN,
    "A servo endurance run bounded by a serial-request limit is bounded "
    "by nothing meaningful.",
    verified_by=VerifiedBy.OFFLINE_SUITE)

_requirement(
    "HW-REQ-FW-006", "A qualification claim needs its sample size",
    "A run below the declared qualification minimum produces "
    "characterization evidence, never a qualification PASS.",
    Source.DESIGN,
    "Otherwise --iterations 10 quietly passes a criterion that says "
    "100.",
    verified_by=VerifiedBy.OFFLINE_SUITE)


# ======================================================================
# the diagnostic agent
# ======================================================================

_requirement(
    "HW-REQ-DIAG-001", "The diagnostic agent is never the flight build",
    "The test-side firmware is separately identified, manually "
    "deployed, and its presence is recorded in the run fingerprint.",
    Source.DESIGN,
    "A diagnostic build that can reach a competition is worse than no "
    "diagnostic build.")

_requirement(
    "HW-REQ-DIAG-002", "The agent is read-only by default",
    "It exposes a strict command whitelist, refuses arbitrary register "
    "writes, and defaults to no movement and all lamps off.",
    Source.DESIGN,
    "A wrong write to the ST3215 memory table can change the servo id "
    "or baud and take the bus away entirely.")

_requirement(
    "HW-REQ-DIAG-003", "Production firmware is restored and verified",
    "After diagnostic use, the competition firmware is restored and its "
    "hash recorded.",
    Source.DESIGN,
    "The restoration is part of the procedure, not an afterthought.")


# ======================================================================
# lookup
# ======================================================================

def get(requirement_id):
    try:
        return REQUIREMENTS[requirement_id]

    except KeyError:
        raise KeyError(
            "no such hardware requirement: {}".format(requirement_id))


def all_requirements():
    return [REQUIREMENTS[key] for key in sorted(REQUIREMENTS)]


def unknown(ids):
    """Which of these requirement ids name nothing."""
    return [name for name in ids if name not in REQUIREMENTS]
