"""
Science — every scientific formula in the project, and the Decision Model.

    preprocessing   dark, white, representations, repeated readings
    quality         is this measurement usable, and which channels of it
    features        derivatives, ratios, cross-illumination
    metrics         cosine, spectral angle, correlation, distances
    comparison      distance to a material CLASS
    calibration     building and validating a calibration
    taxonomy        material identity and family
    class_models    class statistics from verified history
    model_registry  which model is ACTIVE, and what it takes to replace it
    decision        the Decision Model
    pipeline        the evidence package, and the one entry point

ONE ENTRY POINT

    run = pipeline.analyze(measurement, calibration, registry, ...)

DETERMINISTIC AND HARDWARE-INDEPENDENT. Given the same Measurement,
calibration and databases this returns the same answer. Nothing here
opens a serial port, moves a servo, asks the operator a question or
writes a file - which is what makes a stored conclusion reproducible,
and what lets every formula be tested against numbers worked out by
hand.

WHAT THIS LAYER DOES NOT DO

It does not produce a report. A structured Sample record is this
project's final product; turning one into prose, a PDF or a mission
document happens outside the repository entirely, and production
Science contains nothing that could.

It does not estimate composition. Similarity is not abundance, and a
percentage here is an angle between two reflectance vectors - never a
statement about how much of a material is present.

It does not decide what is true. It says what the evidence is, keeps
the databases and the methods apart so their disagreements survive, and
lets the Decision Model fuse them into a conclusion that carries its
own reasoning, its own confidence and its own version.
"""
