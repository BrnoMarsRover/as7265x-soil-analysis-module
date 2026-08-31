"""
BD — the authoritative store for everything the system must remember.

    calibration/   calibration.json           every calibration, the
                                              protected LEGACY White/Dark,
                                              which one is ACTIVE, and the
                                              conditions each was taken under
    DB1/           DB1.json                   MEASURED here, 18 channels
    DB2/           DB2.json                   MEASURED here, 54 features
    DB3/           DB3.json                   DERIVED_REFERENCE, projected
    models/        registry.json              model artifacts, and which
                                              one is ACTIVE
    samples/       samples.json               the PC session and the PC
                                              archive - the run's own output
    training/      decision_learning.sqlite3  observations, ground truth
                                              and predictions. OFFLINE only

ONE CANONICAL PERSISTENT STORE PER SUBSYSTEM. The layout used to hold
several truths twice - an active-calibration pointer beside the library
that already recorded it, a JSON seed beside the database it had been
imported into, a schema backup beside the archive. A subsystem with two
persistent files can have two answers, so the duplicates were
consolidated and the redundant files DELETED. There is no fallback read
path for any of them; a fallback that survives migration is a second
source of truth wearing a different name.

DB1 and DB2 are read-only scientific evidence, as is the LEGACY record
inside the calibration database. `samples/` is the only thing normal
operation writes.

LAYER RULE: BD MUST NEVER IMPORT Science.

Storage has to be able to check the shape of a record without depending
on the layer that interprets it - otherwise a stored result cannot be
read back without the exact Science version that produced it, which is
the opposite of an archive. `channels.py` is the shared vocabulary:
which channels exist and what they are called, which is a fact about
the sensor and about the record format, not about what the numbers
mean.

BD contains no hardware code, no operator workflow and no scientific
mathematics.
"""
