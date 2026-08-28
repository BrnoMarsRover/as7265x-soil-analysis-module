"""
Learning History: what the operator can select, and what may become truth.

THE DEFECT THIS SUITE IS SHAPED AROUND

The ground-truth prompt was a bare free-text field. It refused every
name it could not resolve, correctly, and never once said what it would
accept. The operator had to already know the canonical spelling of a
material before they could record one.

That is bad enough on its own. What made it a dead end is that
`Taxonomy.suggest` is substring-only by design - a deliberate choice,
because edit distance would put a plausible WRONG material at the top of
the list and somebody would press 1. So a word sharing no substring
with any material returns nothing at all:

    resolve("Soil")  -> refused
    suggest("soil")  -> []

There is no material called Soil, and there should not be: ordinary
soil has no reference spectrum, and nothing could be scored against it.
It is the MATRIX of a prepared mixture, which the schema has had a
first-class role for all along. The interface simply never said so, so
the one workflow the operator most needed - spike a known material into
soil - looked impossible.

WHAT IS ASSERTED

That the vocabulary is shown rather than guessed at, that an
unresolvable name produces a route forward instead of a lecture, that
prepared proportions are validated in both directions, and that a model
estimate can never be written as ground truth. The last one is checked
at the store, because that is the only door into the table.
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

import support

support.add_project_root()
support.add_path("PC")

import builtins                                              # noqa: E402
import contextlib                                            # noqa: E402
import io                                                    # noqa: E402
import shutil                                                # noqa: E402
import tempfile                                              # noqa: E402

from Science.taxonomy import Taxonomy                        # noqa: E402
from workflow import materials                               # noqa: E402

import BD.decision_learning as learning                      # noqa: E402
from BD.decision_learning import (                           # noqa: E402
    LearningError,
    normalize_mixture,
)

checks = support.Checks("learning-ui")

TAXONOMY = Taxonomy()


class FakeMission:
    """Only what the material picker actually reads."""

    taxonomy = TAXONOMY


def drive(answers, call):
    """Run a screen with a scripted operator, capturing what it printed."""
    scripted = iter(answers)
    original = builtins.input

    def fake_input(prompt=""):
        try:
            return next(scripted)

        except StopIteration:
            raise AssertionError(
                "the screen asked more questions than the script had "
                "answers"
            )

    builtins.input = fake_input

    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            value = call()

        return value, out.getvalue()

    finally:
        builtins.input = original


# ======================================================================
checks.section("the vocabulary is SHOWN, not guessed at")

# Cancelling immediately still has to have printed the list: the whole
# point is that it is on screen before the operator answers.
_value, printed = drive(
    ["c"], lambda: materials.select_material(FakeMission()))

checks.ok("Activated Carbon" in printed,
          "the material list is on screen before anything is typed")
checks.ok("Bentonite" in printed and "Red Clay" in printed,
          "and it is the real vocabulary, not an example")

bench = materials.bench_materials(TAXONOMY)
catalogue = materials.catalogue_materials(TAXONOMY)

checks.ok(len(bench) > 0, "there are materials measured on this bench")
checks.ok(all(item.source == "DB1" for item in bench),
          "and 'measured here' means DB1 - the labelled containers that "
          "physically exist, not a catalogue entry")
checks.ok(all(item.source != "DB1" for item in catalogue),
          "while the reference catalogue is kept separate")
checks.ok(len(catalogue) > len(bench),
          "the catalogue is the larger of the two, which is why pouring "
          "both into one list buried the jars on the bench")

# The catalogue is reachable, not hidden.
checks.ok("[a]" in printed,
          "and the catalogue is one keypress away, not unreachable")

_value, all_printed = drive(
    ["a", "c"], lambda: materials.select_material(FakeMission()))

checks.ok(str(len(bench) + len(catalogue)) in all_printed,
          "[a] shows every material the taxonomy holds")


# ======================================================================
checks.section("selecting by number, and by alias")

identity, printed = drive(
    ["1", "y"], lambda: materials.select_material(FakeMission()))

checks.ok(identity is not None, "a numbered choice selects a material")
checks.equal(identity.source, "DB1",
             "and number 1 is a bench material, because those are listed "
             "first")

# ALIAS RESOLUTION IS SHOWN. The operator types the Czech name off the
# container; the canonical name is what gets stored, and they see the
# mapping before it is written.
identity, printed = drive(
    ["aktivni uhli", "y"],
    lambda: materials.select_material(FakeMission()))

checks.ok(identity is not None and identity.key == "Activated Carbon",
          "a Czech bench alias resolves to the canonical material")
checks.ok("->" in printed and "Activated Carbon" in printed,
          "and the resolution is shown explicitly before it is accepted")

# Declining the confirmation must NOT return the material.
identity, _printed = drive(
    ["aktivni uhli", "n", "c"],
    lambda: materials.select_material(FakeMission()))

checks.ok(identity is None,
          "answering 'no' to the confirmation selects nothing - a label "
          "is never attached to a material the operator rejected")

# Cancelling is always available.
identity, _printed = drive(
    ["c"], lambda: materials.select_material(FakeMission()))

checks.ok(identity is None, "[c] cancels and selects nothing")

identity, _printed = drive(
    [""], lambda: materials.select_material(FakeMission()))

checks.ok(identity is None, "and so does a bare Enter")


# ======================================================================
checks.section("'Soil' is answered, not merely refused")

# THE DEFECT, EXACTLY. The operator wants Activated Carbon in soil.
checks.ok(TAXONOMY.get("Soil") is None,
          "there is genuinely no material called Soil")
checks.equal(TAXONOMY.suggest("soil"), [],
             "and substring suggestion finds nothing for it, so the old "
             "screen had nothing at all to offer")

_value, printed = drive(
    ["soil", "c"], lambda: materials.select_material(FakeMission()))

lowered = printed.lower()

checks.ok("matrix" in lowered,
          "typing 'soil' now explains that soil is the MATRIX")
checks.ok("prepared mixture" in lowered,
          "and names the record type that holds it")
checks.ok("no reference spectrum" in lowered,
          "and says WHY it is not a library material, rather than only "
          "that it is not one")

# The list is still on screen afterwards, so there is a way forward.
checks.ok("Activated Carbon" in printed,
          "and the selectable materials are still shown, so the operator "
          "can pick the component they are spiking in")

for word in ("sand", "dirt", "zemina"):
    checks.ok(materials.looks_like_matrix(word),
              "{!r} is recognised as matrix language".format(word))

checks.ok(not materials.looks_like_matrix("Bentonite"),
          "but a real material name is not")

# THE WORDS THIS TEAM ACTUALLY TYPES.
#
# MATRIX_WORDS is written in ASCII and the operators are Czech, so the
# spellings that reach this function carry a carka or a krouzek:
# "pisek", "puda", "hlina". A plain substring test matched none of
# them, and an operator who spelled their own language correctly was
# refused with "not a known material" and never shown the matrix field
# - the exact dead end the material picker exists to remove.
for word in ("písek", "půda", "hlína", "Žlutý písek"):
    # ascii() rather than {!r}: this suite must also run on a Windows
    # console at cp1252, where PRINTING the word it is checking is
    # itself a UnicodeEncodeError.
    checks.ok(materials.looks_like_matrix(word),
              "{} - spelled with its diacritics - is recognised as "
              "matrix language".format(ascii(word)))

checks.ok(materials.looks_like_matrix("PISEK")
          and materials.looks_like_matrix("PÍSEK"),
          "and case does not matter, with or without the accent")

for name in ("Iron(III) Oxide Red", "Activated Carbon", "Kaolinite"):
    checks.ok(not materials.looks_like_matrix(name),
              "{!r} is still not matrix language - folding accents must "
              "not start matching real materials".format(name))


# ======================================================================
checks.section("prepared proportions are validated in both directions")

AC = {"role": "COMPONENT", "material_key": "Activated Carbon",
      "material_id": "activated_carbon", "family_id": "carbonaceous"}


def mixture(*parts):
    return normalize_mixture([dict(part) for part in parts])


# The workflow the operator actually wants: 30 % carbon in 70 % soil.
ok = mixture(
    dict(AC, prepared_mass_fraction=0.30),
    {"role": "MATRIX", "matrix_label": "garden soil",
     "prepared_mass_fraction": 0.70},
)

checks.equal(len(ok), 2, "a component-plus-matrix mixture is accepted")
checks.equal(ok[1]["role"], "MATRIX",
             "and the soil is stored as the MATRIX, not as a component")
checks.ok(ok[1]["material_key"] is None,
          "with no material_key, because it has no library identity - "
          "which is the honest record and the reason the role exists")


def refused(code, call, message):
    try:
        call()

    except LearningError as error:
        checks.equal(error.code, code, message)

        return

    checks.ok(False, "{} (nothing was refused)".format(message))


refused("MIXTURE_FRACTIONS_DO_NOT_SUM",
        lambda: mixture(
            dict(AC, prepared_mass_fraction=0.30),
            {"role": "MATRIX", "matrix_label": "soil",
             "prepared_mass_fraction": 0.63}),
        "93 % is refused - the missing mass was in the cup")

refused("MIXTURE_FRACTIONS_DO_NOT_SUM",
        lambda: mixture(
            dict(AC, prepared_mass_fraction=0.60),
            {"role": "MATRIX", "matrix_label": "soil",
             "prepared_mass_fraction": 0.60}),
        "and so is 120 % - something was counted twice")

refused("MIXTURE_COMPONENT_REPEATED",
        lambda: mixture(
            dict(AC, prepared_mass_fraction=0.30),
            dict(AC, prepared_mass_fraction=0.70)),
        "one material cannot appear twice - that is two answers to one "
        "question")

refused("MIXTURE_FRACTIONS_INCOMPLETE",
        lambda: mixture(
            dict(AC, prepared_mass_fraction=0.30),
            {"role": "MATRIX", "matrix_label": "soil"}),
        "a half-weighed mixture is refused - a fraction is a share of a "
        "total, and this total has a hole in it")

refused("MIXTURE_FRACTION_OUT_OF_RANGE",
        lambda: mixture(dict(AC, prepared_mass_fraction=30.0)),
        "30 is refused as a fraction - percentages go in as 0.30, and "
        "this is the mistake that would store a 3000 % sample")

refused("MIXTURE_EMPTY", lambda: normalize_mixture([]),
        "a mixture with no components is not a mixture")

# Tolerance is for scale resolution, not for a guess.
tight = mixture(
    dict(AC, prepared_mass_fraction=0.3000),
    {"role": "MATRIX", "matrix_label": "soil",
     "prepared_mass_fraction": 0.7020},
)

checks.equal(len(tight), 2,
             "2 parts in 1000 is inside tolerance - that is a kitchen "
             "scale, not a guess")


# ======================================================================
checks.section("a model estimate can never become ground truth")

# The rule that matters most, checked at the only door into the table.
# An unknown competition sample must not be entered as 30/70 because
# the Decision Model said so.

# A temporary file of its own, never the real learning database: this
# suite writes ground truth, and the operator's history is not a
# scratchpad. §41.
_scratch = tempfile.mkdtemp(prefix="freya-learning-ui-")

store = learning.DecisionLearningStore(
    Path(_scratch) / "learning_ui.sqlite"
)

try:
    store.add_observation("M-UI-1", {"white": {"A": 1.0}})

    for source in ("decision_model", "the model", "inference",
                   "auto", "classifier", "prediction"):
        try:
            store.add_ground_truth(
                "M-UI-1", learning.LABEL_EXACT_MATERIAL,
                material_key="Activated Carbon",
                verification_status=learning.VERIFIED,
                verification_source=source,
            )
            checks.ok(False,
                      "{!r} was accepted as a label source".format(source))

        except LearningError as error:
            checks.equal(error.code, "PREDICTION_IS_NOT_GROUND_TRUTH",
                         "{!r} is refused as a ground-truth source"
                         .format(source))

    # An unknown sample is a first-class record, and it is never
    # trainable however it is asked for.
    record = store.add_ground_truth(
        "M-UI-1", learning.LABEL_UNKNOWN_SAMPLE,
        verification_status=learning.VERIFIED,
        verification_source="operator",
    )

    checks.equal(record["verification_status"], "UNKNOWN",
                 "an UNKNOWN_SAMPLE is forced to UNKNOWN even when "
                 "VERIFIED is requested - 'I do not know' is not a label")

    checks.equal(store.labelled(), [],
                 "and it never appears in the training set")

    try:
        store.labelled(levels=("UNVERIFIED",))
        checks.ok(False, "UNVERIFIED was accepted as a training level")

    except LearningError as error:
        checks.equal(error.code, "UNTRUSTED_LEVEL_REQUESTED",
                     "UNVERIFIED cannot even be REQUESTED as a training "
                     "label")

finally:
    store.close()
    shutil.rmtree(_scratch, ignore_errors=True)


# ======================================================================
checks.section("a MATRIX is context, never a spectral class")

# THE BOUNDARY THE MIXTURE WORKFLOW DEPENDS ON.
#
# "garden soil" is legitimate ground truth: it says what the other 70 %
# of the cup actually was, and without it a model asked to find 30 %
# activated carbon is being scored with no idea what it was hiding in.
#
# It is NOT a material. It has no reference spectrum, nothing has ever
# measured it, and the moment it is treated as a class the library
# contains an entry that can never be matched against anything. These
# checks pin the separation at every layer that could erode it.

store = learning.DecisionLearningStore(
    Path(_scratch) / "matrix_boundary.sqlite"
)

try:
    store.add_observation("M-MIX-1", {"white": {"A": 1.0}})

    carbon = TAXONOMY.resolve("Activated Carbon")

    store.add_ground_truth(
        "M-MIX-1", learning.LABEL_PREPARED_MIXTURE,
        mixture=[
            {"role": "COMPONENT", "material_key": carbon.key,
             "material_id": carbon.material_id,
             "family_id": carbon.family_id,
             "prepared_mass_fraction": 0.30},
            {"role": "MATRIX", "matrix_label": "garden soil",
             "prepared_mass_fraction": 0.70},
        ],
        verification_status=learning.VERIFIED,
        verification_source="operator_prepared_mixture",
    )

    parts = store.components("M-MIX-1")

    checks.equal(len(parts), 2, "the prepared mixture stored both parts")

    component = [p for p in parts if p["role"] == "COMPONENT"][0]
    matrix = [p for p in parts if p["role"] == "MATRIX"][0]

    checks.equal(component["material_key"], "Activated Carbon",
                 "the component carries a canonical material identity")
    checks.ok(component["material_id"] and component["family_id"],
              "with its material_id and family - it can be scored "
              "against a reference spectrum")

    checks.equal(matrix["material_key"], None,
                 "THE MATRIX CARRIES NO material_key - it is described, "
                 "not identified, because no reference spectrum exists "
                 "for it")
    checks.equal(matrix["material_id"], None,
                 "and no material_id")
    checks.equal(matrix["family_id"], None,
                 "and no family - a family is a claim about chemistry "
                 "and nobody analysed this soil")
    checks.equal(matrix["matrix_label"], "garden soil",
                 "what it was is kept as descriptive text, which is the "
                 "honest record")

    # ---- it must not reach the vocabulary --------------------------
    checks.equal(TAXONOMY.get("garden soil"), None,
                 "'garden soil' does not resolve as a material - the "
                 "taxonomy is built from DB1/DB2/DB3 and never from the "
                 "learning history")

    fresh = Taxonomy()

    checks.equal(fresh.get("garden soil"), None,
                 "and a taxonomy rebuilt AFTER the mixture was saved "
                 "still does not know it - saving ground truth cannot "
                 "create a material")

    checks.equal(fresh.count(), TAXONOMY.count(),
                 "the material count is unchanged by recording a "
                 "mixture")

    # ---- it must not become a training class -----------------------
    trained = store.labelled(levels=(learning.VERIFIED,))

    checks.equal(trained, [],
                 "a PREPARED_MIXTURE is not returned as a labelled "
                 "single-material example at all, so neither half of it "
                 "can be learned as 'the spectrum of X'")

    keys = [
        row.get("material_key")
        for row in store.labelled(
            levels=(learning.VERIFIED,),
            label_types=(learning.LABEL_PREPARED_MIXTURE,))
    ]

    checks.ok("garden soil" not in keys,
              "and even when mixtures ARE asked for explicitly, the "
              "matrix is not one of the material keys")

    # ---- queries by material must not match it ---------------------
    checks.equal(store.observations_containing("garden soil"), [],
                 "querying for 'garden soil' as a material returns "
                 "nothing - component queries join on material_key, "
                 "which the matrix deliberately lacks")

    containing = store.observations_containing("Activated Carbon")

    checks.equal([r["measurement_id"] for r in containing], ["M-MIX-1"],
                 "while the real component IS found by that query")

    # ---- the summary keeps them apart ------------------------------
    summary = store.mixture_summary()

    checks.ok("Activated Carbon" in summary["by_material"],
              "the mixture summary counts the component as a material")
    checks.ok("garden soil" not in summary["by_material"],
              "and does NOT count the matrix as one")
    checks.ok("garden soil" in summary["matrices"],
              "it is reported under 'matrices', which is a separate "
              "tally with a separate meaning")

finally:
    store.close()


sys.exit(checks.report())
