"""
The competition software surface, derived rather than asserted.

THE QUESTION

Starting at `rover_science_client.py`, which functions can actually run
during a competition? Everything else is support, offline tooling or
dead. Until that set is known, "mission coverage" is a number about a
set nobody has defined.

HOW

A static reachability walk over the AST. Every module in the mission
domains is parsed once; call sites are resolved by name against the
functions and methods defined in those modules, and the closure is
taken from the entry point.

WHAT IT IS HONEST ABOUT

Name resolution in a dynamic language is approximate, and the
approximation here is deliberately GENEROUS: a call to `.save()`
credits every `save` defined anywhere in the mission tree. That
over-approximates the reachable set, which is the safe direction - a
function wrongly called reachable gets audited unnecessarily, while
one wrongly called unreachable would be a gap nobody looks at.

Two things are therefore true of the output: everything it calls
UNREACHABLE really is unreachable by name, and everything it calls
reachable is a superset of the truth.

    py firmware/Tests/software/audit/call_graph.py
    py firmware/Tests/software/audit/call_graph.py --unreachable
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# BY NAME, NOT BY HOP COUNT. Every suite in this tree resolves
# firmware/ by walking up to the directory called it, because a file
# that moves one level changes a hop count silently and changes nothing
# about a name. The regression suite enforces it.
FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

# The mission domains. ESP32/ is excluded: it is reached over a wire,
# not by a call, and its surface is the command table instead.
DOMAINS = ("PC", "Science", "BD")

ENTRY_FILE = "PC/rover_science_client.py"
ENTRY_FUNCTIONS = ("main", "one_shot", "build_parser")


class Definition:
    def __init__(self, module, qualname, name, node, lineno):
        self.module = module
        self.qualname = qualname
        self.name = name
        self.node = node
        self.lineno = lineno
        self.calls = set()

    def __repr__(self):                                # pragma: no cover
        return "<{}>".format(self.qualname)


def mission_files():
    files = []

    for domain in DOMAINS:
        for path in sorted((FIRMWARE / domain).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue

            files.append(path)

    return files


def called_names(node):
    """
    Every name one function body could transfer control to.

    CALLS ARE NOT ENOUGH, and the menus are why. `screen.py` dispatches
    through a table:

        TOOLS_MENU = (..., ("6", "Clear Physical Slot", ...,
                            measure.menu_clear_slot), ...)
        handler = action
        handler(mission, status, view)

    The only call site is `handler(...)`. A walk that looked at calls
    alone therefore reported `measure.py` and `records.py` as entirely
    unreachable - eleven operator screens, including the measurement
    itself, missing from the mission surface. That is the exact
    direction of error this tool must not make.

    So a bare REFERENCE to a known function counts as well: passing it
    to a table, a callback or a decorator is how it gets called later.
    """
    names = set()

    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func

            if isinstance(func, ast.Name):
                names.add(func.id)

            elif isinstance(func, ast.Attribute):
                names.add(func.attr)

        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            names.add(child.id)

        elif isinstance(child, ast.Attribute) and isinstance(
                child.ctx, ast.Load):
            names.add(child.attr)

    return names


def collect():
    """Every function and method in the mission domains, with its calls."""
    definitions = {}
    by_name = {}
    module_level = {}

    for path in mission_files():
        module = path.relative_to(FIRMWARE).as_posix()

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))

        except (OSError, SyntaxError):                 # pragma: no cover
            continue

        def visit(node, prefix):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = "{}::{}{}".format(
                        module, prefix, child.name)
                    definition = Definition(
                        module, qualname, child.name, child, child.lineno)
                    definition.calls = called_names(child)
                    definitions[qualname] = definition
                    by_name.setdefault(child.name, []).append(definition)

                    # Nested defs and closures count too.
                    visit(child, "{}{}.".format(prefix, child.name))

                elif isinstance(child, ast.ClassDef):
                    visit(child, "{}{}.".format(prefix, child.name))

        visit(tree, "")

        # MODULE-LEVEL CODE IS REACHED BY IMPORTING THE MODULE, and it
        # is where the menu tables live. `TOOLS_MENU` names eleven
        # screen functions in a tuple evaluated at import; without this
        # pseudo-definition none of them is connected to anything.
        top_level = ast.Module(
            body=[
                child for child in tree.body
                if not isinstance(child, (ast.FunctionDef,
                                          ast.AsyncFunctionDef,
                                          ast.ClassDef))
            ],
            type_ignores=[],
        )

        qualname = "{}::<module>".format(module)
        definition = Definition(module, qualname, "<module>", top_level, 0)
        definition.calls = called_names(top_level)
        definitions[qualname] = definition
        module_level[module] = definition

    return definitions, by_name, module_level


def reachable_from(definitions, by_name, module_level):
    seeds = []

    for name in ENTRY_FUNCTIONS:
        for definition in by_name.get(name, []):
            if definition.module == ENTRY_FILE:
                seeds.append(definition)

    if ENTRY_FILE in module_level:
        seeds.append(module_level[ENTRY_FILE])

    seen = set()
    queue = list(seeds)

    while queue:
        current = queue.pop()

        if current.qualname in seen:
            continue

        seen.add(current.qualname)

        # Reaching any function in a module means the module was
        # imported, and importing it runs its top level.
        top = module_level.get(current.module)

        if top is not None and top.qualname not in seen:
            queue.append(top)

        for name in current.calls:
            for target in by_name.get(name, []):
                if target.qualname not in seen:
                    queue.append(target)

            # Constructing a class runs its __init__ and, for a context
            # manager, its __enter__/__exit__.
            for special in ("__init__", "__enter__", "__exit__"):
                for target in by_name.get(special, []):
                    holder = target.qualname.rsplit("::", 1)[1]

                    if holder.startswith(name + "."):
                        if target.qualname not in seen:
                            queue.append(target)

    return seen


def main(argv):
    definitions, by_name, module_level = collect()
    reached = reachable_from(definitions, by_name, module_level)

    unreachable = sorted(
        q for q in definitions if q not in reached
    )

    per_module = {}

    for qualname, definition in definitions.items():
        entry = per_module.setdefault(definition.module, [0, 0])
        entry[0] += 1

        if qualname in reached:
            entry[1] += 1

    print("=" * 72)
    print("  MISSION CALL GRAPH   (from {})".format(ENTRY_FILE))
    print("=" * 72)
    print()
    print("  {:<42} {:>9} {:>9}".format("module", "defined", "reachable"))
    print("  " + "-" * 62)

    for module in sorted(per_module):
        defined, reached_count = per_module[module]
        print("  {:<42} {:>9} {:>9}".format(module, defined, reached_count))

    print("  " + "-" * 62)
    print("  {:<42} {:>9} {:>9}".format(
        "total", len(definitions), len(reached)))
    print()
    print("  mission software surface : {} functions".format(len(reached)))
    print("  not reachable by name    : {} functions".format(
        len(unreachable)))
    print()

    if "--unreachable" in argv:
        print("-" * 72)
        print("  NOT REACHABLE FROM THE ENTRY POINT")
        print("-" * 72)

        current = None

        for qualname in unreachable:
            module = qualname.split("::")[0]

            if module != current:
                current = module
                print()
                print("  {}".format(module))

            print("      {}".format(qualname.split("::")[1]))

        print()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
