"""
A throwaway BD/, so that no test can reach the real one.

THE RULE THIS EXISTS TO ENFORCE

`firmware/BD/` holds scientific evidence. DB1, DB2, DB3 and the
calibration library are reference data; `BD/samples/` is the run's only
irreplaceable output and git cannot restore it. A test that writes
there is not a test, it is data loss with a green tick next to it.

So every store a test touches is pointed at a temporary directory. The
reference files are COPIED in, which gives the tests real data to work
with and makes the copy the only thing they can damage.

`data_integrity/test_protected_data.py` hashes the real files before
and after the whole campaign and fails if any of them moved. This
module is what makes that check pass; the check is what proves this
module is being used.
"""

import shutil
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support                                              # noqa: E402

support.add_project_root()

from BD import config as bd_config                          # noqa: E402


class SandboxBD:
    """
    A temporary BD/ tree, seeded from the real one.

    Use as a context manager; the directory is removed on exit.

        with SandboxBD() as bd:
            store = bd.sample_database().archive()
    """

    # Copied because tests want real reference data to compute against.
    # Everything else is created empty.
    SEEDED = (
        # ONE calibration file now: calibrations, the protected LEGACY
        # White/Dark and the acquisition profiles are one document.
        ("calibration", ("calibration.json",)),
        ("DB1", ("DB1.json", "operator_aliases.json")),
        ("DB2", ("DB2.json",)),
        ("DB3", ("DB3.json",)),
        ("models", ("registry.json",)),
    )

    # The pre-consolidation files, copied only by `SandboxBD(legacy=True)`
    # so the migration itself can be tested against real data without
    # any test being able to reach the real BD/.
    LEGACY_SEEDED = (
        ("calibration", ("calibration_active.json",
                         "calibration_legacy.json",
                         "calibrations.json",
                         "acquisition_profiles.json")),
    )

    def __init__(self, seed=True, legacy=False, samples=True):
        self.root = Path(tempfile.mkdtemp(prefix="freya-bd-"))
        self.seeded = seed

        for name in ("calibration", "DB1", "DB2", "DB3", "models",
                     "samples", "training"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

        if seed:
            self._seed(self.SEEDED)

        if legacy:
            self._seed(self.LEGACY_SEEDED)

        if samples:
            # THE CURRENT SHAPE, so opening a sandbox does not exercise
            # the migration by accident. The file holds the archive and
            # nothing else: the session is a per-process working set
            # that is never written out, so a Sample created in it is
            # deliberately absent from here.
            (self.root / "samples" / "samples.json").write_text(
                '{"schema_version": %d, "storage_layout": '
                '"archive-only-v1", "archive": []}'
                % bd_config.SAMPLE_SCHEMA_VERSION,
                encoding="utf-8",
            )

    def _seed(self, groups):
        for directory, names in groups:
            source_dir = bd_config.BD_DIR / directory

            for name in names:
                source = source_dir / name

                if source.is_file():
                    shutil.copy2(source, self.root / directory / name)

    # -- paths ----------------------------------------------------------

    @property
    def samples_file(self):
        return self.root / "samples" / "samples.json"

    @property
    def calibration_dir(self):
        return self.root / "calibration"

    @property
    def learning_db(self):
        return self.root / "training" / "decision_learning.sqlite3"

    @property
    def calibration_file(self):
        return self.root / "calibration" / "calibration.json"

    # -- stores ---------------------------------------------------------

    def sample_database(self):
        from BD.samples import SampleDatabase

        return SampleDatabase(self.samples_file).load()

    def calibration_store(self):
        from BD.calibrations import CalibrationStore

        return CalibrationStore(directory=self.calibration_dir)

    def profile_store(self):
        from BD.acquisition_profiles import AcquisitionProfileStore

        return AcquisitionProfileStore(directory=self.calibration_dir)

    def learning_store(self):
        from BD.decision_learning import DecisionLearningStore

        return DecisionLearningStore(self.learning_db)

    # -- lifecycle ------------------------------------------------------

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False


def sandbox_mission(link, bd=None, science=True):
    """
    A `Mission` whose every writable store is in a sandbox.

    The read-only reference layers - DB1/DB2/DB3, the taxonomy, the
    registry - are left pointing at the real files, because reading
    them is safe and because a Mission with no databases exercises a
    much less interesting code path than one with them.
    """
    from workflow.session import Mission

    bd = bd or SandboxBD()
    mission = Mission(link)

    mission.samples = bd.sample_database()
    mission.session = mission.samples.session()
    mission.archive = mission.samples.archive()

    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()

    # THE LEARNING STORE TOO. It was the one writable store this helper
    # did not redirect, so `Mission.__init__` left it pointing at the
    # real `BD/training/decision_learning.sqlite3` - and any test that
    # saved ground truth wrote into the actual science archive. The
    # campaign's BD/ hash check catches that after the fact and fails
    # the whole run; nothing stopped it happening.
    #
    # Opened lazily via the sandbox so the file is created inside the
    # temporary tree, and closed with it.
    if mission.learning is not None:
        try:
            mission.learning.close()

        except Exception:                              # noqa: BLE001
            pass

    try:
        mission.learning = bd.learning_store()
        mission.learning_error = None

    except Exception as error:                         # noqa: BLE001
        mission.learning = None
        mission.learning_error = str(error)

    if science:
        # Re-read with the sandboxed calibration store in place, so the
        # active calibration comes from the copy and not from BD/.
        mission.load_science()

    mission.sandbox = bd

    return mission, bd
