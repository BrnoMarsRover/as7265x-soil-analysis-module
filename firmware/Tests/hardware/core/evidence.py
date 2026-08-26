"""
Where a run's evidence goes, and what it is stamped with.

ONE DIRECTORY PER RUN, NAMED BY MODE AND TIMESTAMP

    artifacts/HW-20260825-141233-EXECUTE/
    artifacts/HW-20260825-141233-DRY_RUN/

The mode is in the DIRECTORY NAME, not only in a field inside it,
because evidence gets copied, mailed and pasted into reports, and the
first thing anybody sees is the folder. A dry run cannot be mistaken for
a campaign at a glance.

WHAT IS WRITTEN

    run_manifest.json   what ran, where, on what, from which commit
    events.jsonl        one JSON object per event, appended as it happens
    measurements.csv    the numbers, for a spreadsheet or a plot
    summary.json        machine-readable result of every test
    summary.md          the same thing for a human
    operator_notes.md   what the operator observed, in their words
    defects/HW-*.md     one file per failure, with a persistent ID

`events.jsonl` is FLUSHED AFTER EVERY LINE. A campaign that is killed by
a power cut, a Ctrl+C or a kernel oops must leave behind everything that
happened up to that moment - partial evidence is the only kind an
interrupted endurance run can produce, and it is worth having.

NOTHING HERE IS COMMITTED. `artifacts/` is ignored; see the .gitignore
entry beside this tree.
"""

import csv
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .model import Evidence, Status


HARDWARE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_ARTIFACTS = HARDWARE_DIR / "artifacts"

# The five fields every event line carries, which say what kind of run
# produced it. A caller may not overwrite any of them - see `event`.
ENVELOPE = ("at", "run_id", "mode", "evidence_class", "kind")


def utc_now():
    return datetime.now(timezone.utc)


def timestamp():
    return utc_now().strftime("%Y%m%d-%H%M%S")


def iso():
    return utc_now().isoformat()


def git_revision(repo_root):
    """
    The commit the framework ran from, or an honest None.

    A result that cannot be tied to a revision cannot be reproduced, and
    "unknown" is a better answer than a fabricated one - so a missing
    git, a missing repository and a git that fails are all None with the
    reason recorded beside it.
    """
    try:
        finished = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )

    except (OSError, subprocess.SubprocessError) as error:
        return {"commit": None, "error": str(error)}

    if finished.returncode != 0:
        return {"commit": None,
                "error": finished.stderr.strip() or "git failed"}

    commit = finished.stdout.strip()

    dirty = None

    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )

        dirty = bool(status.stdout.strip())

    except (OSError, subprocess.SubprocessError):
        pass

    return {"commit": commit, "dirty": dirty}


class EvidenceWriter:
    """
    One run's evidence directory.

    Constructed with a mode, and every line it writes carries that mode.
    There is no way to write an event into a DRY_RUN directory that
    claims to be hardware evidence, because the class stamps it rather
    than the caller.
    """

    FRAMEWORK_VERSION = "1.0.0"

    def __init__(self, mode, root=None, run_id=None, profile=None,
                 repo_root=None, argv=None):
        self.mode = mode
        self.evidence_class = (
            Evidence.HARDWARE if mode == "EXECUTE"
            else Evidence.SELFTEST if mode == "SELFTEST"
            else Evidence.DRY_RUN
        )

        self.run_id = run_id or "HW-{}-{}".format(timestamp(), mode)

        root = Path(root) if root else DEFAULT_ARTIFACTS
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True, exist_ok=True)

        self.repo_root = Path(repo_root) if repo_root else HARDWARE_DIR
        self.profile = profile
        self.argv = list(argv or sys.argv)

        self._events = (self.directory / "events.jsonl").open(
            "a", encoding="utf-8")
        self._measurements = None
        self._measurement_writer = None
        self._measurement_fields = None

        self.closed = False

        self._write_manifest()

    # ------------------------------------------------------------------
    # manifest
    # ------------------------------------------------------------------

    def _write_manifest(self):
        manifest = {
            "run_id": self.run_id,
            "mode": self.mode,
            "evidence_class": self.evidence_class,
            "started_utc": iso(),
            "framework_version": self.FRAMEWORK_VERSION,
            "git": git_revision(self.repo_root),
            "argv": self.argv,
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "node": platform.node(),
                "python": sys.version.split()[0],
                "python_executable": sys.executable,
                "user": os.environ.get("USER") or os.environ.get(
                    "USERNAME") or None,
            },
            "profile": (self.profile.as_dict()
                        if self.profile is not None else None),
        }

        if self.evidence_class != Evidence.HARDWARE:
            manifest["warning"] = (
                "{} - nothing in this directory is evidence about the "
                "hardware.".format(self.evidence_class)
            )

        self.write_json("run_manifest.json", manifest)

        self.manifest = manifest

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def event(self, kind, /, **fields):
        """
        Append one event and flush it.

        Flushing every line costs a syscall per event and buys the only
        thing that matters when a run dies unexpectedly: the events that
        already happened are on disk.

        `kind` IS POSITIONAL-ONLY, and that is not decoration. Events
        carry arbitrary caller fields, and a `Check` has a field of its
        own called `kind` - AUTOMATIC or OPERATOR, which is exactly the
        distinction this framework exists to preserve. Without the `/`
        the two collide and every check raises TypeError instead of
        being recorded. Found by offline_tests/test_runner.py before any
        hardware was involved, which is what that suite is for.
        """
        if self.closed:
            raise RuntimeError("evidence writer is closed")

        record = {
            "at": iso(),
            "run_id": self.run_id,
            "mode": self.mode,
            "evidence_class": self.evidence_class,
            "kind": kind,
        }

        # THE ENVELOPE ALWAYS WINS, AND THE CALLER'S VALUE IS NEVER
        # LOST. A caller field called `kind`, `mode` or `run_id` would
        # otherwise overwrite the stamp that says what kind of run this
        # was - which is the one thing in the evidence that must not be
        # forgeable by a test body. Colliding fields are kept under a
        # `field_` prefix instead of being dropped.
        for name, value in fields.items():
            record["field_" + name if name in ENVELOPE else name] = value

        self._events.write(json.dumps(record, default=_json_safe) + "\n")
        self._events.flush()

        return record

    # ------------------------------------------------------------------
    # measurements
    # ------------------------------------------------------------------

    def measurement(self, row):
        """
        One row in measurements.csv.

        The header is fixed by the FIRST row written, and later rows are
        projected onto it with blanks for anything missing. A CSV whose
        columns change halfway down is not readable by any of the tools
        anybody will actually open it with.
        """
        if self._measurement_writer is None:
            self._measurement_fields = list(row.keys())
            self._measurements = (
                self.directory / "measurements.csv"
            ).open("a", newline="", encoding="utf-8")

            self._measurement_writer = csv.DictWriter(
                self._measurements, fieldnames=self._measurement_fields,
                extrasaction="ignore",
            )

            self._measurement_writer.writeheader()

        complete = {field: row.get(field, "")
                    for field in self._measurement_fields}

        extra = [k for k in row if k not in self._measurement_fields]

        if extra:
            # Not silently dropped: the value goes into the event log
            # where nothing is projected away.
            self.event("measurement_extra_fields",
                       fields=extra, row={k: row[k] for k in extra})

        self._measurement_writer.writerow(complete)
        self._measurements.flush()

    # ------------------------------------------------------------------
    # files
    # ------------------------------------------------------------------

    def write_json(self, name, payload):
        path = self.directory / name

        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True,
                       default=_json_safe),
            encoding="utf-8",
        )

        return path

    def write_text(self, name, text):
        path = self.directory / name

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

        return path

    def append_text(self, name, text):
        path = self.directory / name

        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

        return path

    def raw_serial(self, text):
        """Raw bytes-as-text seen on the wire, kept verbatim."""
        return self.append_text("raw_serial.log", text)

    # ------------------------------------------------------------------
    # summaries
    # ------------------------------------------------------------------

    def write_summary(self, results, aborted=False):
        summary = {
            "run_id": self.run_id,
            "mode": self.mode,
            "evidence_class": self.evidence_class,
            "finished_utc": iso(),
            "aborted": bool(aborted),
            "counts": count_statuses(results),
            "campaign_verdicts": campaign_verdicts(results),
            "results": [r.as_dict() for r in results],
        }

        if self.evidence_class != Evidence.HARDWARE:
            summary["warning"] = (
                "{} - no hardware was involved in producing "
                "this.".format(self.evidence_class))

        self.write_json("summary.json", summary)
        self.write_text("summary.md", render_markdown(summary))

        return summary

    def write_defect(self, defect):
        """One HW-xxx defect record, as its own file."""
        name = "defects/{}.md".format(defect["defect_id"])

        return self.write_text(name, render_defect(defect))

    def operator_note(self, test_id, question, answer, kind):
        self.append_text(
            "operator_notes.md",
            "## {}  -  {}\n\n"
            "- question: {}\n"
            "- answer: {}\n"
            "- kind: {}\n"
            "- recorded: {}\n\n".format(
                test_id, kind, question, answer, kind, iso()),
        )

    # ------------------------------------------------------------------

    def close(self):
        if self.closed:
            return

        try:
            self._events.close()

        except Exception:
            pass

        if self._measurements is not None:
            try:
                self._measurements.close()

            except Exception:
                pass

        self.closed = True


def _json_safe(value):
    """Anything json cannot serialize becomes its repr, never an error."""
    try:
        return repr(value)

    except Exception:                                  # pragma: no cover
        return "<unrepresentable>"


# ======================================================================
# aggregation
# ======================================================================

def count_statuses(results):
    counts = {status: 0 for status in Status.ALL}

    for result in results:
        counts[result.status] += 1

    return counts


def campaign_verdicts(results):
    """
    One verdict per campaign, and it is pessimistic on purpose.

    A campaign containing a FAIL is FAIL. A campaign with no failure but
    an unrun test is not a pass either - it is INCOMPLETE, because
    "everything we ran passed" is a different claim from "the campaign
    passed" and the second one is what people quote.
    """
    verdicts = {}

    for result in results:
        bucket = verdicts.setdefault(result.campaign, [])
        bucket.append(result)

    summary = {}

    for campaign, entries in verdicts.items():
        statuses = [e.status for e in entries]
        hardware = all(e.hardware_evidence for e in entries)

        if any(s in Status.BAD for s in statuses):
            verdict = "FAIL"

        elif any(s in Status.UNRUN for s in statuses):
            verdict = "INCOMPLETE"

        elif hardware and all(s == Status.PASS for s in statuses):
            verdict = "PASS"

        else:
            verdict = "INCOMPLETE"

        summary[campaign] = {
            "verdict": verdict,
            "tests": len(entries),
            "by_status": {
                status: statuses.count(status)
                for status in sorted(set(statuses))
            },
        }

    return summary


def render_markdown(summary):
    """The short human-readable report beside the machine-readable one."""
    lines = []

    lines.append("# Hardware run {}".format(summary["run_id"]))
    lines.append("")
    lines.append("- mode: **{}**".format(summary["mode"]))
    lines.append("- evidence: **{}**".format(summary["evidence_class"]))
    lines.append("- finished: {}".format(summary["finished_utc"]))

    if summary.get("aborted"):
        lines.append("- **ABORTED** before the selection was finished")

    if summary.get("warning"):
        lines.append("")
        lines.append("> {}".format(summary["warning"]))

    lines.append("")
    lines.append("## Campaign verdicts")
    lines.append("")
    lines.append("| Campaign | Verdict | Tests | Breakdown |")
    lines.append("| --- | --- | ---: | --- |")

    for campaign in sorted(summary["campaign_verdicts"]):
        entry = summary["campaign_verdicts"][campaign]

        lines.append("| {} | {} | {} | {} |".format(
            campaign, entry["verdict"], entry["tests"],
            ", ".join("{} {}".format(v, k)
                      for k, v in sorted(entry["by_status"].items())),
        ))

    lines.append("")
    lines.append("## Tests")
    lines.append("")
    lines.append("| Test | Status | Checks | Duration | Reason |")
    lines.append("| --- | --- | --- | ---: | --- |")

    for result in summary["results"]:
        lines.append("| {} | {} | {}/{} | {} | {} |".format(
            result["test_id"],
            result["reported_status"],
            result["checks_passed"],
            result["checks_passed"] + result["checks_failed"],
            "-" if result["duration_s"] is None
            else "{:.2f} s".format(result["duration_s"]),
            (result["reason"] or "").replace("|", "/")[:120],
        ))

    lines.append("")

    return "\n".join(lines) + "\n"


def render_defect(defect):
    """
    One failure, in the shape a defect has to have to be closable.

    Observed, expected, reproduction, logs, suspected layer, root cause,
    fix, verification, regression test, status. The last four are empty
    when the defect is raised and are filled in by whoever works on it -
    an empty "root cause" is a true statement about a fresh defect, and
    a defect record that cannot say what is still unknown is useless.
    """
    lines = [
        "# {} - {}".format(defect["defect_id"], defect["title"]),
        "",
        "| field | value |",
        "| --- | --- |",
        "| raised | {} |".format(defect.get("raised_utc", "")),
        "| run | {} |".format(defect.get("run_id", "")),
        "| test | {} |".format(defect.get("test_id", "")),
        "| campaign | {} |".format(defect.get("campaign", "")),
        "| suspected layer | {} |".format(
            defect.get("suspected_layer") or "UNKNOWN"),
        "| assumption | {} |".format(defect.get("assumption") or "-"),
        "| status | {} |".format(defect.get("status", "OPEN")),
        "",
        "## Observed",
        "",
        defect.get("observed", "") or "-",
        "",
        "## Expected",
        "",
        defect.get("expected", "") or "-",
        "",
        "## Reproduction",
        "",
    ]

    for step in defect.get("reproduction", []) or ["-"]:
        lines.append("- {}".format(step))

    lines += [
        "",
        "## Evidence",
        "",
        "```json",
        json.dumps(defect.get("evidence", {}), indent=2, sort_keys=True,
                   default=_json_safe),
        "```",
        "",
        "## Root cause",
        "",
        defect.get("root_cause", "") or "NOT YET ESTABLISHED",
        "",
        "## Fix",
        "",
        defect.get("fix", "") or "NOT YET APPLIED",
        "",
        "## Verification",
        "",
        defect.get("verification", "") or "NOT YET VERIFIED",
        "",
        "## Regression test",
        "",
        defect.get("regression_test", "") or "NOT YET WRITTEN",
        "",
    ]

    return "\n".join(lines)
