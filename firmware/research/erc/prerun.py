"""
Pre-run science check, and the configuration lock.

Run this before the rover enters the yard. It answers one question for
each subsystem the science depends on: is this ready, is it degraded, or
is it broken — and if it is broken, which ERC requirement suffers.

Three outcomes, and no fourth:

    PASS   ready
    WARN   usable, with a stated consequence
    FAIL   not usable; the report will be missing something specific

There is deliberately no "OK probably". A check that cannot determine its
answer reports FAIL or WARN with the reason, never silence. Silence
before a competition run is the failure mode this module exists to
prevent.

The hardware checks degrade honestly: with no serial link supplied they
report WARN "not verified on hardware", never PASS.

Layer rule: Science may import BD, Science and Science.decision.
"""

from research.erc import config

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

OUTCOMES = (PASS, WARN, FAIL)


class Check:
    """One readiness check and its consequence."""

    def __init__(self, name, outcome, detail, requirements=None,
                 blocks_run=False, remedy=None):
        self.name = name
        self.outcome = outcome
        self.detail = detail
        self.requirements = list(requirements or [])
        self.blocks_run = blocks_run
        self.remedy = remedy

    def as_dict(self):
        return {
            "check": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "affected_requirements": self.requirements,
            "blocks_run": self.blocks_run,
            "remedy": self.remedy,
        }

    def __repr__(self):
        return "<{} {}>".format(self.outcome, self.name)


class PreRunReport:
    """Every check, and whether the run may proceed."""

    def __init__(self, checks, at=None):
        self.checks = list(checks)
        self.at = at

    def __iter__(self):
        return iter(self.checks)

    def of(self, outcome):
        return [c for c in self.checks if c.outcome == outcome]

    @property
    def failures(self):
        return self.of(FAIL)

    @property
    def warnings(self):
        return self.of(WARN)

    @property
    def blocking(self):
        return [c for c in self.checks if c.blocks_run and c.outcome == FAIL]

    @property
    def may_proceed(self):
        """
        A run may proceed with warnings, and with non-blocking failures.

        Non-blocking failures are real - a missing photograph subsystem
        costs O/SCI-110 points - but they are the team's call, not the
        software's. Only things that would make the science meaningless
        block.
        """
        return not self.blocking

    def affected_requirements(self):
        affected = {}

        for check in self.checks:
            if check.outcome == PASS:
                continue

            for requirement_id in check.requirements:
                affected.setdefault(requirement_id, []).append({
                    "check": check.name,
                    "outcome": check.outcome,
                    "detail": check.detail,
                })

        return affected

    def as_dict(self):
        return {
            "at": self.at,
            "checks": [c.as_dict() for c in self.checks],
            "counts": {
                PASS: len(self.of(PASS)),
                WARN: len(self.of(WARN)),
                FAIL: len(self.of(FAIL)),
            },
            "may_proceed": self.may_proceed,
            "blocking": [c.name for c in self.blocking],
            "affected_requirements": self.affected_requirements(),
        }

    def render(self):
        """One line per check, for the operator's screen."""
        lines = ["PRE-RUN SCIENCE CHECK", "=" * 68]

        for check in self.checks:
            lines.append(
                "{:<5} {:<34} {}".format(
                    check.outcome, check.name, check.detail
                )
            )

            if check.outcome != PASS and check.remedy:
                lines.append("      -> {}".format(check.remedy))

        lines.append("=" * 68)
        lines.append(
            "{} pass, {} warn, {} fail".format(
                len(self.of(PASS)), len(self.of(WARN)), len(self.of(FAIL))
            )
        )

        if self.blocking:
            lines.append(
                "RUN BLOCKED by: {}".format(
                    ", ".join(c.name for c in self.blocking)
                )
            )

        else:
            lines.append("Run may proceed.")

        return "\n".join(lines)


def _check_plan(plan, checks):
    if plan is None:
        checks.append(Check(
            "science plan", FAIL,
            "no science plan is loaded",
            ["O/SCI-060", "O/SCI-070", "O/SCI-080"],
            blocks_run=True,
            remedy="author Science/data/science_plan.json",
        ))

        return

    problems = plan.validate()

    if problems:
        checks.append(Check(
            "science plan", FAIL,
            "{} structural problem(s): {}".format(
                len(problems), "; ".join(problems[:3])
            ),
            ["O/SCI-060", "O/SCI-070", "O/SCI-080", "O/SCI-090",
             "O/SCI-100"],
            remedy="run plan.validate() for the full list",
        ))

    else:
        checks.append(Check(
            "science plan", PASS,
            "valid, {} mapped features, {} predictions".format(
                len(plan.features), len(plan.predictions)
            ),
        ))

    if plan.hypothesis is None:
        checks.append(Check(
            "hypothesis", FAIL, "the plan states no hypothesis",
            ["O/SCI-070"], blocks_run=True,
        ))

    elif not plan.hypothesis.frozen:
        checks.append(Check(
            "hypothesis frozen", FAIL,
            "hypothesis {} is not frozen".format(
                plan.hypothesis.hypothesis_id
            ),
            ["O/SCI-070", "O/SCI-120"],
            blocks_run=True,
            remedy=(
                "freeze it before the traverse: an editable hypothesis "
                "cannot be tested against what was claimed beforehand"
            ),
        ))

    else:
        unchanged, detail = plan.hypothesis.verify_unchanged()

        checks.append(Check(
            "hypothesis frozen",
            PASS if unchanged else FAIL,
            detail,
            ["O/SCI-070", "O/SCI-120"],
            blocks_run=not unchanged,
        ))

    review = plan.review_problems()

    if review:
        checks.append(Check(
            "geology reviewed", WARN,
            review[0],
            ["O/SCI-010", "O/SCI-020", "O/SCI-030"],
            remedy=(
                "a judge scores a DRAFT map as a real one; have a "
                "geologist review the units before submission"
            ),
        ))

    else:
        checks.append(Check(
            "geology reviewed", PASS,
            "all mapped features reviewed",
        ))


def _check_sites(site_plan, yard, plan, checks):
    if site_plan is None:
        checks.append(Check(
            "planned sites", FAIL, "no site plan is loaded",
            ["O/SCI-110", "O/SCI-120"], blocks_run=True,
        ))

        return

    problems = site_plan.validate(yard, plan)

    if len(site_plan) != config.PLANNED_SITE_COUNT:
        checks.append(Check(
            "planned sites", FAIL,
            "{} sites planned, {} required".format(
                len(site_plan), config.PLANNED_SITE_COUNT
            ),
            ["O/SCI-110"], blocks_run=True,
        ))

    elif problems:
        checks.append(Check(
            "planned sites", FAIL,
            "{} problem(s): {}".format(
                len(problems), "; ".join(problems[:3])
            ),
            ["O/SCI-110", "O/SCI-120"],
        ))

    else:
        checks.append(Check(
            "planned sites", PASS,
            "{} sites, all source-grounded, roles {}".format(
                len(site_plan),
                "/".join(sorted({s.role for s in site_plan})),
            ),
        ))


def _check_databases(registry, checks):
    if registry is None:
        checks.append(Check(
            "databases", FAIL, "the database registry did not load",
            ["O/SCI-150"], blocks_run=True,
        ))

        return

    for key in ("DB1", "DB2", "DB3"):
        handle = registry.get(key)

        if handle is None:
            checks.append(Check(
                "{} available".format(key), FAIL,
                "not defined in the registry", ["O/SCI-150"],
            ))
            continue

        if handle.ready:
            checks.append(Check(
                "{} available".format(key), PASS,
                "{} materials, {}, {}".format(
                    handle.count(), handle.feature_space, handle.evidence
                ),
            ))

        elif key == "DB2":
            # DB2 being empty is a known, documented state, not a
            # surprise. It costs the 54-feature comparison and nothing
            # else, so it warns rather than fails.
            checks.append(Check(
                "DB2 available", WARN,
                "{}: {}".format(
                    handle.status,
                    (handle.problems or ["no reason recorded"])[0][:90],
                ),
                ["O/SCI-150"],
                remedy=(
                    "all comparisons will run in the 18-band space only"
                ),
            ))

        else:
            checks.append(Check(
                "{} available".format(key), FAIL,
                "{}: {}".format(
                    handle.status,
                    (handle.problems or ["no reason recorded"])[0][:90],
                ),
                ["O/SCI-150"],
            ))


def _check_calibration(calibration_store, checks):
    if calibration_store is None:
        checks.append(Check(
            "calibration", WARN,
            "no calibration store supplied to the check",
            ["O/SCI-150"],
            remedy="pass the store so calibration validity is verified",
        ))

        return

    try:
        active = calibration_store.active()

    except Exception as error:                      # noqa: BLE001
        checks.append(Check(
            "calibration", FAIL,
            "the calibration store could not be read: {}".format(error),
            ["O/SCI-150"], blocks_run=True,
        ))

        return

    if active is None:
        checks.append(Check(
            "calibration", FAIL,
            "no calibration is active",
            ["O/SCI-150"],
            blocks_run=True,
            remedy=(
                "Tools -> Sensor Test -> Full Spectral Calibration. "
                "Without one, reflectance cannot be computed and the "
                "instrument evidence for O/SCI-150 does not exist."
            ),
        ))

    else:
        checks.append(Check(
            "calibration", PASS,
            "active calibration {}".format(
                getattr(active, "calibration_id", active)
            ),
        ))


def _check_storage(checks):
    """Can we write the run record at all?"""
    try:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.OUTPUT_DIR / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()

        checks.append(Check(
            "storage writable", PASS,
            "output directory is writable",
        ))

    except OSError as error:
        checks.append(Check(
            "storage writable", FAIL,
            "cannot write to {}: {}".format(config.OUTPUT_DIR, error),
            ["O/SCI-110", "O/SCI-920"],
            blocks_run=True,
        ))


def _check_clock(checks):
    """
    Timestamps have to be usable, because O/SCI-920 is arithmetic on them.
    """
    from datetime import datetime, timezone

    try:
        now = datetime.now(timezone.utc)

        if now.tzinfo is None:
            checks.append(Check(
                "clock", WARN,
                "the system clock reports no timezone",
                ["O/SCI-920"],
            ))

        else:
            checks.append(Check(
                "clock", PASS,
                "UTC timestamps available ({})".format(now.isoformat()[:19]),
            ))

    except Exception as error:                      # noqa: BLE001
        checks.append(Check(
            "clock", FAIL,
            "the clock is unusable: {}".format(error),
            ["O/SCI-920"], blocks_run=True,
        ))


def run_check(plan=None, site_plan=None, yard=None, registry=None,
              calibration_store=None, sample_store=None,
              requirement_registry=None, sensor_link=None, at=None):
    """
    Everything the science depends on, checked once.

    Every argument is optional so the check can run in any state of
    readiness; a missing subsystem produces a stated WARN or FAIL rather
    than an exception or, worse, a silent skip.
    """
    checks = []

    # --- the map ----------------------------------------------------
    if yard is None:
        checks.append(Check(
            "Mars Yard map", FAIL, "the spatial model did not load",
            ["O/SCI-110", "O/SCI-140"], blocks_run=True,
        ))

    else:
        problems = yard.validate()

        if problems:
            checks.append(Check(
                "Mars Yard map", FAIL,
                "; ".join(problems[:2]),
                ["O/SCI-110", "O/SCI-140"],
            ))

        else:
            counts = yard.counts()
            checks.append(Check(
                "Mars Yard map", PASS,
                "{} surveyed objects ({} landmarks, {} waypoints), frame "
                "{}".format(
                    sum(counts.values()), counts["LANDMARK"],
                    counts["NAVIGATION_WAYPOINT"],
                    yard.frame.get("frame_id"),
                ),
            ))

        if not config.MARS_YARD_IMAGE.exists():
            checks.append(Check(
                "map source image", WARN,
                "{} is not present".format(config.MARS_YARD_IMAGE.name),
                ["O/SCI-140"],
                remedy="the annotated map overlay needs the source image",
            ))

        else:
            checks.append(Check(
                "map source image", PASS,
                "{} present".format(config.MARS_YARD_IMAGE.name),
            ))

    # --- plan, hypothesis, sites ------------------------------------
    _check_plan(plan, checks)
    _check_sites(site_plan, yard, plan, checks)

    # --- science subsystems -----------------------------------------
    _check_databases(registry, checks)
    _check_calibration(calibration_store, checks)

    try:
        from Science import pipeline as evidence_module

        checks.append(Check(
            "measurement engine", PASS,
            "EvidencePackage schema v{}".format(
                evidence_module.EVIDENCE_SCHEMA_VERSION
            ),
        ))

    except Exception as error:                      # noqa: BLE001
        checks.append(Check(
            "measurement engine", FAIL,
            "Science did not import: {}".format(error),
            ["O/SCI-150"], blocks_run=True,
        ))

    try:
        from Science import decision as decision_engine

        checks.append(Check(
            "decision model", PASS,
            "{} ({})".format(
                decision_engine.MODEL_VERSION, decision_engine.MODEL_KIND
            ),
        ))

    except Exception as error:                      # noqa: BLE001
        checks.append(Check(
            "decision model", FAIL,
            "Science.decision did not import: {}".format(error),
            ["O/SCI-150"],
        ))

    # --- storage ----------------------------------------------------
    if sample_store is None:
        checks.append(Check(
            "sample store", WARN,
            "no sample store supplied to the check",
            ["O/SCI-110"],
        ))

    else:
        try:
            status = sample_store.status()

            if not status.get("ready"):
                checks.append(Check(
                    "sample store", FAIL,
                    "the archive is not ready: {}".format(
                        status.get("error") or "no reason recorded"
                    ),
                    ["O/SCI-110"],
                    blocks_run=True,
                    remedy=(
                        "a damaged archive is reported, never overwritten "
                        "- resolve it before measuring"
                    ),
                ))

            else:
                checks.append(Check(
                    "sample store", PASS,
                    "archive ready ({} samples saved)".format(
                        status.get("samples_saved", 0)
                    ),
                ))

        except Exception as error:                  # noqa: BLE001
            checks.append(Check(
                "sample store", FAIL,
                "the sample archive is unusable: {}".format(error),
                ["O/SCI-110"], blocks_run=True,
            ))

    _check_storage(checks)
    _check_clock(checks)

    # --- requirements subsystem -------------------------------------
    if requirement_registry is None:
        checks.append(Check(
            "requirements registry", WARN,
            "not supplied to the check",
        ))

    else:
        problems = requirement_registry.validate()

        checks.append(Check(
            "requirements registry",
            PASS if not problems else FAIL,
            "{} requirements loaded".format(len(requirement_registry))
            if not problems else "; ".join(problems[:2]),
        ))

    # --- hardware ---------------------------------------------------
    # No link means not verified. It never means fine.
    if sensor_link is None:
        checks.append(Check(
            "AS7265x link", WARN,
            "not verified: no serial link was supplied to this check",
            ["O/SCI-150"],
            remedy=(
                "run the check with a live link before entering the yard; "
                "O/SCI-150 depends entirely on this instrument working"
            ),
        ))

    else:
        try:
            status = sensor_link.sensor_status()

            checks.append(Check(
                "AS7265x link", PASS,
                "sensor responded: {}".format(status),
            ))

        except Exception as error:                  # noqa: BLE001
            checks.append(Check(
                "AS7265x link", FAIL,
                "the sensor did not respond: {}".format(error),
                ["O/SCI-150"],
                blocks_run=True,
                remedy=(
                    "without the instrument there is no non-camera "
                    "evidence, and O/SCI-150 is 50 of the 300 points"
                ),
            ))

    return PreRunReport(checks, at=at)


def configuration_payload(registry=None, calibration_store=None,
                          plan=None, site_plan=None, sensor_settings=None,
                          software_version=None, firmware_version=None):
    """
    What gets hashed into the O/SCI-900 configuration snapshot.

    Only software-visible facts. It cannot see a swapped bracket or a
    changed lens, and it does not pretend to: the snapshot proves what
    the software was configured as, and the operator checklist covers the
    rest.
    """
    payload = {
        "software_version": software_version,
        "firmware_version": firmware_version,
        "sensor_settings": sensor_settings,
        "expected_measurement_mode": None,
        "expected_integration_cycles": None,
        "expected_gain": None,
        "plan_id": plan.plan_id if plan else None,
        "hypothesis_hash": (
            plan.hypothesis.content_hash
            if plan and plan.hypothesis else None
        ),
        "planned_site_points": (
            sorted(site_plan.point_ids()) if site_plan else None
        ),
        "database_versions": None,
        "active_calibration_id": None,
        "decision_model_version": None,
    }

    try:
        from research.erc import config as measurement_config

        payload["expected_measurement_mode"] = (
            measurement_config.EXPECTED_MEASUREMENT_MODE
        )
        payload["expected_integration_cycles"] = (
            measurement_config.EXPECTED_INTEGRATION_CYCLES
        )
        payload["expected_gain"] = measurement_config.EXPECTED_GAIN

    except Exception:                               # noqa: BLE001
        pass

    if registry is not None:
        payload["database_versions"] = {
            key: {
                "version": handle.version,
                "status": handle.status,
                "material_count": handle.count(),
            }
            for key, handle in sorted(registry.databases.items())
        }

    if calibration_store is not None:
        try:
            active = calibration_store.active()
            payload["active_calibration_id"] = getattr(
                active, "calibration_id", None
            )

        except Exception:                           # noqa: BLE001
            payload["active_calibration_id"] = "UNREADABLE"

    try:
        from Science import decision as decision_engine

        payload["decision_model_version"] = decision_engine.MODEL_VERSION

    except Exception:                               # noqa: BLE001
        pass

    return payload
