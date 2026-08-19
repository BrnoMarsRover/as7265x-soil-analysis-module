"""
BD — the authoritative store for everything the system must remember.

    calibration/   dark and white references, and the conditions
    DB1/           MEASURED here, 18 channels, 23 materials, legacy
    DB2/           MEASURED here, 54 features (WHITE/UV/IR)
    DB3/           DERIVED_REFERENCE, external spectra projected
    training/      labelled records and the decision history, OFFLINE only
    models/        validated model artifacts and the registry
    samples/       completed Sample records - the run's only output

Two of these are read-only scientific evidence and one is the only
thing this system writes.

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
