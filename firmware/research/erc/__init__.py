"""
Science — the mission layer.

Science answers "what did the instrument measure?". Science.decision
answers "what does this one measurement most defensibly mean?". Neither
knows there is a competition, a hypothesis, or a Mars Yard.

This layer knows. It holds the pre-declared hypothesis, the four planned
sites, the mission record, the mission-level analysis that evaluates the
hypothesis against everything observed, and the ERC requirement registry
that says what the judges asked for.

Layer rule, enforced by Tests/test_architecture.py:

    Science -> BD              allowed
    Science -> Science    allowed
    Science -> Science.decision   allowed

    BD -> Science              FORBIDDEN
    Science -> Science    FORBIDDEN
    Science.decision -> Science   FORBIDDEN
    ESP32 -> Science           FORBIDDEN

The direction matters for the same reason it did one layer down: a
mission concept leaking into the mathematics would make the mathematics
un-testable without a mission, and would let a competition deadline
change a scientific result.
"""
