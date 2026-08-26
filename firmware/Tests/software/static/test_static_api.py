"""
Names, imports and call sites, checked mechanically across the tree.

WHY THIS SUITE EXISTS

Python resolves a name when the line runs. A branch that only runs when
a servo has answered is a branch whose spelling nobody has checked, and
three defects of exactly that shape reached the bench:

    mission.link.sync_load_slot(1)      AttributeError, carousel setup
    spectral_features.first_derivative  NameError, one representation
    from Science.decision import class_models   ImportError, whole file

None of them needed hardware to find. They needed somebody to read the
name. This suite reads every name in the repository on every run.

WHAT IT DOES NOT DO

It is not a type checker and does not try to be. It answers four
questions that have exact answers:

    does this name exist in any enclosing scope?
    does this module attribute exist on that module?
    does this file parse, and does it import?
    does this path constant match the case on disk?

Everything uncertain is left to the suites that execute code.
"""

import ast
import builtins
import importlib
import sys
import traceback
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support

checks = support.Checks("static-api")

FIRMWARE = support.FIRMWARE

BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__",
}

# MicroPython-only names the ESP32 tree may use without importing.
MICROPYTHON_NAMES = {"const", "micropython"}


def sources(*domains):
    found = []

    for domain in domains:
        found.extend(
            p for p in (FIRMWARE / domain).rglob("*.py")
            if "__pycache__" not in p.parts
        )

    return sorted(found)


ALL_DOMAINS = ("BD", "ESP32", "PC", "Science", "Tests", "research", "tools")


def relative(path):
    return path.relative_to(FIRMWARE).as_posix()


def parse(path):
    return ast.parse(path.read_text(encoding="utf-8-sig"),
                     filename=str(path))


# ======================================================================
checks.section("every source file parses")

parsed = {}
unparsed = []

for path in sources(*ALL_DOMAINS):
    try:
        parsed[path] = parse(path)

    except SyntaxError as error:
        unparsed.append("{}:{}: {}".format(
            relative(path), error.lineno, error.msg))

checks.equal(unparsed, [],
             "no source file has a latent syntax error - a file no test "
             "imports still ships to the board")
checks.ok(len(parsed) > 60,
          "and the check walked the whole tree ({} files)".format(
              len(parsed)))


# ======================================================================
checks.section("no name is used that is never bound")


class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.names = set()
        self.star = parent.star if parent else False


class NameChecker(ast.NodeVisitor):
    """
    A small pyflakes: flags Load of a name bound in no enclosing scope.

    Deliberately conservative. `import *`, `global` and `nonlocal` all
    widen a scope to "anything", and a scope that has been widened
    reports nothing rather than reporting noise.
    """

    def __init__(self):
        self.module = Scope()
        self.scopes = [self.module]
        self.pending = []
        self.problems = []

    def bind(self, name):
        if name:
            self.scopes[-1].names.add(name)

    def bind_target(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                self.bind(child.id)

    def visit_Import(self, node):
        for alias in node.names:
            self.bind(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == "*":
                self.scopes[-1].star = True

            else:
                self.bind(alias.asname or alias.name)

    def visit_Global(self, node):
        for name in node.names:
            self.bind(name)
            self.module.names.add(name)

    visit_Nonlocal = visit_Global

    def visit_Assign(self, node):
        self.visit(node.value)

        for target in node.targets:
            self.bind_target(target)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)

        self.bind_target(node.target)

    def visit_AugAssign(self, node):
        self.visit(node.value)
        self.bind_target(node.target)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.bind_target(node.target)

    def visit_For(self, node):
        self.visit(node.iter)
        self.bind_target(node.target)

        for child in node.body + node.orelse:
            self.visit(child)

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)

            if item.optional_vars:
                self.bind_target(item.optional_vars)

        for child in node.body:
            self.visit(child)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node):
        if node.type:
            self.visit(node.type)

        self.bind(node.name)

        for child in node.body:
            self.visit(child)

    def _arguments(self, args):
        every = (list(args.posonlyargs) + list(args.args)
                 + list(args.kwonlyargs))

        if args.vararg:
            every.append(args.vararg)

        if args.kwarg:
            every.append(args.kwarg)

        return every

    def _defaults(self, args):
        return list(args.defaults) + [d for d in args.kw_defaults if d]

    def _function(self, node):
        self.bind(node.name)

        for decorator in node.decorator_list:
            self.visit(decorator)

        for default in self._defaults(node.args):
            self.visit(default)

        self.scopes.append(Scope(self.scopes[-1]))

        for arg in self._arguments(node.args):
            self.bind(arg.arg)

        for child in node.body:
            self.visit(child)

        self.scopes.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node):
        for default in self._defaults(node.args):
            self.visit(default)

        self.scopes.append(Scope(self.scopes[-1]))

        for arg in self._arguments(node.args):
            self.bind(arg.arg)

        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node):
        self.bind(node.name)

        for decorator in node.decorator_list:
            self.visit(decorator)

        for base in node.bases:
            self.visit(base)

        self.scopes.append(Scope(self.scopes[-1]))

        for child in node.body:
            self.visit(child)

        self.scopes.pop()

    def _comprehension(self, node):
        self.scopes.append(Scope(self.scopes[-1]))

        for generator in node.generators:
            self.bind_target(generator.target)

        for generator in node.generators:
            self.visit(generator.iter)

            for condition in generator.ifs:
                self.visit(condition)

        for field in ("elt", "key", "value"):
            child = getattr(node, field, None)

            if isinstance(child, ast.AST):
                self.visit(child)

        self.scopes.pop()

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_GeneratorExp = _comprehension
    visit_DictComp = _comprehension

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.pending.append((node.id, node.lineno, list(self.scopes)))

        else:
            self.bind(node.id)

    def run(self, tree):
        # Module-level names are collected first: a function body may
        # legitimately use a global that is defined further down.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                self.module.names.add(node.name)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.module.names.add(
                        alias.asname or alias.name.split(".")[0])

            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        self.module.star = True

                    else:
                        self.module.names.add(alias.asname or alias.name)

        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign,
                                      ast.AugAssign, ast.Try, ast.If,
                                      ast.For, ast.While, ast.With)):
                for child in ast.walk(statement):
                    if isinstance(child, ast.Name) and not isinstance(
                            child.ctx, ast.Load):
                        self.module.names.add(child.id)

        for statement in tree.body:
            self.visit(statement)

        for name, line, scopes in self.pending:
            if name in BUILTIN_NAMES or name in MICROPYTHON_NAMES:
                continue

            if any(name in scope.names or scope.star
                   for scope in reversed(scopes)):
                continue

            self.problems.append((line, name))

        return self.problems


undefined = []

for path, tree in parsed.items():
    for line, name in NameChecker().run(tree):
        undefined.append("{}:{}: {}".format(relative(path), line, name))

checks.equal(sorted(undefined), [],
             "every name used is bound somewhere - this is the check "
             "that would have caught `spectral_features`")


# ======================================================================
checks.section("every module attribute reached actually exists")


def module_index():
    """Importable dotted name -> file, for the first-party tree."""
    found = {}

    for path in sources(*ALL_DOMAINS):
        parts = list(path.relative_to(FIRMWARE).with_suffix("").parts)

        if parts[-1] == "__init__":
            parts = parts[:-1]

        if not parts:
            continue

        found[".".join(parts)] = path

        # PC/, ESP32/ and tools/ go on sys.path directly, so their
        # modules are importable without the directory prefix.
        if parts[0] in ("PC", "ESP32", "tools", "Tests"):
            found.setdefault(".".join(parts[1:]) or parts[0], path)

    return found


MODULES = module_index()
TOP_LEVEL_CACHE = {}


def top_level_names(path):
    if path not in TOP_LEVEL_CACHE:
        names = set()

        for node in ast.walk(parsed[path]):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)

            elif isinstance(node, ast.Name) and not isinstance(
                    node.ctx, ast.Load):
                names.add(node.id)

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])

            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)

        TOP_LEVEL_CACHE[path] = names

    return TOP_LEVEL_CACHE[path]


def resolve(dotted, package_parts):
    for depth in range(len(package_parts), -1, -1):
        candidate = ".".join(list(package_parts[:depth]) + [dotted])

        if candidate in MODULES:
            return candidate

    return None


missing_attributes = []

for path, tree in parsed.items():
    package_parts = path.relative_to(FIRMWARE).parent.parts
    bound = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve(alias.name, package_parts)

                if target:
                    bound[alias.asname or alias.name] = target

        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                if alias.name == "*":
                    continue

                dotted = "{}.{}".format(node.module, alias.name) \
                    if node.module else alias.name
                target = resolve(dotted, package_parts)

                if target:
                    bound[alias.asname or alias.name] = target

    # A local binding of the same name shadows the module everywhere in
    # the file, which is legal and was in fact happening. Those files
    # are skipped rather than reported: the shadowing itself is caught
    # by test_architecture, and reporting it here would be noise.
    shadowed = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            if node.id in bound:
                shadowed.add(node.id)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (list(node.args.posonlyargs) + list(node.args.args)
                        + list(node.args.kwonlyargs)):
                if arg.arg in bound:
                    shadowed.add(arg.arg)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue

        if not isinstance(node.value, ast.Name):
            continue

        name = node.value.id

        if name in shadowed:
            continue

        target = bound.get(name)

        if not target:
            continue

        if node.attr.startswith("__"):
            continue

        if node.attr not in top_level_names(MODULES[target]):
            missing_attributes.append("{}:{}: {}.{} is not in {}".format(
                relative(path), node.lineno, name, node.attr, target))

checks.equal(sorted(missing_attributes), [],
             "every `module.name` reached exists in that module - this "
             "is the check that would have caught the wrong config")


# ======================================================================
checks.section("every host module imports")

# ESP32/ needs MicroPython's `machine`, and Tests/ suites run
# themselves on import. Both are covered by the suites that load them
# properly, so they are excluded here rather than half-imported.
SKIP_IMPORT_ROOTS = ("ESP32", "Tests")

support.add_project_root()
support.add_path("PC")
support.add_path("tools")

import_failures = []
imported = 0

for path in sources("BD", "PC", "Science", "research", "tools"):
    parts = list(path.relative_to(FIRMWARE).with_suffix("").parts)

    if parts[0] in SKIP_IMPORT_ROOTS:
        continue

    if parts[-1] == "__init__":
        parts = parts[:-1]

    dotted = ".".join(parts[1:] if parts[0] in ("PC", "tools") else parts)

    if not dotted:
        continue

    try:
        importlib.import_module(dotted)
        imported += 1

    except BaseException:                              # noqa: BLE001
        import_failures.append("{}: {}".format(
            dotted, traceback.format_exc().strip().splitlines()[-1]))

checks.equal(sorted(import_failures), [],
             "every host-side module imports cleanly - this is the check "
             "that would have caught the class_models ImportError")
checks.ok(imported > 50,
          "and it really imported them ({} modules)".format(imported))


# ======================================================================
checks.section("path constants match the case on disk")

# Windows does not care about case and Linux does. A constant spelled
# BD/db1 works on the development machine and is a FileNotFoundError on
# the main computer.

def spelled_on_disk(path):
    """None if absent, True if exact, or the real spelling if it differs."""
    parts = []
    current = Path(path)

    while current != current.parent:
        parts.append(current.name)
        current = current.parent

    walk = current

    for name in reversed(parts):
        if not walk.is_dir():
            return None

        try:
            entries = {entry.name for entry in walk.iterdir()}

        except OSError:
            return None

        if name not in entries:
            lowered = {entry.lower(): entry for entry in entries}

            if name.lower() in lowered:
                return lowered[name.lower()]

            return None

        walk = walk / name

    return True


case_faults = []
constants_checked = 0

for dotted in ("BD.config", "Science.config", "research.erc.config"):
    module = importlib.import_module(dotted)

    for name in dir(module):
        if name.startswith("_"):
            continue

        value = getattr(module, name)

        if not isinstance(value, Path):
            continue

        constants_checked += 1
        spelling = spelled_on_disk(value)

        if isinstance(spelling, str):
            case_faults.append("{}.{} = {} but disk says {!r}".format(
                dotted, name, value, spelling))

checks.equal(sorted(case_faults), [],
             "no path constant differs from the disk only by case")
checks.ok(constants_checked > 30,
          "and every Path constant in the three config modules was "
          "checked ({})".format(constants_checked))


# ======================================================================
checks.section("no mutable default argument")

# A shared list or dict default is a container that survives between
# calls. In a store or an accumulator it is silent data corruption.

mutable_defaults = []

for path, tree in parsed.items():
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        defaults = list(node.args.defaults) + [
            d for d in node.args.kw_defaults if d
        ]

        for default in defaults:
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                mutable_defaults.append("{}:{}: {}()".format(
                    relative(path), node.lineno, node.name))

checks.equal(sorted(mutable_defaults), [],
             "no function has a list, dict or set as a default argument")


# ======================================================================
checks.section("no bare except in production code")

# `except:` catches KeyboardInterrupt and SystemExit as well, so a
# Ctrl+C during a carousel movement can be swallowed by the recovery
# handler that was meant to catch a servo timeout.

bare = []

for path, tree in parsed.items():
    if path.relative_to(FIRMWARE).parts[0] == "Tests":
        continue

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            bare.append("{}:{}".format(relative(path), node.lineno))

checks.equal(sorted(bare), [],
             "production code never writes `except:` - it would swallow "
             "Ctrl+C during a carousel movement")


# ======================================================================
checks.section("every silent exception handler is one we have justified")

# `except Exception: pass` is where a programming error becomes
# legitimate-looking runtime data. The audit of 2026-08-24 walked all
# 215 handlers in mission code and found every silent one to be
# best-effort CLEANUP - releasing a port, clearing a buffer, switching
# a lamp off in a `finally` where the original error must survive.
#
# The list is pinned rather than the count, so adding a new silent
# handler fails here and has to be justified in the same commit that
# introduces it. That is the whole mechanism: not a ban, a gate.

MISSION_DOMAINS = ("PC", "Science", "BD", "ESP32")

JUSTIFIED_SILENT = {
    # Releasing the port must never raise: it runs in every finally,
    # including the ones unwinding a different error.
    "PC/serial_link.py:close",

    # Clearing the receive buffer after a reset is best effort; failing
    # to clear it is not a reason to fail the reset.
    "PC/serial_link.py:hard_reset",

    # Switching the lamps off after an illumination error that has
    # ALREADY been recorded. If this raised it would replace the
    # recorded diagnosis with an I2C error about the lamp.
    "ESP32/protocol.py:handle_led_test",
    "ESP32/protocol.py:handle_sensor_test_raw",

    # The same, in a `finally` beside a bare `raise`. This one matters
    # most: raising here would MASK the SensorError being re-raised,
    # and the operator would be told the lamp failed rather than why
    # the acquisition did. The lamp state is reported separately, and
    # read back from the device rather than assumed - see _bulbs_off.
    "ESP32/sensor.py:acquire_one",

    # The last-resort attempt to tell the host that a response could
    # not be sent. If even that fails there is nothing further to try,
    # and the serving loop must not die with it.
    "ESP32/protocol.py:serve_forever",

    # Returning a UART pin to input while releasing the bus. A pin that
    # will not reset is not a reason to fail the release.
    "ESP32/servo.py:release_uart_pins",
}


def enclosing_function(tree, node):
    """The def a node sits inside, or '<module>'."""
    best = None

    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef,
                                      ast.AsyncFunctionDef)):
            continue

        if candidate.lineno <= node.lineno and (
                best is None or candidate.lineno > best.lineno):
            end = getattr(candidate, "end_lineno", None)

            if end is None or node.lineno <= end:
                best = candidate

    return best.name if best else "<module>"


silent = []

for path, tree in parsed.items():
    domain = path.relative_to(FIRMWARE).parts[0]

    if domain not in MISSION_DOMAINS:
        continue

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        broad = node.type is None or (
            isinstance(node.type, ast.Name)
            and node.type.id in ("Exception", "BaseException")
        )

        if not broad:
            continue

        if not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)):
            continue

        silent.append("{}:{}".format(
            relative(path), enclosing_function(tree, node)))

unjustified = sorted(set(silent) - JUSTIFIED_SILENT)

checks.equal(unjustified, [],
             "no `except Exception: pass` outside the justified cleanup "
             "list - a new one is a new place a programming error can "
             "become plausible data")

checks.ok(len(silent) <= len(JUSTIFIED_SILENT) + 2,
          "and there are {} of them in total, all cleanup".format(
              len(silent)))


# ======================================================================
checks.section("a broad handler always leaves a trace")

# The other half of the same rule. A handler that catches Exception and
# neither re-raises, records, reports nor returns has swallowed
# something without telling anyone.

swallowed = []

for path, tree in parsed.items():
    domain = path.relative_to(FIRMWARE).parts[0]

    if domain not in MISSION_DOMAINS:
        continue

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        if not (isinstance(node.type, ast.Name)
                and node.type.id in ("Exception", "BaseException")):
            continue

        where = "{}:{}".format(relative(path),
                               enclosing_function(tree, node))

        if where in JUSTIFIED_SILENT:
            continue

        acts = False

        for child in ast.walk(node):
            if isinstance(child, (ast.Raise, ast.Assign, ast.AugAssign,
                                  ast.Return, ast.Call)):
                acts = True

                break

        if not acts:
            swallowed.append("{}:{}".format(relative(path), node.lineno))

checks.equal(sorted(swallowed), [],
             "every broad handler either re-raises, records the failure, "
             "returns a documented value or reports it")


# ======================================================================
checks.section("nothing imports a module that does not exist")

# `from BD.repositories.calibrations import ...` parses perfectly and
# is a stale path from an architecture that was renamed.

FIRST_PARTY_ROOTS = ("BD", "Science", "research", "workflow")

broken_imports = []

for path, tree in parsed.items():
    package_parts = path.relative_to(FIRMWARE).parent.parts

    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue

        if not node.module:
            continue

        root = node.module.split(".")[0]

        if root not in FIRST_PARTY_ROOTS:
            continue

        if resolve(node.module, package_parts):
            continue

        # `from BD import config` names a package, not a module.
        if node.module in ("BD", "Science", "research", "workflow"):
            continue

        broken_imports.append("{}:{}: {}".format(
            relative(path), node.lineno, node.module))

checks.equal(sorted(broken_imports), [],
             "every first-party module named in an import exists")


sys.exit(checks.report())
