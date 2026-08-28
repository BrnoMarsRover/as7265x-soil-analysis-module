"""
The compact vocabulary: one place that decides what a state is CALLED.

WHY THIS MODULE EXISTS

Every screen used to describe the same state in its own words. The main
screen said "Servo: Waveshare ST3215", Carousel Setup said
"Communication: ONLINE", and a failed movement said three different
things about the carousel in three consecutive paragraphs - message,
carousel, action - all derived from the same two fields. An operator
reading them under competition pressure had to work out which of the
four sentences was the one that mattered.

So the labels live here, and the screens ask for them. A state has one
name everywhere it appears.

WHAT THIS MODULE MAY NOT DO

It reports. It never decides. Every fact here comes from `get_status` or
from the `data` block of a firmware refusal; nothing is inferred from a
previous screen, remembered between calls, or computed a second time.

In particular it never claims a physical event. Software knows that a
goal was written and what the encoder read afterwards; it does not know
whether the carousel turned. Those are different statements and §4 says
they stay different. `carousel_outcome` below is the only function that
maps evidence to words, and its three answers are the three the firmware
actually distinguishes.
"""

# ----------------------------------------------------------------------
# the labels
# ----------------------------------------------------------------------
# Fixed strings rather than f-strings at each call site, so a state
# cannot be spelled two ways in two screens.

ONLINE = "ONLINE"
OFFLINE = "OFFLINE"
NOT_ANSWERING = "NOT ANSWERING"
NOT_SELECTED = "NOT SELECTED"
UNREACHABLE = "UNREACHABLE"

POSITION_UNKNOWN = "POSITION UNKNOWN"

# The sensor states the firmware actually reports. AS7265x.state()
# returns exactly these three; nothing here invents a fourth.
SENSOR_READY = "READY"
SENSOR_UNAVAILABLE = "UNAVAILABLE"
SENSOR_NOT_INITIALIZED = "NOT INITIALIZED"

# Field width for a compact status block. Wide enough for "Temperature"
# and narrow enough that value columns line up inside 60 characters.
_WIDTH = 11


def field(label, value):
    """One aligned `label: value` line of a compact status block."""
    return "{:<{}} {}".format(label + ":", _WIDTH, value)


def print_fields(pairs):
    """
    A compact status block from (label, value) pairs.

    A pair whose value is None is DROPPED, not printed as "-". A field
    the firmware did not report is not a field with an empty value, and
    a block full of dashes is the wall of text this module exists to
    remove.
    """
    for label, value in pairs:
        if value is None:
            continue

        print(field(label, value))


# ----------------------------------------------------------------------
# servo and carousel
# ----------------------------------------------------------------------


def servo_link(status):
    """
    Whether the firmware currently holds a live link to the ST3215.

    THE DISTINCTION §5 IS ABOUT. A servo that is answering and a
    carousel whose position is unknown are two independent facts, and
    collapsing them sent operators to "connect the servo" for a servo
    that had never disconnected. This function answers only the first.
    """
    servo = (status or {}).get("servo") or {}
    backend = servo.get("backend") or {}

    # `connected` IS THE KEY THE FIRMWARE SENDS. `selected` IS NOT.
    #
    # This read `servo["selected"]` - a key ServoLink.status() has
    # never produced - so the test was ALWAYS false and this function
    # ALWAYS returned NOT SELECTED. Every screen showed
    # "Servo: NOT SELECTED" over a servo that was answering normally,
    # `servo_online()` was permanently False, and `recovery_action()`
    # permanently answered "Connect Servo / Carousel Setup".
    #
    # That is the whole of the §5 fix - servo ONLINE plus carousel
    # UNKNOWN means RE-SYNC, not reconnect - dead on arrival in
    # production. It passed review because the tests built their own
    # status dicts containing `selected`, so the fake agreed with the
    # reader and neither agreed with the board.
    #
    # The firmware's two levels mean different things and both matter:
    #   servo["connected"]             a driver is attached at all
    #   servo["backend"]["connected"]  and that driver's link answers
    # When nothing is attached there is no `backend` key whatsoever.
    if not servo.get("connected"):
        return NOT_SELECTED

    return ONLINE if backend.get("connected") else NOT_ANSWERING


def servo_online(status):
    """True only when the servo is selected AND answering."""
    return servo_link(status) == ONLINE


def position_valid(status):
    """Whether the firmware still trusts its carousel position."""
    carousel = (status or {}).get("carousel") or {}

    return bool(carousel.get("position_valid"))


def carousel_label(status):
    """
    Where the carousel is, or that nobody knows.

    POSITION UNKNOWN outranks the phase: a phase read off an invalidated
    position is a number the firmware itself does not believe.
    """
    if not position_valid(status):
        return POSITION_UNKNOWN

    carousel = (status or {}).get("carousel") or {}

    return carousel.get("carousel_phase") or "?"


def recovery_action(status):
    """
    The one thing the operator should do next about the carousel.

    Returns (key_label, needed). This is what makes `[0]` say
    "Re-sync Carousel" instead of telling somebody to connect a servo
    that is already connected - §5. The state model decides the words;
    the screen only prints them.
    """
    link = servo_link(status)

    if link == NOT_SELECTED:
        return "Connect Servo / Carousel Setup", True

    if link == NOT_ANSWERING:
        return "Reconnect Servo", True

    if not position_valid(status):
        return "Re-sync Carousel", True

    return None, False


# ----------------------------------------------------------------------
# sensor
# ----------------------------------------------------------------------


def sensor_state(status):
    """
    The sensor state, and the reason when there is one.

    Returns (state, detail). `detail` is the error CODE, never a
    sentence: the code is what identifies the fault and what an operator
    can look up, and the sentence belongs in diagnostics.

    Prefers the firmware's own `state` field over the `ready` boolean,
    because NOT_INITIALIZED and UNAVAILABLE are different situations and
    only one of them is a fault. A lazily-initialised sensor that
    nothing has asked for yet is not broken.
    """
    sensor = (status or {}).get("sensor") or {}

    state = sensor.get("state")

    if state:
        state = str(state).replace("_", " ")
    else:
        # Firmware older than the `state` field. The boolean is all
        # there is, and it cannot distinguish the two non-ready cases.
        state = SENSOR_READY if sensor.get("ready") else SENSOR_UNAVAILABLE

    detail = None

    if state != SENSOR_READY:
        # `first_init_error` is what the firmware sends. This read
        # `boot_error` for the whole of 6.0.0 - a key no firmware has
        # ever produced - so a sensor that failed to initialise showed
        # a bare "UNAVAILABLE" and the code that said why was dropped
        # on the floor every single time.
        error = sensor.get("current_error") or sensor.get("first_init_error")

        if isinstance(error, dict):
            detail = error.get("code")

    return state, detail


def sensor_label(status):
    """The sensor state as one line: `READY`, or `UNAVAILABLE | CODE`."""
    state, detail = sensor_state(status)

    return "{} | {}".format(state, detail) if detail else state


# ----------------------------------------------------------------------
# what a failed movement means
# ----------------------------------------------------------------------


def carousel_outcome(data):
    """
    What a refusal says about the mechanism, in the firmware's terms.

    Returns (carousel_line, action_line).

    THE THREE ANSWERS ARE THE FIRMWARE'S THREE, and this function adds
    no fourth. `Carousel.motion_verdict` already separates them from the
    encoder evidence:

        NOT_STARTED   the goal was never written; the carousel is where
                      it was, and that claim is supportable.
        MOVED         the encoder MEASURED travel. The only case where
                      movement may be stated as fact.
        UNKNOWN       a goal was written and the encoder saw nothing, or
                      there is no evidence at all.

    UNKNOWN is the case §4 is about, and it is where the old output
    printed "THE CAROUSEL MOVED" over an encoder reading of zero. What
    software knows there is that it wrote a goal. What the operator sees
    with their own eyes is a separate and better source, and the screen
    says so rather than pretending the two agree.
    """
    data = data or {}

    # THE RETURN OUTCOME OUTRANKS THE OUTBOUND ONE.
    #
    # This read `data["recovery"]`, a key no firmware has ever sent -
    # the block is called `return_move` - so the branch was dead and
    # every acquisition failure fell through to the `motion` test
    # below. `motion` describes the OUTBOUND transfer, which by
    # definition succeeded if the acquisition got to run at all, so it
    # is always MOVED there.
    #
    # The result was a screen that contradicted itself two lines apart:
    #
    #     Carousel:   POSITION UNKNOWN - the encoder measured travel
    #     Action:     inspect the mechanism and re-sync
    #     Returning Slot home............ PASS
    #
    # over a carousel the firmware had brought home and still trusted
    # (`position_valid: true`, phase LOAD). The operator was sent to
    # re-sync working, synchronized hardware after a purely SENSOR
    # fault.
    #
    # When the firmware reports on the return, that report is the last
    # thing that happened to the mechanism and it decides. Only when it
    # says the sample did NOT come back does the position become
    # unknown.
    return_move = data.get("return_move")

    if isinstance(return_move, dict) and return_move:
        if return_move.get("returned") and return_move.get("position_valid"):
            return (
                "LOAD - the sample was returned and the position still "
                "holds",
                None,
            )

        return (
            "{} - the sample did not come back".format(POSITION_UNKNOWN),
            "re-sync before moving again",
        )

    motion = data.get("motion")

    if motion == "NOT_STARTED" or (
        motion is None and data.get("moved") is False
    ):
        return "unchanged - the servo was never commanded", None

    if motion == "MOVED":
        return (
            "{} - the encoder measured travel".format(POSITION_UNKNOWN),
            "inspect the mechanism and re-sync",
        )

    return (
        "{} - move commanded, not verified".format(POSITION_UNKNOWN),
        "inspect the mechanism and re-sync",
    )


def _counts(value):
    return None if value is None else "{} cnt".format(value)


def movement_fields(data):
    """
    The evidence behind a failed movement, as compact status lines.

    Every value comes from `motion_detail`, which the servo driver fills
    from the same registers it used to decide the movement had failed.
    Nothing is recomputed here, so the block cannot disagree with the
    verdict printed beside it.
    """
    data = data or {}
    detail = data.get("motion_detail") or {}
    requested = data.get("requested") or {}

    pairs = []

    # What was asked for, in the terms it was asked in.
    if requested.get("slots") is not None:
        pairs.append((
            "Move",
            "{} slot(s) {}".format(
                requested.get("slots"), requested.get("direction") or ""
            ).strip(),
        ))

    elif requested.get("half_turn"):
        pairs.append((
            "Move", "half turn {}".format(requested.get("direction") or "")
        ))

    elif requested.get("degrees") is not None:
        pairs.append(("Move", "{:+g} deg".format(requested["degrees"])))

    start = detail.get("start_position")
    actual = detail.get("actual_position")

    if start is not None and actual is not None:
        travelled = detail.get("travelled_degrees")

        pairs.append((
            "Encoder",
            "{} -> {} cnt{}".format(
                start, actual,
                "" if travelled is None else " ({:+.2f} deg)".format(
                    travelled
                ),
            ),
        ))

    pairs.append(("Expected", _counts(detail.get("expected_position"))))

    error = detail.get("position_error")
    tolerance = detail.get("tolerance_counts")

    if error is not None:
        pairs.append((
            "Error",
            "{} cnt{}".format(
                error,
                "" if tolerance is None else " (tolerance {})".format(
                    tolerance
                ),
            ),
        ))

    return pairs


def print_failure(code, data, message=None, title=None, extra=None,
                  lead=None):
    """
    A refusal as ONE compact block: what failed, the evidence, the state.

    ACTION -> RESULT -> RESULTING STATE, said once each. The old screen
    printed the firmware's sentence, then the same fact again as
    `carousel:`, then a third time as an instruction - three paragraphs
    that an operator had to read in full to find the one line that
    changed what they should do.

    `message` is printed only when it adds something the structured
    fields do not already say. A firmware that has been updated to send
    a short factual message and full evidence produces no prose line at
    all here.

    `extra` is (label, value) pairs the caller knows and this module
    does not - what happened to the spectrum, what state the sample is
    left in. They sit between the carousel verdict and the action,
    because that is the order the operator needs them in: where the
    mechanism is, what was lost, what to do.

    `lead` is the same, for context that belongs BEFORE the evidence -
    which stage failed, and what that means physically.
    """
    print()
    print(title or code)

    pairs = list(lead or ())
    pairs.extend(movement_fields(data))

    carousel, action = carousel_outcome(data)

    if carousel:
        pairs.append(("Carousel", carousel))

    pairs.extend(extra or ())

    if action:
        pairs.append(("Action", action))

    if pairs:
        print()
        print_fields(pairs)

    # Only when there IS no structured evidence. A refusal that carries
    # a motion block has already said everything the sentence would.
    if message and not (data or {}).get("motion_detail"):
        print()
        print("  {}".format(message))
