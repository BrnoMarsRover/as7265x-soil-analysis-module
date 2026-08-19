"""
Material identity: the controlled vocabulary the whole system agrees on.

READS AN EXISTING TAXONOMY, INVENTS NOTHING. Every material in DB1 and
DB3 already carries `material_id`, `display_name`, `canonical_name`,
`chemical_formula`, `material_class` and `aliases` — including the Czech
names the operator uses at the bench ("Bentonit", "Mastek"). Adding a
second, hand-written classification beside that would guarantee the two
drift apart, and would mean asserting chemistry that was never checked.

So this module is a lookup and a normaliser, not a source of truth:

    resolve("bentonit")          -> the Bentonite identity
    resolve("Bentonite")         -> the same identity
    resolve("bentonite")         -> the same identity
    family_of("Talc")            -> "phyllosilicate"

A name that cannot be resolved is REFUSED rather than guessed. Guessing
here would attach a verified ground-truth label to the wrong material,
which is the one error the learning database can never recover from.

Layer rule: BD must never import Measurements or DecisionModel.
"""

import json

from BD import config
from BD.registry import DatabaseRegistry


class TaxonomyError(Exception):
    """A material identity is unknown or ambiguous."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def _normalize(text):
    """Fold a name to its comparison form: case and spacing only."""
    if text is None:
        return ""

    return " ".join(str(text).strip().lower().replace("_", " ").split())


class MaterialIdentity:
    """One material, as the databases already describe it."""

    def __init__(self, key, entry, source):
        self.key = key
        self.source = source

        self.material_id = entry.get("material_id") or _normalize(key).replace(
            " ", "_"
        )
        self.display_name = entry.get("display_name") or key
        self.canonical_name = entry.get("canonical_name")
        self.chemical_formula = entry.get("chemical_formula")
        self.family_id = entry.get("material_class")
        self.aliases = list(entry.get("aliases") or [])

    def names(self):
        """Every string that legitimately refers to this material."""
        candidates = [
            self.key, self.display_name, self.canonical_name,
            self.material_id,
        ]
        candidates.extend(self.aliases)

        return [name for name in candidates if name]

    def as_dict(self):
        return {
            "material_key": self.key,
            "material_id": self.material_id,
            "display_name": self.display_name,
            "canonical_name": self.canonical_name,
            "chemical_formula": self.chemical_formula,
            "family_id": self.family_id,
            "aliases": list(self.aliases),
            "source_database": self.source,
        }


class Taxonomy:
    """
    The union of the identities DB1 and DB3 already carry.

    DB1 wins a name collision, deliberately: it was measured on this
    instrument, and its identities were checked against the physical
    containers. DB3's identities come from an external catalogue and are
    kept, but they do not overwrite a measured one.
    """

    def __init__(self, registry=None):
        self.registry = registry or DatabaseRegistry()

        self.materials = {}
        self._by_name = {}
        self.collisions = []

        self.operator_aliases = {}
        self.unresolved_aliases = []

        # DB3 first so DB1 overwrites it, not the other way round.
        for key in ("DB3", "DB2", "DB1"):
            self._absorb(key)

        self._absorb_operator_aliases()

    def _absorb_operator_aliases(self):
        """
        Bench names for materials that already exist.

        An alias that does not resolve to a material in the libraries is
        RECORDED AND IGNORED, never created: a name is not a spectrum,
        and a typo must not become a new material with no data behind it.
        """
        try:
            with open(config.OPERATOR_ALIASES_FILE, "r",
                      encoding="utf-8") as handle:
                document = json.load(handle)

        except (OSError, ValueError):
            return

        for alias, target in (document.get("aliases") or {}).items():
            key = self._by_name.get(_normalize(target))

            if key is None:
                self.unresolved_aliases.append({
                    "alias": alias, "target": target,
                })

                continue

            self._by_name[_normalize(alias)] = key
            self.operator_aliases[alias] = key

    def _absorb(self, database_key):
        handle = self.registry.get(database_key)

        if handle is None or not handle.ready:
            return

        for name in sorted(handle.materials):
            entry = (handle.metadata or {}).get(name) or {}
            identity = MaterialIdentity(name, entry, database_key)

            self.materials[name] = identity

            for alias in identity.names():
                folded = _normalize(alias)

                if not folded:
                    continue

                existing = self._by_name.get(folded)

                if existing is not None and existing != name:
                    self.collisions.append({
                        "name": alias, "kept": name, "replaced": existing,
                    })

                self._by_name[folded] = name

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------

    def count(self):
        return len(self.materials)

    def get(self, name):
        """Resolve a name to an identity, or None."""
        key = self._by_name.get(_normalize(name))

        if key is None:
            return None

        return self.materials.get(key)

    def resolve(self, name):
        """
        Resolve or raise.

        Used on the ground-truth path, where a wrong answer is worse than
        no answer: a mistyped label becomes a verified training example
        for the wrong material and quietly poisons every model trained
        afterwards.
        """
        identity = self.get(name)

        if identity is None:
            raise TaxonomyError(
                "MATERIAL_UNKNOWN",
                "'{}' is not a known material name, id or alias. Ground "
                "truth is never guessed: add the material to the library "
                "first, or record the observation without a label."
                .format(name),
                {"name": name},
            )

        return identity

    def suggest(self, text, limit=8):
        """
        Candidate names for an operator prompt. Ranked, never auto-applied.

        Substring matching only. Anything cleverer - edit distance,
        phonetics - would put a plausible-looking wrong material at the
        top of the list, and the operator would press 1.
        """
        folded = _normalize(text)

        if not folded:
            return sorted(self.materials)[:limit]

        starts = []
        contains = []

        for alias, key in sorted(self._by_name.items()):
            if alias.startswith(folded):
                starts.append(key)
            elif folded in alias:
                contains.append(key)

        ordered = []

        for key in starts + contains:
            if key not in ordered:
                ordered.append(key)

        return ordered[:limit]

    def family_of(self, name):
        identity = self.get(name)

        return identity.family_id if identity else None

    def families(self):
        """Every family the libraries actually use, with its members."""
        grouped = {}

        for key, identity in sorted(self.materials.items()):
            if not identity.family_id:
                continue

            grouped.setdefault(identity.family_id, []).append(key)

        return grouped

    def status(self):
        families = self.families()

        return {
            "materials": self.count(),
            "families": len(families),
            "sources": sorted({
                identity.source for identity in self.materials.values()
            }),
            "name_collisions": len(self.collisions),
            "operator_aliases": len(self.operator_aliases),
            "unresolved_operator_aliases": len(self.unresolved_aliases),
            "note": "Read from the identities stored inside DB1/DB3. No "
                    "classification is defined here.",
        }
