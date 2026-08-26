"""
The two sides of the wire, checked against each other.

THE QUESTION

The firmware produces error codes and response fields; the host handles
error codes and reads response fields. Nothing keeps the two lists in
step except somebody remembering, and the failure is silent in both
directions:

    produced but never handled    the board reports a condition and the
                                  operator gets the generic sentence
    handled but never produced    a branch written for a code the
                                  firmware cannot emit - dead code that
                                  looks like coverage
    consumed but never produced   the host reads a field the firmware
                                  does not send, and gets None

The third is the one that has already bitten this project. A.2 records
three names that did not exist, and the records screen crashing on
`match.get("rank")` because migrated records have no rank.

WHAT THIS IS NOT

It is not a linter for typos - `static/test_static_api.py` does that.
This is about the VOCABULARY: which words each side knows, and whether
the two dictionaries agree.

    py firmware/Tests/software/audit/vocabulary.py
    py firmware/Tests/software/audit/vocabulary.py --verbose
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

ESP32 = FIRMWARE / "ESP32"
PC = FIRMWARE / "PC"


# ----------------------------------------------------------------------
# codes the host handles generically, on purpose
#
# A screen that catches LinkError and prints `error.code` handles EVERY
# code by definition. These are the ones no screen names individually,
# and that is a decision rather than an oversight: the operator is shown
# the firmware's own message, which is more specific than anything a
# generic branch could say.
# ----------------------------------------------------------------------

GENERIC_BY_DESIGN = {
    # Argument validation. The firmware's message names the field and
    # the bound; a host-side branch could only repeat it.
    "INVALID_ARGUMENT",
    "MISSING_ARGUMENT",
    "INVALID_JSON",
    "COMMAND_TOO_LONG",
    "UNKNOWN_COMMAND",
    # Internal faults. There is no operator action, and the type name
    # in the message is what a developer needs.
    "INTERNAL_ERROR",
    "RESPONSE_FAILED",
    "RESPONSE_TOO_LARGE",
    "JSON_SERIALIZATION_ERROR",
}


def string_constants(node):
    """Every string literal under one AST node."""
    found = set()

    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.add(child.value)

    return found


def looks_like_a_code(text):
    """SHOUTING_SNAKE_CASE, at least two words, no spaces."""
    if not text or " " in text or len(text) < 5 or len(text) > 48:
        return False

    if not text.replace("_", "").isalnum():
        return False

    return text.isupper() and "_" in text


def docstring_nodes(tree):
    """
    Every Constant that is a docstring, so it can be excluded.

    This matters more than it sounds. Docstrings in this project QUOTE
    error codes - `serial_link.py` lists all ten of them in its module
    docstring, and the firmware explains ECHO_ONLY in a comment above
    the scan. Counting those as "produced" would make the audit agree
    with itself no matter what the code did.
    """
    found = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if not node.body:
            continue

        first = node.body[0]

        if isinstance(first, ast.Expr) and isinstance(
                first.value, ast.Constant):
            if isinstance(first.value.value, str):
                found.add(id(first.value))

    return found


def vocabulary_of(root):
    """
    Every code-like string a tree PRODUCES, however it produces it.

    THE EXTRACTOR HAS TO BE BLUNT, and the first version was not. It
    looked only at `raise SomeError("CODE", ...)` and `"code": "CODE"`,
    and so missed three whole shapes this project uses:

        code = "SERVO_UART_TIMEOUT"     a class attribute on the
                                        exception itself, in servo.py
        return LinkError("PORT_BUSY")   classified and returned, not
                                        raised, in serial_link.py
        report["result"] = "ECHO_ONLY"  a verdict, not an error at all

    All three were reported as codes the host handles but nothing can
    produce - nineteen findings, every one false. A vocabulary audit
    that cries wolf nineteen times is worse than none, so the rule is
    now simply: any SHOUTING_SNAKE_CASE literal outside a docstring is
    part of this tree's vocabulary.
    """
    words = {}

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative = path.relative_to(FIRMWARE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = docstring_nodes(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue

            if id(node) in docstrings:
                continue

            if isinstance(node.value, str) and looks_like_a_code(node.value):
                words.setdefault(node.value, set()).add(relative)

    return words


def codes_produced():
    """Every code-like word the firmware can put on the wire."""
    return vocabulary_of(ESP32)


def codes_handled():
    """
    Every code the host BRANCHES on - compares, or keys a table by.

    Narrower than the host's whole vocabulary on purpose: a word the
    host merely prints is not a word it handles, and only a word it
    handles can be a dead branch.
    """
    handled = {}

    for path in sorted(PC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative = path.relative_to(FIRMWARE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = docstring_nodes(tree)

        def record(node, where):
            for child in ast.walk(node):
                if not isinstance(child, ast.Constant):
                    continue

                if id(child) in docstrings:
                    continue

                if isinstance(child.value, str) and looks_like_a_code(
                        child.value):
                    handled.setdefault(child.value, set()).add(where)

        for node in ast.walk(tree):
            # error.code == "CODE"  /  code in ("A", "B")
            if isinstance(node, ast.Compare):
                record(node, relative)

            # A table keyed by code, as the screens use for messages.
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(
                            key.value, str):
                        if id(key) in docstrings:
                            continue

                        if looks_like_a_code(key.value):
                            handled.setdefault(
                                key.value, set()).add(relative)

    return handled


def host_codes_produced():
    """
    Every code-like word produced ANYWHERE outside the firmware.

    PC, Science and BD all have their own vocabularies - PORT_BUSY is a
    transport code, KNOWN_MATERIAL is a decision level, PROJECTED_TO_18
    is a feature-space note - and none of them travels over the wire. A
    host branch on one of these is not a dead branch.
    """
    produced = {}

    for root in (PC, FIRMWARE / "Science", FIRMWARE / "BD"):
        for word, where in vocabulary_of(root).items():
            produced.setdefault(word, set()).update(where)

    return produced


def response_fields_produced():
    """Every key the firmware puts into a response dict."""
    fields = {}

    for path in sorted(ESP32.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative = path.relative_to(FIRMWARE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue

            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(
                        key.value, str):
                    if key.value.replace("_", "").isalnum():
                        fields.setdefault(key.value, set()).add(relative)

    return fields


def response_fields_consumed():
    """Every key the host reads out of a response."""
    fields = {}

    for path in sorted(PC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative = path.relative_to(FIRMWARE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            # data.get("field")
            if isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Attribute):
                if node.func.attr != "get":
                    continue

                if node.args and isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value

                    if isinstance(value, str) and value.replace(
                            "_", "").isalnum():
                        fields.setdefault(value, set()).add(relative)

            # data["field"]
            if isinstance(node, ast.Subscript) and isinstance(
                    node.slice, ast.Constant):
                value = node.slice.value

                if isinstance(value, str) and value.replace(
                        "_", "").isalnum():
                    fields.setdefault(value, set()).add(relative)

    return fields


def report(title, entries, verbose):
    print("  {:<52} {:>4}".format(title, len(entries)))

    if verbose and entries:
        for name in sorted(entries):
            print("        {}".format(name))


def main(argv):
    verbose = "--verbose" in argv

    produced = codes_produced()
    handled = codes_handled()
    host_produced = host_codes_produced()

    all_produced = set(produced) | set(host_produced)

    print("=" * 72)
    print("  PROTOCOL VOCABULARY")
    print("=" * 72)
    print()

    report("error codes the firmware can produce", produced, verbose)
    report("error codes the host itself raises", host_produced, verbose)
    report("error codes the host names individually", handled, verbose)
    print()

    print("-" * 72)
    print("  ERROR CODES")
    print("-" * 72)

    unhandled = {
        code: where for code, where in produced.items()
        if code not in handled and code not in GENERIC_BY_DESIGN
    }

    report("produced by the firmware, never named by the host",
           unhandled, True)

    orphaned = {
        code: where for code, where in handled.items()
        if code not in all_produced
    }

    report("named by the host, never produced anywhere", orphaned, True)

    print()
    print("-" * 72)
    print("  RESPONSE FIELDS")
    print("-" * 72)

    produced_fields = response_fields_produced()
    consumed_fields = response_fields_consumed()

    missing = {
        field: where for field, where in consumed_fields.items()
        if field not in produced_fields
    }

    # The host reads plenty of keys that are its OWN - archive records,
    # calibration files, Science results - so this list is only
    # interesting for names that LOOK like protocol fields. Filtered to
    # the ones the firmware nearly produces, which is where a typo hides.
    # PROTOCOL FIELD NAMES ARE LOWERCASE, every one of them:
    # request_id, ok, cmd, data, carousel, verified. An UPPERCASE token
    # in the host is an enum VALUE from somewhere else entirely - a
    # decision level, a verification status, a sample state - and it
    # shares a namespace with nothing on the wire.
    #
    # Without that rule the audit reported `VERIFIED` colliding with
    # the servo's `verified`: a ground-truth status in the learning
    # history against a field in a servo response. Two unrelated words
    # that happen to differ by case, reported as a Linux-portability
    # hazard.
    near_misses = {}

    for field in missing:
        if field.isupper():
            continue

        for candidate in produced_fields:
            if candidate.isupper():
                continue

            if field.lower() == candidate.lower() and field != candidate:
                near_misses[field] = candidate

    report("fields consumed by the host with a case-different twin "
           "in the firmware", near_misses, True)

    if verbose:
        print()
        print("  firmware response keys: {}".format(len(produced_fields)))
        print("  host-consumed keys:     {}".format(len(consumed_fields)))

    print()
    print("-" * 72)

    failures = len(orphaned) + len(near_misses)

    if orphaned:
        print("  FAIL  the host branches on {} code(s) nothing can "
              "produce".format(len(orphaned)))

    else:
        print("  ok    every code the host names can actually be produced")

    if near_misses:
        print("  FAIL  {} field name(s) differ only by case".format(
            len(near_misses)))

    else:
        print("  ok    no response field differs from its producer only "
              "by case")

    if unhandled:
        print("  note  {} firmware code(s) are handled generically; "
              "each is shown above".format(len(unhandled)))

    print("-" * 72)
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
