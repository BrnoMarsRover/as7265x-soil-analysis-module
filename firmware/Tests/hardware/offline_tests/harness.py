"""
Scaffolding for the framework's own tests.

Everything here runs on CPython with nothing plugged in. The only fake
is the transport; the registry, the adapters, the gates, the runner, the
evidence writer and the operator are all the real ones, because a test
that reimplements the thing it is testing proves only that the
reimplementation works.

EVERY CONTEXT BUILT HERE IS `Mode.SELFTEST` WITH `fake_transport=True`,
which is the only combination the framework accepts for a fake device -
and which stamps FRAMEWORK_SELFTEST on every result it produces.
"""

import io
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.configuration.profile import Profile         # noqa: E402
from hardware.core import registry as registry_module      # noqa: E402
from hardware.core.context import RunContext               # noqa: E402
from hardware.core.evidence import EvidenceWriter          # noqa: E402
from hardware.core.model import Mode                       # noqa: E402
from hardware.core.operator import Operator                # noqa: E402
from hardware.core.runner import Ledger, Runner            # noqa: E402
from hardware.offline_tests.fake_link import (             # noqa: E402
    FakeLink, healthy_script)


# ----------------------------------------------------------------------
# check counting - the same shape Tests/software uses, so a reader of
# one recognises the other
# ----------------------------------------------------------------------

class Checks:
    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = []

    def section(self, name):
        print()
        print("[{}]".format(name))

    def ok(self, condition, description):
        if condition:
            self.passed += 1
            print("  ok   {}".format(description))

        else:
            self.failed.append(description)
            print("  FAIL {}".format(description))

    def equal(self, actual, expected, description):
        if actual == expected:
            self.ok(True, description)

        else:
            self.ok(False, "{} (got {!r}, expected {!r})".format(
                description, actual, expected))

    def raises(self, exception_type, call, description):
        try:
            call()

        except exception_type:
            self.ok(True, description)

        except Exception as error:
            self.ok(False, "{} (raised {}: {})".format(
                description, type(error).__name__, error))

        else:
            self.ok(False, "{} (nothing was raised)".format(description))

    def report(self):
        print()
        print("-" * 70)
        print("{}: {} passed, {} failed".format(
            self.title, self.passed, len(self.failed)))

        for description in self.failed:
            print("   FAILED  {}".format(description))

        return 0 if not self.failed else 1


# ----------------------------------------------------------------------
# building a self-test context
# ----------------------------------------------------------------------

class Bench:
    """
    One throwaway run: a temporary evidence directory, a fake transport
    and a scripted console.

    Used as a context manager so the temporary directory is removed even
    when a framework test fails.
    """

    def __init__(self, script=None, answers=None, interactive=True,
                 profile=None, mode=Mode.SELFTEST):
        self.directory = Path(tempfile.mkdtemp(prefix="freya-hw-selftest-"))

        self.profile = profile or Profile({
            "name": "self-test bench",
            "port": "/dev/fake0",
        })

        self.evidence = EvidenceWriter(
            mode, root=self.directory, profile=self.profile,
            repo_root=HERE.parent, argv=["selftest"])

        self.input = io.StringIO(
            "".join("{}\n".format(a) for a in (answers or [])))

        self.output = io.StringIO()

        self.operator = Operator(
            self.evidence, interactive=interactive,
            stream_in=self.input, stream_out=self.output)

        self.context = RunContext(
            mode, self.profile, self.evidence, self.operator,
            hardware_confirmed=(mode == Mode.EXECUTE),
            device="/dev/fake0",
            fake_transport=(mode == Mode.SELFTEST))

        if mode == Mode.SELFTEST:
            self.link = FakeLink(
                self.context,
                script if script is not None else healthy_script())

            self.install(self.link)

        else:
            self.link = None

        self.ledger = Ledger(self.directory / "ledger.json")

    def install(self, link):
        """
        Replace the transport everywhere it is held.

        The adapters keep their own reference to the link, so a single
        assignment to `context.link` would leave the servo adapter
        talking to the real one.
        """
        self.context.link = link

        for adapter in (self.context.servo, self.context.sensor,
                        self.context.carousel, self.context.workflow):
            adapter.link = link

        self.context.adapters["link"] = link

    def runner(self, **kwargs):
        return Runner(registry_module.load(), self.context,
                      ledger=self.ledger, **kwargs)

    def run(self, test_id, **kwargs):
        """Run one registered test and return its result."""
        registry = registry_module.load()

        return self.runner(**kwargs).run([registry.get(test_id)])[0]

    def events(self):
        """Every event line written so far, parsed."""
        import json

        path = self.evidence.directory / "events.jsonl"

        if not path.is_file():
            return []

        return [json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def close(self):
        try:
            self.evidence.close()

        except Exception:                              # pragma: no cover
            pass

        shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False


def registry():
    return registry_module.load()


# ----------------------------------------------------------------------
# --help must ANSWER, not run
# ----------------------------------------------------------------------

def cli(run, doc=None, argv=None):
    """
    Run one suite from the command line, or answer --help and stop.

    THE RULE THIS EXISTS FOR is the repository's own, enforced by
    `Tests/software/entrypoints/test_entrypoints.py`: an entry point
    asked for `--help` prints usage and DOES NOTHING. Without this, the
    entrypoints suite asks each of these files for help and gets the
    entire offline campaign instead - correct, harmless, and several
    minutes of it, several times over.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    if any(argument in ("-h", "--help") for argument in argv):
        name = Path(sys.argv[0]).name

        summary = ""

        if doc:
            lines = [line for line in doc.strip().splitlines() if line]

            summary = lines[0] if lines else ""

        print("usage: {} [-h]".format(name))
        print()

        if summary:
            print(summary)
            print()

        print("Takes no options. Runs on a fake transport; no serial "
              "port is opened")
        print("and no hardware is touched.")
        print()
        print("Run every offline suite with:")
        print("    python3 run_offline.py")

        return 0

    return run()
