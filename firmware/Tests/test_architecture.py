"""
Architecture and metric-dependency tests.

Two things are guarded here, and both are the kind that decay silently.

LAYER BOUNDARIES (see Documentation/ARCHITECTURE.md). The four-layer split is only worth having if
something enforces it. A single convenient `from Measurements import ...`
inside BD would re-entangle data with mathematics, and nothing at runtime
would complain. These tests read the import statements directly.

METRIC DEPENDENCE (see Documentation/ARCHITECTURE.md). Several popular "different" similarity
metrics are mathematically the same evidence. That fact is easy to forget
and expensive to rediscover, so it is asserted here rather than left in a
comment: if someone later adds SAM as a fourth voting family, a test
fails and explains why.
"""

import ast
import math
import sys
from pathlib import Path

import support
from support import Checks

support.add_project_root()

from BD.channels import CHANNELS            # noqa: E402
from Measurements import metrics                    # noqa: E402
from Measurements import config as science_config   # noqa: E402

REPO = support.REPO


def module_imports(path):
    """Every module name imported by one file, via the AST."""
    # utf-8-sig: some files in this tree carry a byte-order mark, and
    # ast.parse refuses one.
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import: stays inside its own package.
                continue

            if node.module:
                names.add(node.module.split(".")[0])

    return names


def layer_files(*parts):
    return sorted(
        path for path in (REPO.joinpath(*parts)).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def main_tests():
    checks = Checks("Architecture")

    # ==================================================================
    checks.section("0. all software lives under firmware/")

    # ARCHITECTURE.md put every software layer back under firmware/. The failure
    # mode this guards is a HALF-completed move: a stale copy left at the
    # repository root that still imports, so tests pass while the operator
    # edits one tree and runs the other.
    repository_root = REPO.parent

    checks.equal(
        REPO.name, "firmware",
        "the layer root is firmware/",
    )

    for layer in ("ESP32", "PC", "BD", "Measurements", "DecisionModel",
                  "Science", "Tests", "research"):
        checks.ok(
            (REPO / layer).is_dir(),
            "firmware/{} exists".format(layer),
        )

    stale = [
        layer for layer in
        ("ESP32", "PC", "BD", "Measurements", "DecisionModel", "Science",
         "Tests", "research")
        if (repository_root / layer).exists()
    ]
    checks.equal(
        stale, [],
        "no stale copy of any layer is left at the repository root",
    )

    # Hardware/ and Documentation/ deliberately stay at the root.
    checks.ok(
        (repository_root / "Hardware").is_dir(),
        "Hardware/ remains at the repository root, untouched",
    )

    # ==================================================================
    checks.section("1. ESP32 depends on nothing but itself")

    esp32_files = layer_files("ESP32")

    # boot, config, main + drivers/ + control/ + protocol/, each with its
    # package marker. The exact count matters less than the fact that a
    # new file has to be a deliberate addition to a named layer.
    checks.ok(
        len(esp32_files) >= 15,
        "the device runtime is a layered tree, not a flat directory",
    )

    forbidden = {"BD", "PC", "Measurements", "DecisionModel", "Science",
                 "Tests"}
    offenders = {}

    for path in esp32_files:
        bad = module_imports(path) & forbidden

        if bad:
            offenders[path.name] = sorted(bad)

    checks.equal(
        offenders, {},
        "no ESP32 module imports a host layer",
    )

    # The device also has no filesystem science data to load.
    for path in esp32_files:
        text = path.read_text(encoding="utf-8")
        checks.ok(
            "database.json" not in text and "references.json" not in text,
            "{} does not reference a science data file".format(path.name),
        )

    # ==================================================================
    checks.section("2. BD must never import Measurements")

    # This is THE edge that would undo the refactor: persistence reaching
    # for mathematics. Measurements -> BD is allowed and used; the reverse
    # is not.
    bd_offenders = {}

    for path in layer_files("BD"):
        bad = module_imports(path) & {
            "Measurements", "PC", "DecisionModel", "Science",
        }

        if bad:
            bd_offenders[str(path.relative_to(REPO))] = sorted(bad)

    checks.equal(
        bd_offenders, {},
        "no BD module imports Measurements, DecisionModel, Science or PC",
    )

    # ==================================================================
    checks.section("2b. Measurements must never import DecisionModel")

    # The other edge that would undo the split: mathematics reaching for
    # interpretation. Measurements produces an EvidencePackage and has no
    # opinion about what it means; DecisionModel -> Measurements is
    # allowed and used, and the reverse would put a learned judgement
    # inside a deterministic calculation.
    measurement_offenders = {}

    for path in layer_files("Measurements"):
        bad = module_imports(path) & {"DecisionModel", "PC", "Science"}

        if bad:
            measurement_offenders[str(path.relative_to(REPO))] = sorted(bad)

    checks.equal(
        measurement_offenders, {},
        "no Measurements module imports DecisionModel or PC",
    )

    # And it reaches no semantic conclusion, which is the same rule
    # expressed in vocabulary rather than in imports.
    verdict_words = ("KNOWN_MATERIAL", "AMBIGUOUS_SET", "best_match",
                     "automatic_conclusion")
    verdict_offenders = {}

    for path in layer_files("Measurements"):
        if path.name == "analysis.py":
            # The pre-split single-database path, kept working for the
            # legacy DB1 workflow until its callers move over.
            continue

        text = path.read_text(encoding="utf-8")
        found = [word for word in verdict_words if word in text]

        if found:
            verdict_offenders[path.name] = found

    checks.equal(
        verdict_offenders, {},
        "and none of them names a decision level or a best match",
    )

    # ==================================================================
    checks.section("2bb. Science sits on top; nothing below may see it")

    # Science knows there is a competition, a hypothesis and a deadline.
    # BD, Measurements and DecisionModel must not, or the mathematics
    # would stop being testable without a mission - and worse, a
    # competition deadline could reach into a scientific result.
    science_offenders = {}

    for path in layer_files("Science"):
        bad = module_imports(path) & {"PC", "ESP32"}

        if bad:
            science_offenders[str(path.relative_to(REPO))] = sorted(bad)

    checks.equal(
        science_offenders, {},
        "no Science module imports PC or ESP32",
    )

    upward = {}

    for layer in ("BD", "Measurements", "DecisionModel"):
        for path in layer_files(layer):
            if "Science" in module_imports(path):
                upward[str(path.relative_to(REPO))] = layer

    checks.equal(
        upward, {},
        "no BD, Measurements or DecisionModel module imports Science",
    )

    # The mission layer must not write a reference database either. Only
    # the run record and generated output are its to write.
    for path in layer_files("Science"):
        text = path.read_text(encoding="utf-8")

        for forbidden_call in ("DB1_FILE", "DB2_FILE", "DB1.json",
                               "DB2.json"):
            checks.ok(
                forbidden_call not in text,
                "Science/{} never names {}".format(
                    path.name, forbidden_call
                ),
            )

    # ==================================================================
    checks.section("2c. DecisionModel may read the databases, never write")

    decision_files = layer_files("DecisionModel")

    checks.ok(
        len(decision_files) >= 12,
        "the decision layer is a package, not one module",
    )

    hardware_offenders = {}

    for path in decision_files:
        bad = module_imports(path) & {"serial", "machine", "esp32_link"}

        if bad:
            hardware_offenders[path.name] = sorted(bad)

    checks.equal(
        hardware_offenders, {},
        "no DecisionModel module reaches for hardware",
    )

    # The rule the whole architecture exists to protect: learning must
    # never edit a measured reference.
    for path in decision_files:
        text = path.read_text(encoding="utf-8")

        for forbidden_call in ("_write_json(config.DB1", "DB1_FILE",
                               "DB2_FILE", "DB3_FILE", "REFERENCES_FILE"):
            checks.ok(
                forbidden_call not in text,
                "{} never touches {}".format(path.name, forbidden_call),
            )

    # ==================================================================
    checks.section("3. Measurements touches no hardware and no UI")

    hardware = {"serial", "machine", "esp32_link", "PC"}
    science_offenders = {}

    for path in layer_files("Measurements"):
        bad = module_imports(path) & hardware

        if bad:
            science_offenders[str(path.relative_to(REPO))] = sorted(bad)

    checks.equal(
        science_offenders, {},
        "no Measurements module reaches for a serial port, I2C or the UI",
    )

    # ==================================================================
    checks.section("4. PC owns no persistence implementation")

    pc_text = "\n".join(
        path.read_text(encoding="utf-8") for path in layer_files("PC")
    )

    # json.dump( writes to a file; json.dumps( returns a string and is
    # legitimate here — the serial protocol is JSON text.
    checks.ok(
        "os.replace" not in pc_text,
        "PC performs no atomic file replacement of its own",
    )
    checks.ok(
        "json.dump(" not in pc_text,
        "PC writes no scientific file itself; it calls BD repositories",
    )

    # ==================================================================
    checks.section("5. the host mirrors the device sensor settings")

    sys.path.insert(0, str(support.ESP32_DIR))
    import config as esp32_config  # noqa: E402

    checks.equal(
        science_config.EXPECTED_MEASUREMENT_MODE,
        esp32_config.SENSOR_MEASUREMENT_MODE,
        "expected measurement mode matches ESP32/config.py",
    )
    checks.equal(
        science_config.EXPECTED_INTEGRATION_CYCLES,
        esp32_config.SENSOR_INTEGRATION_CYCLES,
        "expected integration cycles match ESP32/config.py",
    )
    checks.equal(
        science_config.EXPECTED_GAIN,
        esp32_config.SENSOR_GAIN,
        "expected gain matches ESP32/config.py",
    )
    checks.equal(
        esp32_config.SENSOR_MEASUREMENT_MODE, 0b11,
        "the device is configured for Mode 3 one-shot acquisition",
    )

    # ==================================================================
    checks.section("6. metric dependence: cosine and SAM are one family")

    vector = {
        channel: float(index + 1) + 0.5 * (index % 3)
        for index, channel in enumerate(CHANNELS)
    }
    library = {
        "scaled_quarter": {c: v * 0.25 for c, v in vector.items()},
        "scaled_triple": {c: v * 3.0 for c, v in vector.items()},
        "reversed": {
            c: float(len(CHANNELS) - i)
            for i, c in enumerate(CHANNELS)
        },
        "offset": {c: v + 4.0 for c, v in vector.items()},
        "noisy": {
            c: v * (1.0 + 0.07 * ((i % 5) - 2))
            for i, (c, v) in enumerate(vector.items())
        },
    }

    by_cosine = sorted(
        library,
        key=lambda name: -metrics.cosine_similarity_percent(
            vector, library[name]
        ),
    )
    by_sam = sorted(
        library,
        key=lambda name: metrics.spectral_angle_degrees(
            vector, library[name]
        ),
    )

    checks.equal(
        by_cosine, by_sam,
        "SAM = arccos(cosine), so it produces an IDENTICAL ranking",
    )

    # ==================================================================
    checks.section("7. metric dependence: RMSE and Euclidean are one family")

    def euclidean(a, b):
        return math.sqrt(sum((a[c] - b[c]) ** 2 for c in CHANNELS))

    by_rmse = sorted(
        library, key=lambda name: metrics.rmse(vector, library[name])
    )
    by_euclid = sorted(
        library, key=lambda name: euclidean(vector, library[name])
    )

    checks.equal(
        by_rmse, by_euclid,
        "RMSE = Euclidean / sqrt(18), so it produces an IDENTICAL ranking",
    )

    # ==================================================================
    checks.section("8. what each family can and cannot see")

    dim = {c: v * 0.25 for c, v in vector.items()}

    checks.close(
        metrics.cosine_similarity_percent(vector, dim), 100.0,
        "cosine is blind to a pure brightness change",
        tolerance=1e-6,
    )
    checks.close(
        metrics.pearson_r(vector, dim), 1.0,
        "Pearson is ALSO blind to a pure brightness change",
        tolerance=1e-9,
    )
    checks.ok(
        metrics.rmse(vector, dim) > 0.0,
        "only the magnitude family sees it",
    )

    offset = {c: v + 5.0 for c, v in vector.items()}

    checks.close(
        metrics.pearson_r(vector, offset), 1.0,
        "Pearson is additionally blind to a constant offset",
        tolerance=1e-9,
    )
    checks.ok(
        metrics.cosine_similarity_percent(vector, offset) < 100.0,
        "cosine is not blind to an offset, so the two differ",
    )

    # ==================================================================
    checks.section("9. one family, one vote")

    checks.equal(
        sorted(metrics.FAMILIES),
        ["angular", "centered_shape", "magnitude"],
        "exactly three evidence families vote",
    )
    checks.equal(
        len(metrics.FAMILIES), len(science_config.FAMILY_WEIGHTS),
        "every voting family has exactly one weight",
    )

    entries = metrics.compare_all(vector, library)

    rank_keys = [
        key for key in entries[0]
        if key.endswith("_rank") and key != "combined_rank"
    ]
    family_rank_keys = {
        "{}_rank".format(family) for family in metrics.FAMILIES
    }

    checks.ok(
        family_rank_keys.issubset(set(rank_keys)),
        "each family contributes one rank",
    )

    # The angular family reports BOTH cosine and spectral angle, but the
    # spectral angle must not appear as a separate voting rank.
    checks.ok(
        "spectral_angle_deg" in entries[0],
        "the spectral angle is still reported for readability",
    )
    checks.ok(
        "spectral_angle_deg_rank" not in entries[0],
        "but it does NOT get a rank of its own - that would double-count "
        "the angular family",
    )
    checks.ok(
        "mae" in entries[0] and "mae_rank" not in entries[0],
        "same for MAE inside the magnitude family",
    )

    # ==================================================================
    checks.section("10. rank aggregation is order-independent")

    agreement = metrics.family_agreement(entries)

    checks.ok(
        agreement["family_best"],
        "family agreement names the winner of each family",
    )

    # family_agreement used to resolve ties with min() over a list already
    # sorted by combined rank, which quietly biased every tie toward the
    # combined winner. Shuffling the input must not change the verdict.
    shuffled = list(reversed(entries))
    checks.equal(
        metrics.family_agreement(shuffled)["family_best"],
        agreement["family_best"],
        "the verdict does not depend on the order of the candidate list",
    )

    checks.equal(
        agreement["weights_status"], "PROVISIONAL_UNVALIDATED",
        "the ensemble weights are labelled as unvalidated, not implied "
        "to be tuned",
    )

    # ==================================================================
    checks.section("11. the ESP32 firmware is layered, one way only")

    # The tree exists to make one thing structurally true: hardware
    # drivers, subsystem control and the wire protocol are separate, and
    # they depend on each other in ONE direction.
    #
    #     main  ->  protocol  ->  control  ->  drivers
    #
    # A driver reaching back into the carousel would stop being reusable
    # and would make swapping actuators a non-local change. Nothing at
    # runtime would complain, which is why it is asserted here.
    esp32 = support.ESP32_DIR

    for directory in ("drivers", "control", "protocol"):
        checks.ok(
            (esp32 / directory).is_dir(),
            "firmware/ESP32/{}/ exists".format(directory),
        )
        checks.ok(
            (esp32 / directory / "__init__.py").is_file(),
            "and is a real package, so MicroPython can import it",
        )

    # The ESP32 root holds application entry points only.
    root_modules = sorted(
        path.name for path in esp32.glob("*.py")
    )

    checks.equal(
        root_modules, ["boot.py", "config.py", "main.py"],
        "the ESP32 root holds only boot, config and main",
    )

    # Every device has exactly one home.
    for name in ("as7265x.py", "st3215.py",
                 "st3215_registers.py", "servo_base.py"):
        checks.ok(
            (esp32 / "drivers" / name).is_file(),
            "drivers/{} is the one home for that device".format(name),
        )

    for name in ("carousel.py", "servo_manager.py"):
        checks.ok(
            (esp32 / "control" / name).is_file(),
            "control/{} holds subsystem logic".format(name),
        )

    # No duplicate authoritative copy survives the move.
    for stale in ("as7265x.py", "mg995.py", "st3215.py", "carousel.py"):
        checks.ok(
            not (esp32 / stale).exists(),
            "no stale copy of {} is left at the ESP32 root".format(stale),
        )

    # Dependency direction, read from the imports themselves.
    layer_of = {}

    for directory in ("drivers", "control", "protocol"):
        for path in (esp32 / directory).glob("*.py"):
            layer_of[path] = directory

    rank = {"drivers": 0, "control": 1, "protocol": 2}
    offenders = []

    for path, layer in sorted(layer_of.items()):
        for imported in module_imports(path):
            if imported not in rank:
                continue

            if rank[imported] > rank[layer]:
                offenders.append("{}/{} imports {}".format(
                    layer, path.name, imported
                ))

    checks.equal(
        offenders, [],
        "no module imports a layer above itself",
    )

    # And main.py is the only place allowed to know about all of them.
    main_imports = module_imports(esp32 / "main.py")

    checks.ok(
        {"control", "protocol"} <= main_imports,
        "main.py wires control and protocol together",
    )

    main_source = (esp32 / "main.py").read_text(encoding="utf-8")

    checks.ok(
        len(main_source.splitlines()) < 400,
        "and main.py stays small enough to read in one sitting",
    )

    # The things main.py must NOT contain any more. Each of these used to
    # live in it, and each is now behind a layer boundary.
    for token, owner in (
        ("duty_ns", "a PWM backend this firmware no longer has"),
        ("0xFF", "the ST3215 frame builder"),
        ("checksum", "the ST3215 frame builder"),
        ("REG_", "the ST3215 register map"),
        ("json.dumps", "protocol/transport.py"),
        ("sys.stdin.", "protocol/transport.py"),
        ("scan_slot_for_load", "the carousel"),
    ):
        checks.ok(
            token not in main_source,
            "main.py contains no '{}' - that belongs to {}".format(
                token, owner
            ),
        )

    # ==================================================================
    checks.section("12. one actuator, and it is fully declared")

    # The MG995 backend was removed. What must hold now is that the
    # removal was complete rather than merely disconnected: no driver
    # file, no config block, no importable module - and that the one
    # remaining actuator still declares everything the carousel may ask
    # of it.
    sys.path.insert(0, str(esp32))

    support.purge_esp32_modules()

    import config as esp32_config          # noqa: E402
    from control import servo_manager      # noqa: E402
    from drivers import servo_base         # noqa: E402
    from drivers import st3215             # noqa: E402

    checks.equal(
        servo_manager.SERVO_TYPE, "st3215",
        "the firmware drives exactly one actuator",
    )

    try:
        from drivers import mg995          # noqa: F401,E402

        removed = False

    except ImportError:
        removed = True

    checks.ok(removed, "the removed backend cannot be imported")
    checks.ok(
        not (esp32 / "drivers" / "mg995.py").exists(),
        "and its driver file is gone, not merely unreferenced",
    )

    # An abstract base with one implementation is not an abstraction, so
    # it was removed too. The driver must stand on its own.
    checks.ok(
        not hasattr(servo_base, "ServoBackend"),
        "no single-implementation base class survives",
    )

    missing = [
        name for name in (
            "initialize", "deinitialize", "capabilities", "move_slots",
            "move_degrees", "half_turn", "stop", "capture_origin",
            "travel_since_origin_deg", "status", "diagnostics",
            "calibration", "test_move_kinds", "test_move",
            "slot_step_deg", "half_turn_deg", "require_ready",
        )
        if not callable(getattr(st3215.ST3215, name, None))
    ]

    checks.equal(
        missing, [],
        "ST3215 implements every method the carousel may call, including "
        "the ones it used to inherit",
    )

    st3215_capabilities = st3215.ST3215.capabilities(
        st3215.ST3215.__new__(st3215.ST3215)
    )

    checks.equal(
        sorted(st3215_capabilities.keys()),
        sorted(servo_base.CAPABILITY_KEYS),
        "and declares every capability key, none missing",
    )

    # Namespaced configuration: no generic SERVO_* name can be ambiguous
    # between two actuators.
    generic = sorted(
        name for name in dir(esp32_config) if name.startswith("SERVO_")
    )

    checks.equal(
        generic, [], "no generic SERVO_* constant survives in config.py"
    )

    st3215_names = [
        name for name in dir(esp32_config) if name.startswith("ST3215_")
    ]
    mg995_names = [
        name for name in dir(esp32_config) if name.startswith("MG995_")
    ]

    checks.ok(
        len(st3215_names) >= 15, "the ST3215 has its own config section"
    )
    checks.equal(
        mg995_names, [],
        "and no MG995 timing constant survives - the removal reached "
        "config.py, not just the driver",
    )

    # The firmware may not invent authority over servo power. The ST3215
    # is externally powered and this PCB has no switch for it.
    invented_power = (
        "SERVO_POWER_PIN", "SERVO_POWER_ENABLE", "SERVO_SUPPLY_CONTROL",
        "servo_power_enabled", "+5V_SERVO", "ST3215_POWER", "MG995_POWER",
    )

    offenders = []

    for path in layer_files("ESP32"):
        text = path.read_text(encoding="utf-8")

        for token in invented_power:
            if token in text:
                offenders.append("{}: {}".format(path.name, token))

    checks.equal(
        offenders, [],
        "no firmware module claims authority over servo power",
    )

    # The servo link and the host link must stay distinct peripherals.
    checks.ok(
        "UART(" not in main_source,
        "only the ST3215 driver creates a UART",
    )

    servo_source = (esp32 / "drivers" / "st3215.py").read_text(
        encoding="utf-8"
    )

    checks.ok(
        "sys.stdin" not in servo_source and "sys.stdout" not in servo_source,
        "and the servo driver never touches the USB console streams",
    )

    transport_source = (esp32 / "protocol" / "transport.py").read_text(
        encoding="utf-8"
    )

    checks.ok(
        "UART(" not in transport_source,
        "while the host transport never opens a UART",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
