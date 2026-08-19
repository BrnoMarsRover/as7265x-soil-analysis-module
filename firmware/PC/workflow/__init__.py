"""
The operator workflow.

One module per screen family, because the alternative was a single file
of six thousand lines that owned the prompts, the tables, the
calibration procedure, the servo diagnostics, the measurement sequence,
the record browser and the menu loop at once - and in which the one
ordering that matters scientifically, RAW before Science, was a detail
buried two hundred lines into a function.

    prompts      asking the operator; the only input() in the project
    display      turning results into tables
    session      the link, the stores, the loaded science layer
    calibration  making and choosing a calibration
    carousel     servo setup, alignment, diagnostics
    measure      the measurement sequence
    records      browsing the archive, recording ground truth
    screen       the main screen and the menu loop
"""
