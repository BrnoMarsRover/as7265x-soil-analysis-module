"""
Build DB3 from the USGS Spectral Library Version 7.

    py firmware/research/import_usgs.py --download
    py firmware/research/import_usgs.py

Reads  the splib07a ASCII archive (downloaded, not committed)
Writes firmware/BD/data/DB3.json

WHAT THIS DOES
--------------
Takes real laboratory reflectance spectra measured by the USGS, and
projects each one into the 18 AS7265x bands using the band-response model
in spectral_projection.py. Nothing is invented: every DB3 record carries
the USGS record number, sample identifier, instrument and wavelength range
it came from, and can be traced back to the archive by SHA256.

WHY USGS splib07
----------------
It is public domain (a U.S. Geological Survey data release), it is
measured rather than modelled, its samples are characterised by supporting
chemical analysis, and it covers the minerals a rover science task
actually cares about. Alternatives were considered: RELAB requires
registration, and the JPL/ECOSTRESS site serves its data through a
JavaScript application with no documented static endpoint.

INSTRUMENT CHOICE
-----------------
Only spectra measured on instruments whose range covers 410-940 nm are
usable:

    BECK    Beckman     0.2 - 3.0  um    yes
    ASD     FieldSpec   0.35 - 2.5 um    yes
    AVIRIS              0.37 - 2.5 um    yes
    NIC4    Nicolet     1.12 - 216 um    NO - starts beyond our red end

Deleted channels are marked -1.23e34 in the archive and are dropped
before projection, never interpolated across.
"""

import argparse
import collections
import hashlib
import json
import re
import ssl
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIRMWARE_ROOT = HERE.parent

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD import config                                   # noqa: E402
from BD.channels import AS7265X_18                      # noqa: E402
from research import spectral_projection as projection  # noqa: E402

# The ASCII data release, standard resolution. 21.8 MB.
# ScienceBase item 586e8c88e4b0f5ce109fccae, file ASCIIdata_splib07a.zip.
ARCHIVE_URL = (
    "https://www.sciencebase.gov/catalog/file/get/586e8c88e4b0f5ce109fccae"
    "?f=__disk__a7%2F4f%2F91%2Fa74f913e0b7d1b8123ad059e52506a02b75a2832"
)
ARCHIVE_NAME = "ASCIIdata_splib07a.zip"

# Cached outside the repository: 21 MB of third-party data does not belong
# in version control, and DB3 is rebuilt from it rather than committed
# alongside it.
CACHE = FIRMWARE_ROOT / "BD" / "data" / ".cache"
ARCHIVE = CACHE / ARCHIVE_NAME

SOURCE_DATASET = "USGS Spectral Library Version 7 (splib07a)"
SOURCE_CITATION = (
    "Kokaly, R.F., Clark, R.N., Swayze, G.A., Livo, K.E., Hoefen, T.M., "
    "Pearson, N.C., Wise, R.A., Benzel, W.M., Lowers, H.A., Driscoll, "
    "R.L., and Klein, A.J., 2017, USGS Spectral Library Version 7: "
    "U.S. Geological Survey Data Series 1035, 61 p., "
    "https://doi.org/10.3133/ds1035"
)
SOURCE_LICENSE = "Public domain (U.S. Geological Survey data release)"
SOURCE_DOI = "https://doi.org/10.3133/ds1035"

# The archive's no-data marker.
DELETED = -1.0e34

# Instruments whose wavelength range covers the AS7265x bands, with the
# wavelength file each one's spectra are sampled on.
INSTRUMENTS = {
    "BECKa": "Wavelengths_BECK",
    "ASDFRa": "Wavelengths_ASD",
    "ASDHRa": "Wavelengths_ASD",
    "ASDNGa": "Wavelengths_ASD",
    "AVIRISa": "Wavelengths_AVIRIS",
}

# ----------------------------------------------------------------------
# What DB3 is for: identifying what a rover might actually be looking at.
#
# Grouped by the question each group answers. Ordering inside a group is
# preference - the importer takes the best-covered spectrum per group and
# moves on, so a group with several samples contributes one entry unless
# `variants` asks for more.
# ----------------------------------------------------------------------
WANTED = [
    # ==================================================================
    # MARS / PLANETARY - the minerals a rover is actually looking for
    # ==================================================================

    # --- iron oxides: why Mars is red ---------------------------------
    ("Hematite_GDS27", "Hematite", "iron_oxide", "Fe2O3", 1),
    ("Goethite_WS222", "Goethite", "iron_oxide", "FeO(OH)", 2),

    # --- sulfates: aqueous alteration, Meridiani and Gale -------------
    # Jarosite is the classic acidic-aqueous indicator. K and Na
    # end-members plus natural samples: the group genuinely varies, and
    # DB3 should show that rather than average it away.
    ("Jarosite_GDS99", "Jarosite", "sulfate", "KFe3(SO4)2(OH)6", 1),
    ("Jarosite_GDS101", "Jarosite", "sulfate", "NaFe3(SO4)2(OH)6", 1),
    ("Jarosite_GDS635", "Jarosite", "sulfate", None, 1),
    ("Ammonio-Jarosite", "Jarosite", "sulfate", None, 1),
    ("Gypsum_SU2202", "Gypsum", "sulfate", "CaSO4*2H2O", 1),
    ("Gypsum_HS333", "Gypsum", "sulfate", "CaSO4*2H2O", 1),
    ("Anhydrite_GDS42", "Anhydrite", "sulfate", "CaSO4", 1),
    ("Bassanite_GDS145", "Bassanite", "sulfate", "CaSO4*0.5H2O", 1),
    ("Alunite_HS295", "Alunite", "sulfate", "KAl3(SO4)2(OH)6", 1),
    ("Alunite_GDS84", "Alunite", "sulfate", None, 1),
    ("Mirabilite_GDS150", "Mirabilite", "sulfate", "Na2SO4*10H2O", 1),
    ("Polyhalite", "Polyhalite", "sulfate", None, 1),
    ("Bloedite_GDS147", "Bloedite", "sulfate", "Na2Mg(SO4)2*4H2O", 1),
    ("Butlerite_GDS25", "Butlerite", "sulfate", "Fe(SO4)(OH)*2H2O", 1),
    ("Syngenite_GDS139", "Syngenite", "sulfate", None, 1),

    # --- phyllosilicates: the record of water -------------------------
    ("Kaolinite_CM3", "Kaolinite", "phyllosilicate_clay",
     "Al2Si2O5(OH)4", 1),
    ("Dickite_NMNH46967", "Dickite", "phyllosilicate_clay",
     "Al2Si2O5(OH)4", 1),
    ("Halloysite_NMNH106236", "Halloysite", "phyllosilicate_clay",
     "Al2Si2O5(OH)4", 1),
    ("Illite_IMt-1", "Illite", "phyllosilicate_clay", None, 1),
    ("Nontronite_SWa-1", "Nontronite", "phyllosilicate_clay", None, 1),
    ("Chlorite_SMR-13", "Chlorite", "phyllosilicate_clay", None, 2),
    ("Clinochlore_Fe_GDS157", "Clinochlore", "phyllosilicate_clay",
     None, 1),
    ("Prochlorite_SMR-14", "Prochlorite", "phyllosilicate_clay", None, 1),
    ("Thuringite_SMR-15", "Thuringite", "phyllosilicate_clay", None, 1),
    ("Sepiolite_SepNev-1", "Sepiolite", "phyllosilicate_clay", None, 1),
    ("Vermiculite_GDS13", "Vermiculite", "phyllosilicate_clay", None, 1),
    ("Pyrophyllite_PYS1A", "Pyrophyllite", "phyllosilicate", None, 1),

    # --- carbonates: habitability and CO2 history ---------------------
    ("Calcite_WS272", "Calcite", "carbonate", "CaCO3", 1),
    ("Siderite_HS271", "Siderite", "carbonate", "FeCO3", 1),
    ("Witherite_HS273", "Witherite", "carbonate", "BaCO3", 1),
    ("Trona_GDS148", "Trona", "carbonate", "Na3(CO3)(HCO3)*2H2O", 1),
    ("Sodium_Bicarbonate_GDS55", "Nahcolite", "carbonate", "NaHCO3", 1),

    # --- mafic minerals: basaltic crust, lunar and martian ------------
    ("Olivine_NMNH137044", "Olivine", "silicate_mafic",
     "(Mg,Fe)2SiO4", 2),
    ("Forsterite_REE_AZ-01", "Forsterite", "silicate_mafic",
     "Mg2SiO4", 1),
    ("Acmite_NMNH133746", "Acmite (pyroxene)", "silicate_mafic",
     "NaFeSi2O6", 1),
    ("Riebeckite_NMNH122689", "Riebeckite (amphibole)",
     "silicate_mafic", None, 1),

    # --- feldspars and silica: highlands, anorthosite -----------------
    ("Anorthite_HS201", "Anorthite", "feldspar", "CaAl2Si2O8", 1),
    ("Anorthite_GDS28", "Anorthite", "feldspar", "CaAl2Si2O8", 1),
    ("Sanidine_GDS19", "Sanidine", "feldspar", "KAlSi3O8", 1),
    ("Quartz_GDS31", "Quartz", "silica", "SiO2", 1),
    ("Chalcedony_CU91-6A", "Chalcedony", "silica", "SiO2", 1),
    ("Chert_ANP90-6D", "Chert", "silica", "SiO2", 1),
    ("Opal_WS732", "Opal", "silica", "SiO2*nH2O", 1),

    # --- serpentine group ---------------------------------------------
    ("Lizardite_NMNHR4687", "Lizardite", "serpentine", None, 2),
    ("Chrysotile_HS323", "Chrysotile", "serpentine", None, 1),
    ("Talc_GDS23", "Talc", "phyllosilicate", "Mg3Si4O10(OH)2", 1),

    # --- micas ---------------------------------------------------------
    ("Muscovite_GDS107", "Muscovite", "mica", None, 1),
    ("Biotite_HS28", "Biotite", "mica", None, 1),
    ("Phlogopite_HS23", "Phlogopite", "mica", None, 1),
    ("Paragonite_GDS109", "Paragonite", "mica", None, 1),

    # --- evaporites, salts, borates: playas and crater basins ---------
    ("Halite_HS433", "Halite", "halide", "NaCl", 1),
    ("Carnallite_HS430", "Carnallite", "halide", "KMgCl3*6H2O", 1),
    ("Kainite_NMNH83904", "Kainite", "halide", None, 1),
    ("Niter_GDS43", "Niter", "nitrate", "KNO3", 1),
    ("Ulexite_GDS138", "Ulexite", "borate", "NaCaB5O6(OH)6*5H2O", 1),
    ("Tincalconite_GDS142", "Tincalconite", "borate", "Na2B4O7*5H2O", 1),
    ("Howlite_GDS155", "Howlite", "borate", None, 1),

    # --- native elements and carbon ------------------------------------
    ("Sulfur_GDS94", "Sulfur", "native_element", "S8", 1),
    ("Carbon_Black_GDS68", "Carbon black", "carbonaceous", "C", 1),

    # --- phosphates, zeolites, accessory -------------------------------
    ("Fluorapatite_WS416", "Fluorapatite", "phosphate", None, 1),
    ("Analcime_GDS1", "Analcime", "zeolite", None, 1),
    ("Heulandite_GDS3", "Heulandite", "zeolite", None, 1),
    ("Prehnite_GDS613", "Prehnite", "silicate", None, 1),
    ("Portlandite_GDS525", "Portlandite", "hydroxide", "Ca(OH)2", 1),

    # ==================================================================
    # ROCKS, SOILS AND MIXTURES - what a real surface looks like
    #
    # A rover does not point at a pure mineral. These are whole rocks,
    # sands and characterised mixtures, including low-abundance ones that
    # show how little iron oxide it takes to dominate a spectrum.
    # ==================================================================
    ("Pyroxene_Basalt_CU01-20A", "Basalt", "rock_igneous", None, 1),
    ("Jarosite_Rhyolite_CU91-20A", "Rhyolite (jarositic)",
     "rock_igneous", None, 1),
    ("Limestone_CU02-11A", "Limestone", "rock_sedimentary", None, 1),
    ("Sand_GrndIsle1", "Sand", "regolith_analogue", None, 1),
    ("Sand_DWO-3-DEL2a", "Sand", "regolith_analogue", None, 1),
    ("Stonewall_Playa_CU93-52A", "Playa evaporite crust",
     "regolith_analogue", None, 1),
    ("Stonewall_Playa_Dry_Mud", "Playa dry mud", "regolith_analogue",
     None, 1),
    ("Hematite.02+Quartz.98_GDS76", "Hematite 2% in quartz",
     "mixture_characterised", None, 1),
    ("Goethite0.02+Quartz_GDS240", "Goethite 2% in quartz",
     "mixture_characterised", None, 1),
    ("Calcite.80+Mont_Swy-1_GDS212", "Calcite 80% + montmorillonite",
     "mixture_characterised", None, 1),
    ("Calcite.80wt+Kaol_CM9_GDS213", "Calcite 80% + kaolinite",
     "mixture_characterised", None, 1),
    ("Chlor+Goethite_CU93-4B", "Chlorite + goethite",
     "mixture_characterised", None, 1),
    ("Magnesite+Hydromag_HS47", "Magnesite + hydromagnesite",
     "carbonate", "MgCO3", 1),
    ("Phlogopite_Sand_Mix_BR93-20", "Phlogopite sand mixture",
     "mixture_characterised", None, 1),
]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def download():
    CACHE.mkdir(parents=True, exist_ok=True)

    print("downloading {} ...".format(ARCHIVE_NAME))

    request = urllib.request.Request(
        ARCHIVE_URL, headers={"User-Agent": "Mozilla/5.0"}
    )
    data = urllib.request.urlopen(
        request, timeout=600, context=ssl.create_default_context()
    ).read()

    ARCHIVE.write_bytes(data)

    print("  {:,} bytes, sha256 {}".format(len(data), sha256_bytes(data)))


def read_values(archive, name):
    """One column of floats from a splib07 ASCII file (line 0 is a header)."""
    lines = archive.read(name).decode("utf-8", "replace").split("\n")
    values = []

    for line in lines[1:]:
        line = line.strip()

        if not line:
            continue

        try:
            values.append(float(line))
        except ValueError:
            continue

    return values, lines[0].strip()


def load_wavelength_grids(archive):
    """Wavelength axis per instrument family, converted from microns to nm."""
    grids = {}

    for name in archive.namelist():
        base = name.split("/")[-1]

        for key in ("Wavelengths_BECK", "Wavelengths_ASD",
                    "Wavelengths_AVIRIS"):
            if key in base:
                microns, _header = read_values(archive, name)
                grids[key] = [value * 1000.0 for value in microns]

    return grids


def usable_spectra(archive):
    """Every AREF spectrum on an instrument that covers our bands."""
    pattern = re.compile(
        r"splib07a_(.+?)_({})_AREF".format("|".join(INSTRUMENTS))
    )
    found = []

    for name in archive.namelist():
        if not name.endswith(".txt") or "errorbars" in name:
            continue

        match = pattern.search(name.split("/")[-1])

        if match:
            found.append((match.group(1), match.group(2), name))

    return found


def clean(wavelengths, reflectances):
    """
    Drop deleted channels and enforce a strictly increasing axis.

    Deleted values are the archive's -1.23e34 marker. They are removed,
    never interpolated across: a gap in the source is a gap, and the
    projection's coverage check is what decides whether the remaining
    data still supports a band.
    """
    pairs = []

    for wavelength, reflectance in zip(wavelengths, reflectances):
        if reflectance <= DELETED or reflectance != reflectance:
            continue

        if reflectance < -1.0 or reflectance > 2.0:
            continue

        if pairs and wavelength <= pairs[-1][0]:
            continue

        pairs.append((wavelength, reflectance))

    return [p[0] for p in pairs], [p[1] for p in pairs]


def build():
    if not ARCHIVE.exists():
        print("archive not cached; run with --download first")

        return 1

    raw = ARCHIVE.read_bytes()
    archive_hash = sha256_bytes(raw)
    archive = zipfile.ZipFile(ARCHIVE)

    grids = load_wavelength_grids(archive)
    catalogue = usable_spectra(archive)

    print("archive : {} ({:,} bytes)".format(ARCHIVE_NAME, len(raw)))
    print("sha256  : {}".format(archive_hash))
    print("usable  : {} spectra on covering instruments".format(
        len(catalogue)))
    print()

    materials = {}
    rejected = []
    per_group = collections.Counter()

    for pattern_name, group, material_class, formula, variants in WANTED:
        candidates = [
            entry for entry in catalogue
            if pattern_name.lower() in entry[0].lower()
        ]

        if not candidates:
            rejected.append((pattern_name, "not present in splib07a"))
            continue

        accepted_here = 0

        for sample, instrument, path in sorted(candidates):
            if accepted_here >= variants:
                break

            grid = grids.get(INSTRUMENTS[instrument])

            if not grid:
                continue

            values, header = read_values(archive, path)

            if len(values) != len(grid):
                continue

            wavelengths, reflectances = clean(grid, values)

            if len(wavelengths) < 50:
                continue

            record_match = re.search(r"Record=(\d+)", header)
            record_id = (
                record_match.group(1) if record_match else path.split("/")[-1]
            )

            provenance = {
                "source_dataset": SOURCE_DATASET,
                "source_record_id": "splib07a Record={}".format(record_id),
                "source_sample": sample.replace("_", " "),
                "source_file": path.split("/")[-1],
                "source_archive_sha256": archive_hash,
                "source_instrument": instrument,
                "source_wavelength_range_nm": [
                    round(wavelengths[0], 1), round(wavelengths[-1], 1)
                ],
                "source_points": len(wavelengths),
                "reflectance_convention": "AREF (absolute reflectance)",
                "license": SOURCE_LICENSE,
                "citation": SOURCE_CITATION,
                "doi": SOURCE_DOI,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                record = projection.build_record(
                    sample.replace("_", " "), wavelengths, reflectances,
                    provenance,
                )

            except projection.ProjectionError as error:
                rejected.append((sample, error.code))
                continue

            record["canonical_name"] = group
            record["material_class"] = material_class
            record["chemical_formula"] = formula

            name = sample.replace("_", " ")
            materials[name] = record
            per_group[group] += 1
            accepted_here += 1

        if accepted_here == 0 and candidates:
            rejected.append((pattern_name, "no candidate passed coverage"))

    document = {
        "database_id": "DB3",
        "database_version": "reference-v1",
        "schema_version": 1,
        "measurement_type": "REFERENCE_PROJECTED",
        "feature_space": AS7265X_18,
        "status": "READY" if materials else "EMPTY",
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "description": "External laboratory reflectance spectra from the "
                       "USGS Spectral Library Version 7, projected into the "
                       "18 AS7265x bands. Measured elsewhere, on other "
                       "instruments, on characterised samples - never "
                       "measured on this instrument.",

        "source": {
            "dataset": SOURCE_DATASET,
            "doi": SOURCE_DOI,
            "license": SOURCE_LICENSE,
            "citation": SOURCE_CITATION,
            "archive": ARCHIVE_NAME,
            "archive_sha256": archive_hash,
            "redistribution": "Public domain, so the derived projection is "
                              "committed. The 21 MB source archive is NOT "
                              "committed; rebuild with --download.",
            "rebuild": "py firmware/research/import_usgs.py --download",
        },

        "projection": {
            "equation": "band_i = integral(R(lam)*S_i(lam) dlam) / "
                        "integral(S_i(lam) dlam)",
            "model": projection.PROJECTION_MODEL_VERSION,
            "method": "GAUSSIAN_APPROXIMATION",
            "approximate": True,
            "response_source": "NOMINAL_CENTRE_AND_FWHM",
            "nominal_fwhm_nm": projection.NOMINAL_FWHM_NM,
            "warning": "The band response is a Gaussian on the nominal "
                       "centre wavelength, NOT the manufacturer's measured "
                       "response curve. Replace channel_response() in "
                       "spectral_projection.py and regenerate when real "
                       "curves are obtained.",
            "extrapolation": "NONE - bands outside a source's measured "
                             "range are omitted, never estimated",
        },

        "limitations": [
            "These are laboratory spectra of characterised samples, "
            "measured under controlled geometry. A rover measures a "
            "natural surface with unknown grain size, packing and "
            "roughness; a good match indicates spectral resemblance, not "
            "identity.",
            "Projection into 18 broad bands discards every diagnostic "
            "feature narrower than the band spacing, and everything "
            "outside 410-940 nm. Minerals separable in the source library "
            "may be inseparable here - see the collision analysis.",
        ],

        "material_count": len(materials),
        "materials": materials,
    }

    config.DB3_FILE.write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )

    print("imported {} spectra across {} material groups".format(
        len(materials), len(per_group)))
    print()

    by_class = collections.Counter(
        record["material_class"] for record in materials.values()
    )

    for material_class, count in sorted(by_class.items()):
        print("  {:<24} {}".format(material_class, count))

    if rejected:
        print()
        print("not imported:")

        for name, reason in rejected:
            print("  {:<24} {}".format(name, reason))

    print()
    print("wrote {}".format(config.DB3_FILE.name))

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()

    if args.download:
        download()

    return build()


if __name__ == "__main__":
    sys.exit(main())
