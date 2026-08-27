"""
Every hardware test, in one place, with its layer gates.

WHY A REGISTRY AND NOT A FOLDER OF SCRIPTS

The previous shape of this campaign was a list of stage functions in one
2000-line file. That works until you need to answer three questions the
project actually asks:

    which tests are ready to run right now, and which are blocked?
    what has to pass before this test's result means anything?
    where is the evidence for the claim in the report?

A registry answers all three by construction. Registration validates the
definition, so a test with no expected result cannot enter the campaign;
the prerequisite graph is checked for cycles at import time, so a gate
that can never open is a startup error rather than a mystery at the
bench; and every ID is unique, so a result can be traced to exactly one
procedure.

IMPORTING THIS MODULE TOUCHES NO HARDWARE. It imports the campaign
modules, which build definitions out of plain data and function objects.
Nothing is called, no port is opened, no adapter is constructed.
"""

from . import requirements as requirements_module
from .model import Campaign, TestDefinition


class RegistryError(Exception):
    """A structural fault in the campaign catalogue itself."""


class Registry:
    """The catalogue. One instance, built at import time."""

    def __init__(self):
        self._tests = {}
        self._campaigns = {}
        self._order = []

    # ------------------------------------------------------------------
    # building
    # ------------------------------------------------------------------

    def add_campaign(self, campaign):
        if campaign.campaign_id in self._campaigns:
            raise RegistryError(
                "duplicate campaign id {!r}".format(campaign.campaign_id))

        self._campaigns[campaign.campaign_id] = campaign

        return campaign

    def campaign(self, campaign_id, layer, title, purpose,
                 prerequisites=(), gate_note=""):
        return self.add_campaign(Campaign(
            campaign_id, layer, title, purpose, prerequisites, gate_note
        ))

    def add(self, definition):
        """
        Register one test. Raises rather than warns.

        A duplicate ID is refused because two procedures sharing an ID
        makes every result that quotes it ambiguous, and an unknown
        campaign is refused because a test outside the layer structure
        has no gate above or below it.
        """
        if not isinstance(definition, TestDefinition):
            raise RegistryError("not a TestDefinition: {!r}".format(
                definition))

        if definition.test_id in self._tests:
            raise RegistryError(
                "duplicate test id {!r}".format(definition.test_id))

        if definition.campaign not in self._campaigns:
            raise RegistryError(
                "{} belongs to unknown campaign {!r}".format(
                    definition.test_id, definition.campaign))

        self._tests[definition.test_id] = definition
        self._order.append(definition.test_id)

        return definition

    def test(self, **fields):
        """Build and register in one call; the campaign modules use this."""
        return self.add(TestDefinition(**fields))

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def __len__(self):
        return len(self._tests)

    def __contains__(self, test_id):
        return test_id in self._tests

    def all_tests(self):
        """Registration order, which is layer order. Never alphabetical."""
        return [self._tests[test_id] for test_id in self._order]

    def all_campaigns(self):
        return list(self._campaigns.values())

    def get(self, test_id):
        try:
            return self._tests[test_id]

        except KeyError:
            raise RegistryError("no such test: {}".format(test_id))

    def get_campaign(self, campaign_id):
        try:
            return self._campaigns[campaign_id]

        except KeyError:
            raise RegistryError("no such campaign: {}".format(campaign_id))

    def tests_in(self, campaign_id):
        self.get_campaign(campaign_id)          # raises if unknown

        return [t for t in self.all_tests() if t.campaign == campaign_id]

    def select(self, names):
        """
        Resolve a mixed list of test ids and campaign ids, in layer order.

        Case-insensitive, because an operator reading HW-B3-001 off a
        printed procedure at the bench should not be defeated by it.
        """
        wanted = []

        for name in names:
            key = str(name).strip()
            upper = key.upper()

            if upper in self._campaigns:
                wanted.extend(t.test_id for t in self.tests_in(upper))

            elif upper in self._tests:
                wanted.append(upper)

            else:
                raise RegistryError(
                    "{!r} is neither a test id nor a campaign id. Run "
                    "--list to see both.".format(name))

        seen = set()
        ordered = []

        for test_id in self._order:
            if test_id in wanted and test_id not in seen:
                seen.add(test_id)
                ordered.append(self._tests[test_id])

        return ordered

    # ------------------------------------------------------------------
    # the gate graph
    # ------------------------------------------------------------------

    def resolve_prerequisites(self, definition):
        """
        Expand a test's prerequisites into concrete test ids.

        A campaign id as a prerequisite means "every test in that
        campaign", which is what makes "B5 is gated by B3" expressible
        in one line without listing B3's contents twice.
        """
        found = []

        for name in definition.prerequisites:
            if name in self._campaigns:
                found.extend(t.test_id for t in self.tests_in(name))

            elif name in self._tests:
                found.append(name)

            else:
                raise RegistryError(
                    "{} names unknown prerequisite {!r}".format(
                        definition.test_id, name))

        # A test cannot gate itself, directly or through its campaign.
        return [t for t in found if t != definition.test_id]

    def unknown_prerequisites(self):
        """Every prerequisite that names nothing. Empty is the only pass."""
        missing = []

        for definition in self.all_tests():
            for name in definition.prerequisites:
                if name not in self._tests and name not in self._campaigns:
                    missing.append((definition.test_id, name))

        return missing

    def cycles(self):
        """
        Every prerequisite cycle, as the path that closes it.

        Depth-first with an explicit stack rather than recursion: the
        catalogue is small, but a cycle is exactly the input that turns
        a recursive walk into a RecursionError instead of a diagnosis.
        """
        found = []
        permanent = set()

        def walk(start):
            stack = [(start, iter(self._edges(start)))]
            path = [start]
            on_path = {start}

            while stack:
                node, children = stack[-1]

                try:
                    child = next(children)

                except StopIteration:
                    stack.pop()
                    permanent.add(node)
                    on_path.discard(path.pop())

                    continue

                if child in on_path:
                    cycle = path[path.index(child):] + [child]

                    if cycle not in found:
                        found.append(cycle)

                    continue

                if child in permanent:
                    continue

                path.append(child)
                on_path.add(child)
                stack.append((child, iter(self._edges(child))))

        for definition in self.all_tests():
            if definition.test_id not in permanent:
                walk(definition.test_id)

        return found

    def _edges(self, test_id):
        definition = self._tests.get(test_id)

        if definition is None:
            return []

        edges = []

        for name in definition.prerequisites:
            if name in self._campaigns:
                edges.extend(
                    t.test_id for t in self.tests_in(name)
                    if t.test_id != test_id
                )

            elif name in self._tests and name != test_id:
                edges.append(name)

        return edges

    def check(self):
        """
        Every structural fault in the whole catalogue, as sentences.

        Called by the CLI before it does anything and by the offline
        tests as an assertion. An empty list is the only acceptable
        result; the CLI refuses to run otherwise, because a campaign
        with a broken gate graph cannot be trusted to gate anything.
        """
        problems = []

        for definition in self.all_tests():
            problems.extend(
                "{}: {}".format(definition.test_id, p)
                for p in definition.problems()
            )

        for test_id, name in self.unknown_prerequisites():
            problems.append(
                "{}: prerequisite {!r} names no test and no "
                "campaign".format(test_id, name))

        for cycle in self.cycles():
            problems.append(
                "prerequisite cycle: {}".format(" -> ".join(cycle)))

        for campaign in self.all_campaigns():
            for name in campaign.prerequisites:
                if name not in self._campaigns:
                    problems.append(
                        "campaign {}: prerequisite {!r} is not a "
                        "campaign".format(campaign.campaign_id, name))

        problems.extend(self.traceability_problems())

        return problems

    # ------------------------------------------------------------------
    # traceability
    # ------------------------------------------------------------------

    def traceability_problems(self):
        """
        Every break in the requirement <-> test correspondence.

        Both directions matter and they fail differently. A test with a
        requirement that names nothing is a typo nobody would notice; a
        requirement with no test is a promise the campaign never keeps.
        """
        problems = []

        claimed = set()

        for definition in self.all_tests():
            unknown = requirements_module.unknown(definition.requirements)

            for name in unknown:
                problems.append(
                    "{}: requirement {!r} names nothing in "
                    "core/requirements.py".format(
                        definition.test_id, name))

            claimed.update(
                name for name in definition.requirements
                if name not in unknown)

        for requirement in requirements_module.all_requirements():
            if requirement.verified_by != (
                    requirements_module.VerifiedBy.HARDWARE_TEST):
                # Established by the offline suite, which is the only
                # place it CAN be established. `test_traceability.py`
                # checks those separately.
                continue

            if requirement.requirement_id not in claimed:
                problems.append(
                    "requirement {} has no test - it is a promise the "
                    "campaign never keeps".format(
                        requirement.requirement_id))

        return problems

    def requirement_to_tests(self):
        """requirement id -> the tests that are evidence for it."""
        mapping = {
            requirement.requirement_id: []
            for requirement in requirements_module.all_requirements()
        }

        for definition in self.all_tests():
            for name in definition.requirements:
                mapping.setdefault(name, []).append(definition.test_id)

        return mapping

    def test_to_requirements(self):
        """test id -> the requirements it is evidence for."""
        return {
            definition.test_id: list(definition.requirements)
            for definition in self.all_tests()
        }


# The one registry. Campaign modules import it and register into it.
REGISTRY = Registry()


_LOADED = False


def load():
    """
    Import every campaign module exactly once, and validate the result.

    Import-time validation is deliberate: a malformed test definition
    should stop the CLI before it prints a list that an operator might
    act on, not halfway through a run at the bench.
    """
    global _LOADED

    if _LOADED:
        return REGISTRY

    from .. import campaigns                          # noqa: F401

    campaigns.load_all(REGISTRY)

    problems = REGISTRY.check()

    if problems:
        raise RegistryError(
            "the hardware test catalogue is not valid:\n  - {}".format(
                "\n  - ".join(problems)))

    _LOADED = True

    return REGISTRY
