"""
Choosing a material, from the vocabulary the databases actually carry.

WHY THIS IS ITS OWN MODULE

Ground truth is entered by name, and the name has to be one the
learning store will accept. The old prompt was a bare free-text field:

    Material (name, alias or blank to cancel):

An operator who did not already know the canonical spelling had two
options - guess, or give up - and a guess was refused with "'X' is not
a known material" and a paragraph explaining why guessing is dangerous.
Correct, and useless: it never said what WAS valid.

Worse, `suggest()` is substring-only by design, so a word that shares
no substring with any material returns NOTHING. Typing `soil` produced
an empty suggestion list and the lecture, with no way forward at all.

So the list is shown, and it is shown in the order that matches the
bench.

THE TWO LIBRARIES ARE NOT THE SAME KIND OF THING

    DB1   23 materials measured ON THIS INSTRUMENT, from labelled
          containers that physically exist on the bench. These are the
          ones an operator can actually pick up and weigh.

    DB3   84 entries from an external reference catalogue. Real
          materials with real spectra, but nothing here has ever seen
          them, and none of them is in the room.

Both are legitimate labels; only one is likely to be what the operator
is holding. So DB1 is listed first and DB3 is a keypress away, rather
than both being poured into one 107-line list where the jar on the
bench and a USGS catalogue number look identical.

NOTHING HERE RESOLVES A NAME ITSELF. Every lookup goes through
Science.taxonomy, which refuses what it cannot resolve. This module
chooses what to SHOW; it never decides what a name means.
"""

import unicodedata

from workflow.prompts import ask, choose, confirm

# Words that name a substance which is real, is usually most of the
# sample, and is NOT in any library: ordinary soil, sand, dirt.
#
# THIS IS NOT MATERIAL MATCHING. It never selects a material and never
# influences a label - it recognises that the operator is describing a
# MATRIX and points at the field built for it. The alternative, which
# is what happened before, is refusing "soil" with no explanation of
# where soil is supposed to go, when the schema has a first-class place
# for exactly that.
MATRIX_WORDS = (
    "soil", "sand", "dirt", "earth", "ground", "regolith", "mud",
    "zemina", "hlina", "pisek", "puda",
)


def _fold(text):
    """
    Lower case, and strip the diacritics off it.

    THE CZECH WORDS WERE UNREACHABLE AS TYPED. The list above is
    ASCII, and this is a Brno team typing on a Czech keyboard: the
    words they actually write are "pisek", "puda" and "hlina" WITH the
    carka and krouzek on them. A plain substring test against an ASCII
    list matches none of those, so an operator who spelled their own
    language correctly got the "not a known material" refusal and the
    matrix field was never offered - which is the exact dead end this
    module was written to remove.

    NFD splits a letter into its base and its accent; dropping the
    combining marks leaves the base. Nothing else is normalised, and
    the folded form is used only to RECOGNISE a matrix word - the
    operator's own text is stored as they typed it.
    """
    decomposed = unicodedata.normalize("NFD", str(text or "").strip().lower())

    return "".join(ch for ch in decomposed
                   if not unicodedata.combining(ch))


def looks_like_matrix(text):
    """Whether an unresolvable name is describing the matrix."""
    folded = _fold(text)

    return any(word in folded for word in MATRIX_WORDS)


class MatrixChoice:
    """
    The operator picked the matrix rather than a library material.

    A DISTINCT TYPE, NOT A FAKE MaterialIdentity. Soil is visible and
    selectable on the material screen now, and the one thing that must
    not follow from that is soil quietly becoming a library entry with
    a reference spectrum it does not have. Nothing can score a
    measurement against this object; it carries a label and a role and
    that is all it can ever carry.

    `label` is the operator's own words, stored as typed.
    """

    role = "MATRIX"

    def __init__(self, label):
        self.label = str(label).strip()

    @property
    def display_name(self):
        return self.label

    def __repr__(self):
        return "MatrixChoice({!r})".format(self.label)


# What the matrix option offers before the operator types anything.
# Soil first because it is what this instrument is for.
MATRIX_SUGGESTIONS = ("soil", "sand", "regolith")


def bench_materials(taxonomy):
    """The materials measured on this instrument, in display order."""
    return [
        identity for _key, identity in sorted(taxonomy.materials.items())
        if identity.source == "DB1"
    ]


def catalogue_materials(taxonomy):
    """Everything else: real spectra, but not from this bench."""
    return [
        identity for _key, identity in sorted(taxonomy.materials.items())
        if identity.source != "DB1"
    ]


def print_material_table(identities, offset=0):
    """A numbered material list: what to type, and what it is."""
    for index, identity in enumerate(identities, start=offset + 1):
        print("  {:>3}  {:<34} {}".format(
            index,
            identity.display_name[:34],
            identity.family_id or "-",
        ))


def _confirm_identity(identity, typed=None):
    """
    Show what a name resolved TO before it becomes a label.

    Alias resolution is made explicit - §13. "Aktivni uhli" and
    "Activated charcoal" both resolve to Activated Carbon, and an
    operator who typed one of them should see the other before the
    record is written, not afterwards in the history screen.
    """
    print()

    if typed and typed.strip().lower() != identity.display_name.lower():
        print("  {!r} -> {}".format(typed, identity.display_name))
    else:
        print("  {}".format(identity.display_name))

    print("  family: {}   source: {}".format(
        identity.family_id or "-", identity.source
    ))

    return confirm("Is that the material?")


def search_materials(taxonomy, text, bench_first=True):
    """
    Materials matching typed text, the bench ones first.

    `taxonomy.suggest` ranks by starts-with then contains, which is the
    right doctrine - substring only, so a plausible-looking wrong
    material never reaches the top - but it knows nothing about which
    jars are in the room. Searching "carbon" therefore put
    `Carbon Black GDS68`, a catalogue entry nobody here has ever seen,
    ABOVE `Activated Carbon`, which is on the shelf.

    So the ranking is kept and re-sorted by source: DB1 is what the
    operator can physically pick up.
    """
    matches = []

    for name in taxonomy.suggest(text, limit=24):
        identity = taxonomy.get(name)

        if identity is not None and identity not in matches:
            matches.append(identity)

    if bench_first:
        matches.sort(key=lambda i: 0 if i.source == "DB1" else 1)

    return matches


def _ask_matrix_label(typed=None):
    """
    Name the matrix, defaulting to what the operator already typed.

    Separated so both routes into it - the [s] option and typing
    "soil" at the prompt - end in exactly the same question.
    """
    default = (typed or "").strip() or "soil"

    print()
    print("MATRIX - what the components were mixed INTO")
    print()
    print("This is a real substance with a real mass and NO reference")
    print("spectrum: nothing scores a measurement against it. It is")
    print("recorded by name and counted in the 100 %, and it is not")
    print("turned into a library material.")
    print()
    print("Examples: {}.".format(", ".join(MATRIX_SUGGESTIONS)))
    print()

    label = ask("Matrix name [{}]".format(default))

    if label is None:
        return None

    label = label.strip() or default

    return MatrixChoice(label)


def select_material(mission, prompt="Select material",
                    allow_matrix=False):
    """
    Pick one material from the controlled vocabulary, or None to cancel.

    Returns a MaterialIdentity, a MatrixChoice when `allow_matrix` is
    set and the operator chose the matrix, or None to cancel. A typed
    name is never turned into a label without the taxonomy resolving
    it - except the matrix, which is deliberately not a library name and
    is stored as its own role.

    SOIL IS ON THE MENU WHEN THE CALLER CAN USE IT.

    It used to be reachable only by typing it, being told at length
    that soil is not a library material, and being returned to the same
    "COMPONENT 2 - SELECT MATERIAL" prompt - so the operator could type
    "soil" as many times as they liked and get the same lecture. The
    explanation was correct and the screen was a dead end. Now the
    option is listed, typing a matrix word takes it, and the difference
    between a COMPONENT and the MATRIX is still recorded exactly.

    SEARCH IS A FIRST-CLASS ACTION, NOT A FAILED LOOKUP.

    This screen advertises "[text] search by name or alias", and then
    sent free text to an EXACT resolve; anything that did not resolve
    fell into an error path whose first line was

        'carbon' is not a known material name, id or alias.

    So typing the word on the jar - the documented way to use this
    screen - was reported as a mistake, the suggestions came back with
    a catalogue material first, and the full 23-line table was reprinted
    underneath. That is the "awkward and not discoverable" the operator
    hit. Searching now answers with results.
    """
    taxonomy = mission.taxonomy

    if taxonomy is None:
        print("The material vocabulary is not loaded.")

        return None

    bench = bench_materials(taxonomy)
    catalogue = catalogue_materials(taxonomy)
    showing_all = False

    # When set, the screen shows these results instead of the whole
    # library - §20, and the reason the list stops scrolling past.
    results = None
    searched_for = None

    while True:
        if results is not None:
            listed = results

            print()
            print("{} match{} for {!r}:".format(
                len(listed), "" if len(listed) == 1 else "es", searched_for))
            print()
            print_material_table(listed)
            print()
            print("  [number]  select")
            print("  [text]    search again")
            print("  [l]       back to the full list")

            if allow_matrix:
                print("  [s]       soil / sand - the MATRIX, no reference "
                      "spectrum")

            print("  [c]       cancel")

        else:
            listed = bench + catalogue if showing_all else bench

            print()
            print(prompt.upper())
            print()

            if showing_all:
                print("All {} materials ({} measured here, {} reference "
                      "catalogue):".format(
                          len(listed), len(bench), len(catalogue)))
            else:
                print("Measured on this instrument ({}):".format(len(bench)))

            print()
            print_material_table(listed)
            print()
            print("  [number]  select")
            print("  [text]    search by name or alias")

            if not showing_all and catalogue:
                print("  [a]       also show the {} reference catalogue "
                      "materials".format(len(catalogue)))

            if allow_matrix:
                print("  [s]       soil / sand - the MATRIX, no reference "
                      "spectrum")

            print("  [c]       cancel")

        answer = ask("Material")

        if not answer or answer.strip().lower() == "c":
            return None

        folded = answer.strip().lower()

        if folded == "l" and results is not None:
            results = None

            continue

        # The advertised key for the matrix. Typing the word works too,
        # but that is handled at the bottom of the loop so an exact
        # library name and a search still win first: if somebody ever
        # adds a material legitimately called "Soil Standard", it
        # resolves as a material.
        if allow_matrix and folded == "s":
            choice = _ask_matrix_label()

            if choice is None:
                continue

            return choice

        if folded == "a" and results is None and not showing_all                 and catalogue:
            showing_all = True

            continue

        if answer.strip().isdigit():
            index = int(answer.strip())

            if 1 <= index <= len(listed):
                # NO SECOND CONFIRMATION FOR A NUMBERED PICK. The
                # operator is choosing a line off a table they are
                # looking at; asking "is that the material?" underneath
                # it is a keypress that answers a question nobody
                # asked. Alias resolution still confirms, below, where
                # the typed text and the material differ.
                return listed[index - 1]

            print()
            print("There is no material {} in the list above.".format(index))

            continue

        # An exact name or alias resolves straight away.
        identity = taxonomy.get(answer)

        if identity is not None:
            if identity.display_name.strip().lower() == folded:
                return identity

            # Typed an alias: show what it became before it is stored.
            if _confirm_identity(identity, typed=answer):
                return identity

            continue

        # Otherwise it is a SEARCH.
        matches = search_materials(taxonomy, answer)

        if matches:
            results = matches
            searched_for = answer.strip()

            continue

        results = None

        # NOTHING MATCHED, AND THE WORD DESCRIBES THE MATRIX.
        #
        # Below the exact resolve and below the search, so a real
        # material always wins - but above the explanation, because
        # explaining is not what the operator needed. They named what
        # is in the bowl; take it.
        if allow_matrix and looks_like_matrix(answer):
            choice = _ask_matrix_label(answer)

            if choice is None:
                continue

            return choice

        _explain_no_match(answer, allow_matrix=allow_matrix)


def _explain_no_match(text, allow_matrix=False):
    """
    Nothing matched. Say where the thing they described actually goes.

    Never "invalid material" on its own - §13. Ordinary soil has no
    reference spectrum and is not in any library, and that is not an
    oversight: it is why PREPARED_MIXTURE has a MATRIX role.
    """
    print()

    if looks_like_matrix(text):
        # Only reachable where the matrix CANNOT be chosen - the plain
        # "what is this sample" screen. Where it can, the caller took
        # the operator to the matrix question instead of reading them
        # this. Ending an explanation at the same prompt that produced
        # it is a loop, and it was one.
        print("Ordinary soil, sand and dirt are not library materials:")
        print("they have no reference spectrum, so nothing could be")
        print("scored against them.")
        print()
        print("They are not missing - they are the MATRIX, and this")
        print("screen is asking for a single material. Record it as a")
        print("KNOWN PREPARED MIXTURE instead: there, soil is one of")
        print("the choices.")

    else:
        print("Nothing in the library matches {!r}. Try part of the "
              "name,".format(text.strip()))
        print("a number from the list, or [a] to include the reference")
        print("catalogue.")


