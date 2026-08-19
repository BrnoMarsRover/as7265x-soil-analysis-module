"""
Architecture: the boundaries that must hold, checked mechanically.

A layering rule stated in a comment is a wish. These are the ones that
are enforced, and each exists because breaking it has a specific
consequence:

    ESP32 doing science          a spectrum interpreted on a device
                                 that cannot see the databases
    PC duplicating Science       two implementations of one metric,
                                 disagreeing silently
    BD importing Science         storage that cannot be read without
                                 the layer that interprets it
    production importing research  an unvalidated experiment reaching a
                                 reported conclusion
    obsolete architecture surviving  MG995, DecisionModel/,
                                 Measurements/, an 8-slot carousel

Run:  py test_architecture.py
"""

import ast
import re
import sys
from pathlib import Path

import support

checks = support.Checks("architecture")

FIRMWARE = support.FIRMWARE

DOMAINS = {
    "ESP32": FIRMWARE / "ESP32",
    "PC": FIRMWARE / "PC",
    "Science": FIRMWARE / "Science",
    "BD": FIRMWARE / "BD",
    "Tests": FIRMWARE / "Tests",
    "tools": FIRMWARE / "tools",
    "research": FIRMWARE / "research",
}


def sources(domain):
    return sorted(p for p in DOMAINS[domain].rglob("*.py")
                  if "__pycache__" not in str(p))


def text_of(path):
    return path.read_text(encoding="utf-8", errors="replace")


def imports_of(path):
    try:
        tree = ast.parse(text_of(path))

    except SyntaxError:
        return set()

    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)

        elif isinstance(node, ast.ImportFrom) and node.module and \
                not node.level:
            found.add(node.module.split(".")[0])

    return found


def relative(path):
    return str(path.relative_to(FIRMWARE)).replace("\\", "/")


# ======================================================================
checks.section("the four production domains exist and nothing else does")

for name in ("ESP32", "PC", "Science", "BD"):
    checks.ok(DOMAINS[name].is_dir(), "{}/ exists".format(name))

for name in ("Tests", "tools", "research"):
    checks.ok(DOMAINS[name].is_dir(), "{}/ exists".format(name))

for gone in ("Measurements", "DecisionModel"):
    path = FIRMWARE / gone
    checks.ok(not path.exists(),
              "{}/ is gone - not emptied, not a shim, gone".format(gone))

top_level = sorted(
    p.name for p in FIRMWARE.iterdir()
    if p.is_dir() and not p.name.startswith((".", "__"))
)
checks.equal(
    top_level,
    ["BD", "ESP32", "PC", "Science", "Tests", "research", "tools"],
    "firmware/ holds exactly the seven intended directories",
)


# ======================================================================
checks.section("MG995 is completely absent")

mg995 = []

# This file is the one place the name may appear, because asserting
# that something is absent means naming it.
SELF = Path(__file__).resolve()

for domain in DOMAINS:
    for path in sources(domain):
        if path.resolve() == SELF:
            continue

        body = text_of(path)

        for number, line in enumerate(body.split("\n"), 1):
            if re.search(r"mg995", line, re.I):
                mg995.append("{}:{}".format(relative(path), number))

checks.equal(mg995, [],
             "no source file mentions MG995 in any form, any case")

for domain in ("ESP32", "PC", "Science", "BD"):
    # MODE_PWM is a real ST3215 memory-table mode and stays. What must
    # not survive is TIMED-PULSE positioning: microsecond pulse widths,
    # millisecond-per-slot calibration, and the modules that held them.
    dead = [
        relative(p) for p in sources(domain)
        if re.search(r"pulse_us|stop_us|_cw_us|_ccw_us|pulse_width|"
                     r"ms_per_degree|next_slot_cw_ms|servo_base|"
                     r"servo_manager",
                     text_of(p), re.I)
    ]
    checks.equal(dead, [],
                 "no timed-pulse positioning survives in {}".format(domain))


# ======================================================================
checks.section("the carousel is four slots at 90 degrees")

sys.path.insert(0, str(DOMAINS["ESP32"]))
support.purge_esp32_modules()

import config as esp32_config  # noqa: E402

checks.equal(esp32_config.CAROUSEL_SLOT_COUNT, 4, "4 logical slots")
checks.close(esp32_config.CAROUSEL_SLOT_GEOMETRY_DEG, 90.0,
             "90 degrees between neighbouring slots")
checks.close(esp32_config.CAROUSEL_HALF_TURN_DEG, 180.0,
             "loader and scanner are 180 degrees apart")
checks.equal(esp32_config.CAROUSEL_SCAN_LOAD_OFFSET, 2,
             "180 degrees is 2 of the 4 slots")
checks.equal(
    esp32_config.CAROUSEL_SCAN_LOAD_OFFSET * 2,
    esp32_config.CAROUSEL_SLOT_COUNT,
    "the offset is half the slot count, which makes the loader/scanner "
    "mapping its own inverse",
)
checks.equal(esp32_config.ST3215_COUNTS_PER_SLOT, 1024,
             "1024 encoder counts per slot, derived not typed")
checks.equal(esp32_config.ST3215_HALF_TURN_COUNTS, 2048,
             "2048 encoder counts per half turn, derived not typed")

obsolete = []

for domain in ("ESP32", "PC", "Science", "BD", "tools"):
    for path in sources(domain):
        for number, line in enumerate(text_of(path).split("\n"), 1):
            stripped = line.strip()

            if stripped.startswith("#") or '"""' in stripped:
                continue

            if re.search(r"range\(1,\s*9\)|%\s*8\b|\[1-8\]|slot\s*[5-8]\b",
                         stripped, re.I):
                obsolete.append("{}:{}".format(relative(path), number))

checks.equal(obsolete, [],
             "no active 8-slot assumption anywhere in production code")


# ======================================================================
checks.section("ESP32 owns hardware and nothing else")

SCIENCE_WORDS = (
    "cosine", "spectral_angle", "pearson", "similarity", "normaliz",
    "dark_correct", "reflectance", "DB1", "DB2", "DB3", "database",
    "decision", "classif", "material", "taxonomy", "calibration_id",
)

def code_only(body):
    """
    The executable lines, with comments and docstrings removed.

    A comment saying "normalization runs on the PC" is exactly the
    documentation this boundary deserves, and must not be mistaken for
    normalization running here.
    """
    try:
        tree = ast.parse(body)

    except SyntaxError:
        return body

    docstrings = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)

            if doc:
                docstrings.add(doc)

    stripped = "\n".join(
        line for line in body.split("\n")
        if not line.strip().startswith("#")
    )

    for doc in docstrings:
        stripped = stripped.replace(doc, "")

    return stripped


for path in sources("ESP32"):
    code = code_only(text_of(path))
    found = [w for w in SCIENCE_WORDS if re.search(r"\b%s" % w, code, re.I)]

    checks.equal(found, [],
                 "{} contains no scientific interpretation".format(
                     relative(path)))

for path in sources("ESP32"):
    imports = imports_of(path)
    forbidden = imports & {"BD", "Science", "PC", "serial", "json5"}

    checks.equal(sorted(forbidden), [],
                 "{} imports no PC-side layer".format(relative(path)))

esp32_files = sorted(p.name for p in DOMAINS["ESP32"].glob("*.py"))
checks.equal(
    esp32_files,
    ["boot.py", "carousel.py", "config.py", "main.py", "protocol.py",
     "sensor.py", "servo.py"],
    "the ESP32 tree is flat: seven modules, no packages",
)
checks.ok(
    not any(p.is_dir() for p in DOMAINS["ESP32"].iterdir()
            if p.name != "__pycache__"),
    "no drivers/, control/ or protocol/ package survives",
)

for path in sources("ESP32"):
    body = text_of(path)

    checks.ok(not re.search(r"(?<![\w.])input\s*\(", body),
              "{} never calls input()".format(relative(path)))


# ======================================================================
checks.section("one implementation of each driver")

for name, marker in (
    ("AS7265x", r"class AS7265X\b"),
    ("ST3215", r"class ST3215\b"),
    ("Carousel", r"class Carousel\b"),
):
    defining = [relative(p) for p in sources("ESP32")
                if re.search(marker, text_of(p))]

    checks.equal(len(defining), 1,
                 "exactly one {} implementation ({})".format(
                     name, ", ".join(defining) or "none"))


# ======================================================================
checks.section("Science owns the mathematics, alone")

METRIC_MARKERS = (
    r"def cosine\b", r"def pearson_r\b", r"def spectral_angle",
    r"def rmse\b", r"def dark_correct\b", r"def normalize\b",
    r"math\.acos", r"def mahalanobis",
)

for domain in ("PC", "BD", "ESP32"):
    for path in sources(domain):
        body = text_of(path)
        found = [m for m in METRIC_MARKERS if re.search(m, body)]

        checks.equal(found, [],
                     "{} implements no scientific formula".format(
                         relative(path)))

for path in sources("BD"):
    imports = imports_of(path)

    checks.ok("Science" not in imports,
              "{} does not import Science".format(relative(path)))
    checks.ok(not (imports & {"serial", "machine"}),
              "{} contains no hardware dependency".format(relative(path)))

for path in sources("Science"):
    imports = imports_of(path)
    forbidden = imports & {"serial", "machine", "PC", "research"}

    checks.equal(sorted(forbidden), [],
                 "{} touches no port, no device and no research code"
                 .format(relative(path)))

    checks.ok(not re.search(r"(?<![\w.])input\s*\(", text_of(path)),
              "{} asks no questions".format(relative(path)))


# ======================================================================
checks.section("production never imports research")

for domain in ("ESP32", "PC", "Science", "BD", "tools"):
    for path in sources(domain):
        imports = imports_of(path)

        checks.ok("research" not in imports,
                  "{} does not import research".format(relative(path)))

        checks.ok(
            not re.search(r"^\s*(from|import)\s+research", text_of(path),
                          re.M),
            "{} has no research import at any depth".format(relative(path)))


# ======================================================================
checks.section("production Science generates no reports")

# What must not appear is DOCUMENT generation. `channel_report` is a
# per-channel verdict and not a document, and "percent" contains "erc",
# so the markers name output formats and mission artifacts rather than
# the words "report" and "ERC" loosely.
REPORT_MARKERS = (r"\.pdf\b", r"\bdocx\b", r"\bmarkdown\b",
                  r"science.?plan", r"mars.?yard",
                  r"def write_report", r"def generate_report",
                  r"def export_report", r"def build_report")

for path in sources("Science"):
    body = text_of(path)
    found = [m for m in REPORT_MARKERS if re.search(m, body, re.I)]

    if re.search(r"\bERC\b", body):
        found.append("ERC")

    checks.equal(found, [],
                 "{} produces no document".format(relative(path)))


# ======================================================================
checks.section("no permanent compatibility architecture")

for domain in ("ESP32", "PC", "Science", "BD", "tools"):
    for path in sources(domain):
        body = text_of(path)

        checks.ok(
            not re.search(r"except ImportError:\s*\n\s*from ", body),
            "{} has no try/except import fallback".format(relative(path)))

        checks.ok(
            not re.search(r"#\s*(Historical|Legacy|Backwards?)\s+"
                          r"(name|alias|key|spelling)", body, re.I),
            "{} keeps no alias for an old name".format(relative(path)))


# ======================================================================
checks.section("the deployment manifest is the ESP32 tree")

sys.path.insert(0, str(DOMAINS["tools"]))

import device  # noqa: E402

checks.equal(
    sorted(device.ESP32_FILES),
    esp32_files,
    "the manifest lists exactly the files in ESP32/",
)
checks.ok(
    device.ESP32_DIR == DOMAINS["ESP32"],
    "the tool resolves ESP32/ from its own location, not the cwd",
)
checks.ok(
    all(name in device.ESP32_FILES for name in ("boot.py", "main.py")),
    "boot.py and main.py are deployed",
)
checks.ok(
    "main" not in device.IMPORT_CHECK,
    "the import check does not import main.py, which would start the "
    "serving loop instead of testing it",
)


# ======================================================================
checks.section("one serial owner")

serial_users = [
    relative(p) for domain in ("PC", "Science", "BD", "tools")
    for p in sources(domain)
    if re.search(r"^\s*import serial\s*$|^\s*from serial(\.|\s)",
                 text_of(p), re.M)
]

checks.equal(serial_users, ["PC/serial_link.py"],
             "exactly one module imports pyserial")

for path in sources("PC"):
    if path.name == "serial_link.py":
        continue

    checks.ok(
        not re.search(r"serial\.Serial\(", text_of(path)),
        "{} constructs no serial port of its own".format(relative(path)))


sys.exit(checks.report())
