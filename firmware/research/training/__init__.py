"""
Training and validation for the decision model.

Nothing in here runs automatically. Retraining is an explicit operator
command, because a model that retrains itself after every measurement
changes what a reported conclusion means without anyone deciding that it
should. §49.

    import_seed.py       bring historical measurements in, with a preview
    dataset_builder.py   a reproducible, hashed dataset snapshot
    cross_validation.py  group-aware splits that do not leak
    benchmark.py         baselines measured against each other
    train.py             the operator entry point
    validate.py          the report a model must pass to be activated
"""
