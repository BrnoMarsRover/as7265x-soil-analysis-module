"""
Canonical identity of the reference materials — names, formulae, classes.

ONE TABLE, TWO DATABASES. DB1 and DB2 hold measurements of the same 23
physical containers, so they must call them by the same names. This table
used to live inside build_db1.py; DB2 needed exactly the same identities
and copying it would have guaranteed the two drift apart the first time
a name was corrected in one place only.

REFERENCE KNOWLEDGE ABOUT THE SUBSTANCES, NEVER A MEASUREMENT. Nothing
here can change a measured value. A builder reads this to fill in
`display_name`, `canonical_name`, `chemical_formula`, `material_class`
and `aliases`; the numbers come from the source snapshots and from
nowhere else.

TWO LANGUAGES, BECAUSE THE BENCH HAS TWO
----------------------------------------
The library is written in English and the containers on the bench are
labelled in Czech. The operator types what the container says, so both
names are first-class:

    name_en   "Green Clay (Illite)"
    name_cs   "Jil zeleny (illit)"

and `aliases_cs` carries the label EXACTLY as it is printed on the
container, including the abbreviations and the misspellings, because
that is what gets typed at three in the morning. An alias is a NAME: it
resolves to a material that already exists, and it can never create one.

ASCII, DELIBERATELY
-------------------
The Czech names here are written without diacritics. The operator's
terminal is a Windows console that does not reliably render or accept
them, the whole database is transcribed from that console's output, and
a name nobody can type is not a name. `Science.taxonomy` folds
diacritics on lookup, so "Jíl zelený" typed with them still resolves.
"""

# key -> (canonical_name, chemical_formula, material_class,
#         aliases_en, name_cs, aliases_cs)
#
# The key is the display name DB1 was built with and is not changed:
# it is what every stored Sample, every prediction and every ground-truth
# label already refers to.
IDENTITIES = {
    "Iron(III) Oxide Red": (
        "Hematite (synthetic)", "Fe2O3", "iron_oxide",
        ["Ferric oxide", "Red iron oxide"],
        "Cerveny oxid zeleza",
        ["Cerveny oxid zeleza", "Oxid zelezity", "Cerven"],
    ),
    "Iron(II,III) Oxide Black": (
        "Magnetite (synthetic)", "Fe3O4", "iron_oxide",
        ["Black iron oxide", "Ferrous ferric oxide"],
        "Cerny oxid zeleza",
        ["Cerny oxid zeleza", "Oxid zeleznato-zelezity"],
    ),
    "Sulfur Powder": (
        "Sulfur", "S8", "native_element", ["Brimstone", "Sulphur"],
        "Sira mleta",
        ["Sira mleta sucha", "Sira mleta", "Sira", "Siry prasek"],
    ),
    "Magnesium Oxide": (
        "Periclase (synthetic)", "MgO", "oxide", ["Magnesia"],
        "Oxid horecnaty (paleny magnezit)",
        ["Oxid horecnaty- paleni magnezit", "Oxid horecnaty",
         "Paleny magnezit", "Paleni magnezit"],
    ),
    "Calcium Carbonate (Chalk)": (
        "Calcite", "CaCO3", "carbonate",
        ["Chalk", "Calcium carbonate", "Whiting"],
        "Uhlicitan vapenaty",
        ["Uhlicitan vapenaty", "Vapenec", "Krida"],
    ),
    "Kaolin (White Clay)": (
        "Kaolinite", "Al2Si2O5(OH)4", "phyllosilicate_clay",
        ["Kaolin", "China clay", "White clay"],
        "Kaolin (bily jil)",
        ["Kaolin", "Bily jil", "Kaolinit"],
    ),
    "Bentonite": (
        "Bentonite (montmorillonite-rich)",
        "(Na,Ca)0.33(Al,Mg)2Si4O10(OH)2*nH2O", "phyllosilicate_clay",
        ["Montmorillonite clay", "Smectite clay"],
        "Bentonit",
        ["Bentonit", "Bentonitovy jil"],
    ),
    "Epsom Salt (Magnesium Sulfate)": (
        "Epsomite", "MgSO4*7H2O", "sulfate",
        ["Epsom salt", "Magnesium sulfate heptahydrate"],
        "Siran horecnaty (horka sul)",
        ["Siran horecnaty potravinarsky", "Siran horecnaty", "Horka sul"],
    ),
    "Activated Carbon": (
        "Activated carbon", "C", "carbonaceous", ["Activated charcoal"],
        "Aktivni uhli",
        ["Aktivni Uhli Hydrafin CC", "Aktivni uhli", "Aktivni uhli Hydrafin",
         "Hydrafin CC"],
    ),
    "Aluminum Sulfate": (
        "Aluminium sulfate", "Al2(SO4)3", "sulfate",
        ["Alum (aluminium sulfate)", "Aluminium sulphate"],
        "Siran hlinity (vlockovac)",
        ["Vlockovac- Siran hlinity", "Vlockovac siran hlinity",
         "Siran hlinity", "Vlockovac"],
    ),
    "Sodium Bicarbonate": (
        "Nahcolite", "NaHCO3", "carbonate",
        ["Baking soda", "Sodium hydrogen carbonate"],
        "Hydrogenuhlicitan sodny (jedla soda)",
        ["Hydrogenuhlicitan sodny", "Jedla soda", "Soda bicarbona"],
    ),
    "Green Clay (Illite)": (
        "Illite", "K0.65Al2(Al0.65Si3.35O10)(OH)2", "phyllosilicate_clay",
        ["Green clay", "Illitic clay"],
        "Jil zeleny (illit)",
        ["BIO Jil Zeleny", "Jil zeleny", "Zeleny jil", "Illit"],
    ),
    "Pink Clay": (
        "Pink clay (kaolinite-rich, Fe-bearing)", None,
        "phyllosilicate_clay", ["Rose clay"],
        "Jil ruzovy",
        ["BIO Jil Ruzovy", "Jil ruzovy", "Ruzovy jil"],
    ),
    "Red Clay": (
        "Red clay (Fe-bearing)", None, "phyllosilicate_clay",
        ["Red illite", "French red clay"],
        "Jil cerveny",
        ["BIO Jil Cerveny", "Jil cerveny", "Cerveny jil"],
    ),
    "Iron(II) Sulfate": (
        "Melanterite / ferrous sulfate", "FeSO4*7H2O", "sulfate",
        ["Ferrous sulfate", "Green vitriol", "Copperas"],
        "Siran zeleznaty (zelena skalice)",
        ["Siran zaleznaty, heptahydrat, zelena skalice",
         "Siran zeleznaty heptahydrat", "Siran zeleznaty", "Zelena skalice"],
    ),
    "Borax (Sodium Tetraborate)": (
        "Borax", "Na2B4O7*10H2O", "borate",
        ["Sodium tetraborate", "Tincal"],
        "Borax (tetraboritan sodny)",
        ["Borax", "Tetraboritan sodny", "Boritan sodny"],
    ),
    "Potassium Nitrate": (
        "Niter", "KNO3", "nitrate", ["Saltpetre", "Saltpeter", "Nitre"],
        "Dusicnan draselny",
        ["Dusicnan draselny Techincka", "Dusicnan draselny technicky",
         "Dusicnan draselny", "Ledek draselny"],
    ),
    "Talc": (
        "Talc", "Mg3Si4O10(OH)2", "phyllosilicate",
        ["Talcum", "Soapstone powder"],
        "Mastek (talek, steatit, klouzek)",
        ["Mastek KT, talek, steatit, klouzek", "Mastek KT", "Mastek",
         "Talek", "Steatit", "Klouzek"],
    ),
    "Copper(II) Sulfate": (
        "Chalcanthite", "CuSO4*5H2O", "sulfate",
        ["Cupric sulfate", "Blue vitriol", "Copper sulphate"],
        "Siran mednaty pentahydrat (modra skalice)",
        ["Siran mednat, pentahydrat", "Siran mednaty pentahydrat",
         "Siran mednat pentahydrat", "Siran mednaty", "Modra skalice"],
    ),
    "Ascorbic Acid": (
        "Ascorbic acid", "C6H8O6", "organic_acid", ["Vitamin C"],
        "Kyselina L-askorbova",
        ["Kyselinaa L-askorbova", "Kyselina L-askorbova",
         "Kyselina askorbova", "Vitamin C"],
    ),
    "Tartaric Acid": (
        "Tartaric acid", "C4H6O6", "organic_acid", [],
        "Kyselina vinna",
        ["Kyselina Vinna", "Kyselina vinna"],
    ),
    "Magnesium Carbonate": (
        "Magnesite / hydromagnesite", "MgCO3", "carbonate",
        ["Magnesia alba"],
        "Uhlicitan horecnaty (magnezit lehky)",
        ["Uhlicitan Horecnaty, Magnesit Lehky", "Uhlicitan horecnaty",
         "Magnezit lehky", "Magnesit lehky"],
    ),
    "Citric Acid": (
        "Citric acid", "C6H8O7", "organic_acid", [],
        "Kyselina citronova monohydrat",
        ["Kyselina cytronova monohydrat", "Kyselina citronova monohydrat",
         "Kyselina citronova"],
    ),
}


def material_id(name):
    """The stable id form of a display name. Matches what DB1 stored."""
    return (
        name.lower()
        .replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
        .replace(",", "")
    )


def identity(name):
    """
    The full identity record for one material.

    A material with no entry is returned with empty fields rather than
    refused: the builder's job is to record what was measured, and an
    unnamed substance that was genuinely measured still belongs in the
    database. The audit reports it.
    """
    canonical, formula, material_class, aliases_en, name_cs, aliases_cs = (
        IDENTITIES.get(name, (None, None, None, [], None, []))
    )

    # The English display name is the key, and the Czech name and every
    # container label join the alias list, so one lookup resolves either
    # language. Order is deliberate: English first, then the Czech
    # display name, then the bench labels.
    aliases = list(aliases_en)

    if name_cs and name_cs not in aliases:
        aliases.append(name_cs)

    for alias in aliases_cs:
        if alias not in aliases and alias != name:
            aliases.append(alias)

    return {
        "material_id": material_id(name),
        "display_name": name,
        "name_en": name,
        "name_cs": name_cs,
        "canonical_name": canonical,
        "chemical_formula": formula,
        "material_class": material_class,
        "aliases": aliases,
        "aliases_en": list(aliases_en),
        "aliases_cs": list(aliases_cs),
    }


def known_names():
    return sorted(IDENTITIES)


def resolve_label(label):
    """
    Which material a bench label names, or None.

    Used by the builders to check that a source snapshot's material
    headings are the containers the identity table knows about. Folds
    case, spacing and punctuation only - never edit distance, because a
    near-miss resolved automatically is how a spectrum ends up filed
    under the wrong substance.
    """
    folded = _fold(label)

    for name in IDENTITIES:
        record = identity(name)

        for candidate in [record["display_name"], record["name_cs"]] + \
                record["aliases"]:
            if candidate and _fold(candidate) == folded:
                return name

    return None


def _fold(text):
    keep = []

    for character in str(text).lower():
        if character.isalnum():
            keep.append(character)

    return "".join(keep)
