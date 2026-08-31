"""
Who owns a Sample, and what each delete actually deletes.

THE DEFECTS THIS SUITE EXISTS FOR

    Every measurement wrote itself straight into what the Sample
    Database screen called the archive. "Import ALL Samples from ESP32"
    therefore had nothing left to do, and the operator had no way to
    say "keep this one" because the program had already decided.

    `[4] Delete sample` removed the PC record and left the device copy
    alone, and the menu said neither. An operator who deleted a sample
    and then saw it vanish from the only table on screen could not tell
    which of the three stores they had just emptied.

    `[7] Delete ALL Samples from ESP32` reported "already empty" over a
    device that had held the acquisition thirty seconds earlier,
    because `clear_slot` had silently dropped the retained measurement
    while telling the operator it was freeing the mechanism only.

Three stores, three owners:

    ESP32       the device's own RAM buffer, one acquisition per slot
    session     the PC's working set for the run in progress
    archive     the PC's permanent record

Nothing crosses between them except an operation that names both ends.
Everything below runs against the REAL firmware behind a fake wire and
the REAL screens, in a sandboxed BD.

Run:  py test_storage_ownership.py
"""

import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support                                              # noqa: E402

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402

from fakes import run_screen                                # noqa: E402
from fakes.clock import FakeClock, install_clock            # noqa: E402
from fakes.esp32 import loopback_link                       # noqa: E402
from fakes.serial_port import install_fake_serial           # noqa: E402
from fakes.storage import sandbox_mission                   # noqa: E402

from BD.samples import StorageError                         # noqa: E402

from workflow import measure as measure_screens             # noqa: E402
from workflow import records as records_screens             # noqa: E402

checks = support.Checks("storage-ownership")

restore_serial = install_fake_serial(serial_link)


# ----------------------------------------------------------------------
# one bench, built the same way every time
# ----------------------------------------------------------------------

class Bench:
    """A Mission over the real firmware, with every store sandboxed."""

    def __init__(self):
        clock = FakeClock()
        install_clock(serial_link, clock)

        self.link, self.fake, self.loop = loopback_link(serial_link)
        self.mission, self.bd = sandbox_mission(self.link)

        self.link.request("connect_servo")
        self.link.request("sync_position", load_slot=1)

    # -- what each store holds -----------------------------------------

    def esp32_ids(self):
        index = records_screens.esp32_index(self.mission)

        return sorted(index["by_id"])

    def session_ids(self):
        return sorted(self.mission.session.sample_ids())

    def archive_ids(self):
        return sorted(self.mission.archive.sample_ids())

    def learning_count(self):
        return self.mission.learning.count_observations()

    # -- doing things --------------------------------------------------

    def measure(self, sample_id, slot=1):
        """Prepare, confirm and measure, through the real screens."""
        status = self.link.get_status()
        view = self.mission.slot_view(status)

        run_screen(
            [sample_id] + [""] * 8,
            lambda: measure_screens.menu_prepare(
                self.mission, status, view),
        )

        status = self.link.get_status()
        view = self.mission.slot_view(status)

        run_screen(
            ["y"],
            lambda: measure_screens.menu_confirm(
                self.mission, status, view),
        )

        status = self.link.get_status()
        view = self.mission.slot_view(status)

        return run_screen(
            ["", "", "3"],
            lambda: measure_screens.menu_measure(
                self.mission, status, view),
        )

    def screen(self, script, call):
        return run_screen(list(script), call)

    def close(self):
        self.bd.close()


# ======================================================================
checks.section("a normal measurement leaves the archive alone")

bench = Bench()

try:
    checks.equal(bench.esp32_ids(), [], "the device starts empty")
    checks.equal(bench.session_ids(), [], "the session starts empty")
    checks.equal(bench.archive_ids(), [], "the archive starts empty")

    bench.measure("s01", slot=1)

    checks.equal(bench.esp32_ids(), ["s01"],
                 "after MEASURE the ESP32 holds the acquisition")
    checks.equal(bench.session_ids(), ["s01"],
                 "and so does the PC session - RAW that exists only in "
                 "device RAM is one board reset from being gone")
    checks.equal(bench.archive_ids(), [],
                 "and the PC ARCHIVE is still empty. THIS IS THE POINT: "
                 "nothing reaches it without an explicit import")

    session_record = bench.mission.session.get_sample("s01")

    checks.equal(session_record["state"], "MEASURED",
                 "the session record is complete")
    checks.ok(session_record["measurements"][0].get("raw"),
              "and carries its RAW")
    checks.ok(session_record["measurements"][0].get("analysis_runs"),
              "and the analysis beside it")

finally:
    bench.close()


# ======================================================================
checks.section("import is the one door into the archive")

bench = Bench()

try:
    bench.measure("s01")

    before = bench.mission.session.get_sample("s01")

    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    checks.equal(bench.esp32_ids(), ["s01"],
                 "import is a COPY: the ESP32 still holds it")
    checks.equal(bench.session_ids(), ["s01"],
                 "and so does the session")
    checks.equal(bench.archive_ids(), ["s01"],
                 "and now the archive does too")

    checks.equal(bench.mission.session.get_sample("s01"), before,
                 "the session record was not touched by the import")

    archived = bench.mission.archive.get_sample("s01")

    checks.ok(archived["measurements"][0].get("raw"),
              "the archived copy carries RAW")
    checks.equal(
        archived["measurements"][0]["acquisition"]["origin"],
        "esp32_buffer",
        "and says where it came from rather than claiming to be a "
        "measurement this program took")

    # ---- repeated import is idempotent -------------------------------
    result, _output, _console = bench.screen(
        [""], lambda: records_screens.import_esp32_samples(bench.mission))

    checks.equal(result["imported"], [],
                 "a repeated import imports nothing")
    checks.equal(result["skipped"], ["s01"],
                 "and reports the identical copy as skipped")
    checks.equal(result["conflicts"], [], "with no conflict")
    checks.equal(len(bench.mission.archive.get_sample("s01")
                     ["measurements"]), 1,
                 "and the archive still holds exactly ONE measurement - "
                 "no duplicate record was created")

finally:
    bench.close()


# ======================================================================
checks.section("a same-id sample with different data is a CONFLICT")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    archived_raw = dict(
        bench.mission.archive.get_sample("s01")["measurements"][0]["raw"]
    )

    # Change what the device is holding, keeping the same Sample ID.
    slot = bench.loop.service.carousel.slots[1]
    spectrum = slot["measurement"]["illuminations"]["white"]["acquisitions"]
    spectrum[0] = {
        channel: value + 500.0 for channel, value in spectrum[0].items()
    }

    result, output, _console = bench.screen(
        [""], lambda: records_screens.import_esp32_samples(bench.mission))

    checks.equal(result["conflicts"], ["s01"],
                 "a same-id sample with different data is a CONFLICT")
    checks.equal(result["imported"], [],
                 "and nothing is imported")
    checks.ok("NOTHING WAS OVERWRITTEN" in output,
              "the screen says so in as many words")

    checks.equal(
        bench.mission.archive.get_sample("s01")["measurements"][0]["raw"],
        archived_raw,
        "and the archived scientific record is byte-identical - a stored "
        "result is never silently replaced")
    checks.equal(len(bench.mission.archive.get_sample("s01")
                     ["measurements"]), 1,
                 "and no second measurement was appended either")

finally:
    bench.close()


# ======================================================================
checks.section("an interrupted import leaves what landed valid")

bench = Bench()

try:
    for slot_id, sample_id in ((1, "s01"), (2, "s02")):
        bench.link.select_slot(slot_id, sample_id)
        bench.link.request("sync_position", load_slot=slot_id)
        bench.link.measure_raw(slot_id, sample_id)

    checks.equal(bench.esp32_ids(), ["s01", "s02"],
                 "the device holds two acquisitions")

    # The second fetch dies on the wire.
    original = bench.mission.link.get_saved_sample
    seen = []

    def flaky(sample_id):
        seen.append(sample_id)

        if len(seen) > 1:
            raise serial_link.LinkError(
                "PORT_LOST", "the port went away mid-import")

        return original(sample_id)

    bench.mission.link.get_saved_sample = flaky

    result, _output, _console = bench.screen(
        [""], lambda: records_screens.import_esp32_samples(bench.mission))

    bench.mission.link.get_saved_sample = original

    checks.equal(len(result["imported"]), 1,
                 "the sample that landed before the link died is imported")
    checks.equal(len(result["failed"]), 1,
                 "and the one that did not is reported as failed, not "
                 "silently dropped")

    landed = result["imported"][0]
    record = bench.mission.archive.get_sample(landed)

    checks.ok(record is not None and record["measurements"][0].get("raw"),
              "what landed is a complete, valid record")
    checks.equal(bench.esp32_ids(), ["s01", "s02"],
                 "and the device still holds BOTH - a failed import "
                 "destroys nothing")

    # Running it again finishes the job.
    result, _output, _console = bench.screen(
        [""], lambda: records_screens.import_esp32_samples(bench.mission))

    checks.equal(sorted(result["imported"] + result["skipped"]),
                 ["s01", "s02"],
                 "re-running the import completes it, without duplicating "
                 "the part that had already landed")
    checks.equal(bench.archive_ids(), ["s01", "s02"],
                 "and the archive now holds both, once each")

finally:
    bench.close()


# ======================================================================
checks.section("delete from the ESP32 touches the ESP32 and nothing else")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    learning_before = bench.learning_count()
    archive_before = bench.mission.archive.get_sample("s01")

    rows = records_screens.storage_rows(bench.mission)

    _value, output, _console = bench.screen(
        ["1", "y", ""],
        lambda: records_screens.delete_one_from_esp32(bench.mission, rows),
    )

    checks.equal(bench.esp32_ids(), [],
                 "the ESP32 copy is gone")
    checks.equal(bench.session_ids(), ["s01"],
                 "the SESSION copy is untouched")
    checks.equal(bench.archive_ids(), ["s01"],
                 "the ARCHIVE copy is untouched")
    checks.equal(bench.mission.archive.get_sample("s01"), archive_before,
                 "and byte-identical")
    checks.equal(bench.learning_count(), learning_before,
                 "the Decision Learning database is untouched")
    checks.ok("Left intact:   SESSION, ARCHIVE" in output,
              "and the confirmation named the survivors before deleting")

finally:
    bench.close()


# ======================================================================
checks.section("delete from the PC archive touches the archive only")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    learning_before = bench.learning_count()
    rows = records_screens.storage_rows(bench.mission)

    _value, output, _console = bench.screen(
        ["1", "y", ""],
        lambda: records_screens.delete_one_from_collection(
            bench.mission, rows, bench.mission.archive,
            records_screens.STORE_ARCHIVE, "in_archive"),
    )

    checks.equal(bench.archive_ids(), [], "the archive copy is gone")
    checks.equal(bench.esp32_ids(), ["s01"], "the ESP32 copy is untouched")
    checks.equal(bench.session_ids(), ["s01"],
                 "the session copy is untouched")
    checks.equal(bench.learning_count(), learning_before,
                 "the Decision Learning database is untouched")
    checks.ok("PC ARCHIVE" in output,
              "and the screen named the store it was emptying")

finally:
    bench.close()


# ======================================================================
checks.section("delete from the PC session touches the session only")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    rows = records_screens.storage_rows(bench.mission)

    bench.screen(
        ["1", "y", ""],
        lambda: records_screens.delete_one_from_collection(
            bench.mission, rows, bench.mission.session,
            records_screens.STORE_SESSION, "in_session"),
    )

    checks.equal(bench.session_ids(), [], "the session copy is gone")
    checks.equal(bench.archive_ids(), ["s01"],
                 "the archive copy is untouched")
    checks.equal(bench.esp32_ids(), ["s01"],
                 "the ESP32 copy is untouched")

finally:
    bench.close()


# ======================================================================
checks.section("delete ALL from the ESP32 is location-specific")

bench = Bench()

try:
    for slot_id, sample_id in ((1, "s01"), (2, "s02")):
        bench.link.select_slot(slot_id, sample_id)
        bench.link.request("sync_position", load_slot=slot_id)
        bench.link.measure_raw(slot_id, sample_id)

    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    learning_before = bench.learning_count()
    archive_before = {
        sample_id: bench.mission.archive.get_sample(sample_id)
        for sample_id in bench.archive_ids()
    }

    _value, output, _console = bench.screen(
        ["y", ""],
        lambda: records_screens.delete_all_esp32_samples(bench.mission),
    )

    checks.equal(bench.esp32_ids(), [], "the ESP32 is empty")
    checks.ok("verified by reading it back" in output,
              "and that was verified by reading the device back, not by "
              "trusting its return code")
    checks.equal(bench.archive_ids(), ["s01", "s02"],
                 "the PC archive still holds both")

    for sample_id, record in archive_before.items():
        checks.equal(bench.mission.archive.get_sample(sample_id), record,
                     "{} is byte-identical in the archive".format(sample_id))

    checks.equal(bench.learning_count(), learning_before,
                 "the Decision Learning database is untouched")

finally:
    bench.close()


# ======================================================================
checks.section("clearing a slot does NOT delete the device's acquisition")

# THE DEFECT THAT MADE [7] LOOK BROKEN. `clear_slot` replaced the slot
# with a blank one, dropping `measurement` with the occupancy - so an
# operator who cleared a slot to load the next sample destroyed the
# device's copy of the spectrum, and the delete screen then correctly
# reported an empty device over data that had been thrown away by a
# command that promised to free the mechanism only.

bench = Bench()

try:
    bench.measure("s01")

    checks.equal(bench.esp32_ids(), ["s01"],
                 "the device holds the acquisition")

    bench.link.clear_slot(1)

    checks.equal(bench.esp32_ids(), ["s01"],
                 "and STILL holds it after the slot is cleared - emptying "
                 "a cup and deleting a measurement are different things")

    status = bench.link.get_status()
    slot = next(row for row in status["slots"] if row["slot_id"] == 1)

    checks.ok(not slot["occupied"],
              "while the slot itself really was freed")

    # And clear_all_slots behaves the same way.
    bench.link.clear_all_slots()

    checks.equal(bench.esp32_ids(), ["s01"],
                 "clear_all_slots keeps the acquisitions too")

    # The delete command is the one that removes them.
    bench.screen(["y", ""],
                 lambda: records_screens.delete_all_esp32_samples(
                     bench.mission))

    checks.equal(bench.esp32_ids(), [],
                 "and delete_saved_samples, which is the command for it, "
                 "does remove them")

finally:
    bench.close()


# ======================================================================
checks.section("clearing the session leaves the archive alone")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    rows = records_screens.storage_rows(bench.mission)

    _value, output, _console = bench.screen(
        ["y", ""],
        lambda: records_screens.clear_session(bench.mission, rows),
    )

    checks.equal(bench.session_ids(), [], "the session is empty")
    checks.equal(bench.archive_ids(), ["s01"],
                 "and the archive is untouched")
    checks.ok("also in" in output and "ARCHIVE" in output,
              "the screen said which session samples had another copy")

finally:
    bench.close()


# ======================================================================
checks.section("clearing an UNARCHIVED session says so first")

bench = Bench()

try:
    bench.measure("s01")

    rows = records_screens.storage_rows(bench.mission)

    _value, output, _console = bench.screen(
        ["n", ""],
        lambda: records_screens.clear_session(bench.mission, rows),
    )

    checks.ok("NOT in the PC archive" in output,
              "a session sample with no ARCHIVED copy is flagged")
    checks.ok("does not count" in output,
              "and the confirmation says that the device's RAM copy is "
              "not a reason to believe the science is safe")
    checks.equal(bench.session_ids(), ["s01"],
                 "and answering no changes nothing")

finally:
    bench.close()


# ======================================================================
checks.section("an unreachable device is not an empty one")

bench = Bench()

try:
    bench.measure("s01")

    def dead():
        raise serial_link.LinkError("PORT_LOST", "the port went away")

    bench.mission.link.list_saved_samples = dead

    index = records_screens.esp32_index(bench.mission)

    checks.ok(not index["reachable"], "the index reports UNREACHABLE")
    checks.equal(index["entries"], [], "with no entries")

    _value, output, _console = bench.screen(
        [""],
        lambda: records_screens.delete_all_esp32_samples(bench.mission),
    )

    checks.ok("NOTHING WAS DELETED" in output,
              "delete-all refuses over a link it could not use")
    checks.ok("not an empty" in output,
              "and says why: an unreachable device is not an empty one")

    _value, output, _console = bench.screen(
        [""],
        lambda: records_screens.import_esp32_samples(bench.mission),
    )

    checks.ok("NOTHING WAS IMPORTED" in output,
              "and the import refuses too, rather than reporting success "
              "over a device it never read")
    checks.equal(bench.archive_ids(), [],
                 "with the archive unchanged")

finally:
    bench.close()


# ======================================================================
checks.section("the table says where every sample is")

bench = Bench()

try:
    bench.measure("s01")
    bench.screen([""], lambda: records_screens.import_esp32_samples(
        bench.mission))

    bench.link.select_slot(2, "s02")
    bench.link.request("sync_position", load_slot=2)
    bench.link.measure_raw(2, "s02")

    rows = records_screens.storage_rows(bench.mission)
    by_id = {row["sample_id"]: row for row in rows}

    checks.equal(sorted(by_id), ["s01", "s02"],
                 "the table is the UNION of the three stores, not any "
                 "one store's list")

    checks.ok(by_id["s01"]["on_esp32"] and by_id["s01"]["in_session"]
              and by_id["s01"]["in_archive"],
              "s01 is shown in all three")
    checks.ok(by_id["s02"]["on_esp32"] and not by_id["s02"]["in_session"]
              and not by_id["s02"]["in_archive"],
              "s02 is shown ONLY on the device - the row an operator "
              "needs before pressing anything destructive")

    _value, output, _console = bench.screen(
        [], lambda: records_screens.print_storage_table(
            rows, records_screens.esp32_index(bench.mission)))

    for column in ("ESP32", "SESSION", "ARCHIVE"):
        checks.ok(column in output,
                  "the drawn table has an explicit {} column".format(column))

finally:
    bench.close()


# ======================================================================
checks.section("adopt refuses to overwrite, at the storage layer")

bench = Bench()

try:
    bench.measure("s01")

    record = bench.mission.session.get_sample("s01")
    bench.mission.archive.adopt(record)

    checks.equal(bench.archive_ids(), ["s01"], "the first adopt lands")

    def adopt_again():
        bench.mission.archive.adopt(record)

    checks.raises(StorageError, adopt_again,
                  "a second adopt of the same id is refused - deciding "
                  "what an existing record should become needs both "
                  "copies in hand, and this layer makes sure the decision "
                  "cannot be skipped")

    checks.equal(len(bench.mission.archive.get_sample("s01")
                     ["measurements"]), 1,
                 "and nothing was appended")

finally:
    bench.close()


restore_serial()

sys.exit(checks.report())
