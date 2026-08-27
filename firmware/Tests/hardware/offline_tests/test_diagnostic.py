"""
The test-side diagnostic agent: its safety properties, and its adapter.

TWO KINDS OF CHECK, AND THEY ARE DELIBERATELY DIFFERENT

The ADAPTER is exercised against a deterministic fake agent, so the
whole PC-side path - handshake, whitelist, bounds, byte interpretation -
is proven before anything is flashed.

The AGENT ITSELF cannot be imported here: it runs on MicroPython and
imports `machine`, `config`, `sensor` and `servo`. So its safety
properties are checked by PARSING ITS SOURCE. That is weaker than
running it and it is the strongest thing available, and it is far better
than "we will find out when we deploy it" for a build whose entire
justification is that it is safe.

WHAT THE SOURCE CHECKS PROVE

    it has no movement command at all
    it writes nothing except the lamps-off command
    its register whitelist matches the adapter's
    it does not run at import, and has no __main__ guard
    it reuses the production drivers rather than reimplementing them
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.adapters.diagnostic import (                   # noqa: E402
    AGENT_PROTOCOL, AGENT_PROTOCOL_VERSION, COMMANDS,
    MAX_READ_LENGTH, READABLE_REGISTERS, DiagnosticAdapter)
from hardware.core.model import Blocked, Status              # noqa: E402
from hardware.offline_tests.fake_diagnostic import (         # noqa: E402
    deployed_profile_section, diagnostic_script)
from hardware.offline_tests.fake_link import healthy_script  # noqa: E402
from hardware.offline_tests.harness import (Bench, Checks,   # noqa: E402
                                            cli)


AGENT = HERE.parent / "test_side_firmware" / "diagnostic_agent.py"

DEPLOYMENT = HERE.parent / "test_side_firmware" / "DEPLOYMENT.md"


def _agent_source():
    return AGENT.read_text(encoding="utf-8")


def _agent_tree():
    return ast.parse(_agent_source())


def _deployed_bench(**kwargs):
    """A bench whose profile records the agent as deployed."""
    from hardware.configuration.profile import Profile

    profile = Profile({
        "name": "diagnostic bench",
        "port": "/dev/fake0",
        "diagnostic_firmware": deployed_profile_section(),
    })

    script = dict(healthy_script())
    script.update(diagnostic_script(**kwargs))

    return Bench(script=script, profile=profile)


def run():
    checks = Checks("hardware/offline_tests/test_diagnostic.py")

    # ------------------------------------------------------------------
    checks.section("the agent exists and is documented")

    checks.ok(AGENT.is_file(),
              "the diagnostic agent source is present")

    checks.ok(DEPLOYMENT.is_file(),
              "and so are its deployment instructions")

    deployment = DEPLOYMENT.read_text(encoding="utf-8")

    for phrase in ("not competition firmware", "Restore production",
                   "HW-B2-011", "HW-B2-012"):
        checks.ok(phrase.lower() in deployment.lower(),
                  "DEPLOYMENT.md covers {!r}".format(phrase))

    # ------------------------------------------------------------------
    checks.section("the agent cannot move anything")

    source = _agent_source()
    tree = _agent_tree()

    for forbidden in ("write_goal", "move_relative", "test_move",
                      "set_goal", "enable_torque", "disable_torque",
                      "write_word", "write_byte"):
        checks.ok(forbidden not in source,
                  "the agent never calls {} - it has no code that "
                  "could turn the carousel or change torque".format(
                      forbidden))

    checks.section("the agent runs nothing at import")

    top_level_calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]

    checks.equal(top_level_calls, [],
                 "no module-level call - copying the file to the board "
                 "cannot make it run")

    # Checked in the AST, not the text: the file's closing comment
    # EXPLAINS that it has no guard, and a string search finds the
    # explanation.
    guards = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and ast.dump(node.test).find("__name__") != -1
    ]

    checks.equal(guards, [],
                 "no __main__ guard either; it is started by hand from "
                 "the REPL, deliberately")

    checks.section("the agent's command surface is a whitelist")

    commands = None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue

        targets = [t for t in node.targets
                   if isinstance(t, ast.Attribute)
                   and t.attr == "commands"]

        if targets and isinstance(node.value, ast.Dict):
            commands = [
                key.value for key in node.value.keys
                if isinstance(key, ast.Constant)
            ]

    checks.ok(commands is not None,
              "the agent's command table was found in its source")

    if commands is not None:
        checks.equal(sorted(commands), sorted(COMMANDS),
                     "the agent's commands are exactly the ones the "
                     "adapter expects - no more")

        checks.equal(len(commands), 5,
                     "five commands, one of which is the only write")

    checks.section("the agent's register whitelist matches the adapter")

    agent_registers = None

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue

        names = [t.id for t in node.targets if isinstance(t, ast.Name)]

        if "READABLE_REGISTERS" in names and isinstance(
                node.value, ast.Dict):
            agent_registers = {
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Constant)
                and isinstance(value, ast.Constant)
            }

    checks.ok(agent_registers is not None,
              "the agent declares a register whitelist")

    if agent_registers is not None:
        checks.equal(sorted(agent_registers), sorted(READABLE_REGISTERS),
                     "the agent and the adapter whitelist the same "
                     "registers - two checks of one rule, because one "
                     "of them runs on a microcontroller that may be a "
                     "version behind")

        checks.equal(agent_registers, dict(READABLE_REGISTERS),
                     "and give them the same names")

    checks.ok("MAX_READ_LENGTH = {}".format(MAX_READ_LENGTH) in source,
              "the agent bounds a read to {} bytes, as the adapter "
              "does".format(MAX_READ_LENGTH))

    checks.section("the agent reuses the production drivers")

    imported = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for module in ("config", "sensor", "servo"):
        checks.ok(module in imported,
                  "the agent imports the production {} module rather "
                  "than reimplementing it".format(module))

    checks.ok("ST3215" in source,
              "and drives the servo through the production ST3215 class")

    checks.ok("scan_bus" in source,
              "and scans I2C through the production scan_bus")

    checks.section("the agent's protocol is unmistakably not production")

    checks.ok('AGENT_PROTOCOL = "{}"'.format(AGENT_PROTOCOL) in source,
              "the agent's protocol string matches the adapter's")

    checks.ok(AGENT_PROTOCOL != "freya-science-module",
              "and is NOT the production firmware name - a PC that read "
              "a production answer as register bytes would be "
              "interpreting a status report as a servo position")

    # ------------------------------------------------------------------
    checks.section("the adapter is BLOCKED until the agent is deployed")

    with Bench() as bench:
        capabilities = bench.context.diagnostic.capabilities()

        for name in ("diagnostic.agent", "diagnostic.servo_raw",
                     "diagnostic.servo_feedback", "diagnostic.i2c_scan"):
            capability = capabilities[name]

            checks.ok(not capability.available,
                      "{} is unavailable on a bench that has not "
                      "deployed the agent".format(name))

            checks.ok("DEPLOYMENT.md" in capability.recommendation,
                      "{} points at the deployment instructions".format(
                          name))

    checks.section("and available once the profile records it")

    with _deployed_bench() as bench:
        capabilities = bench.context.diagnostic.capabilities()

        for name in ("diagnostic.agent", "diagnostic.servo_raw",
                     "diagnostic.servo_feedback", "diagnostic.i2c_scan"):
            checks.ok(capabilities[name].available,
                      "{} is available once deployed".format(name))

    # ------------------------------------------------------------------
    checks.section("the handshake refuses anything that is not the agent")

    with _deployed_bench(protocol="freya-science-module") as bench:
        checks.raises(
            Blocked,
            lambda: bench.context.diagnostic.identify(),
            "a PRODUCTION firmware answering diag_identify is refused - "
            "its replies must not be read as register bytes")

    with _deployed_bench(protocol_version=99) as bench:
        checks.raises(
            Blocked,
            lambda: bench.context.diagnostic.identify(),
            "a protocol version this adapter does not speak is refused")

    with _deployed_bench() as bench:
        identity = bench.context.diagnostic.identify()

        checks.equal(identity["protocol"], AGENT_PROTOCOL,
                     "a matching agent identifies successfully")

        checks.equal(identity["protocol_version"],
                     AGENT_PROTOCOL_VERSION,
                     "with the version this adapter speaks")

        checks.equal(identity["moves"], False,
                     "and declares that it cannot move anything")

    # ------------------------------------------------------------------
    checks.section("the adapter enforces the bounds itself")

    with _deployed_bench() as bench:
        adapter = bench.context.diagnostic

        checks.raises(
            ValueError,
            lambda: adapter.servo_raw_read(register=200),
            "a register outside the whitelist is refused by the ADAPTER, "
            "before it reaches the wire")

        checks.raises(
            ValueError,
            lambda: adapter.servo_raw_read(register=56,
                                           length=MAX_READ_LENGTH + 1),
            "a read longer than the bound is refused by the adapter")

        checks.raises(
            ValueError,
            lambda: adapter.servo_raw_read(register=56, length=0),
            "a zero-length read is refused")

    checks.section("a whitelisted read returns bytes and a reading")

    with _deployed_bench() as bench:
        answer = bench.context.diagnostic.servo_raw_read(
            register=56, length=2)["data"]

        checks.equal(answer["bytes"], [0, 4],
                     "the raw bytes come back untouched")

        checks.equal(answer["parsed_little_endian"], 1024,
                     "and the little-endian reading is 0x0400 = 1024")

        checks.equal(answer["register_name"], "PRESENT_POSITION",
                     "the register is named")

    checks.section("byte-order interpretation is diagnostic, not truth")

    interpretations = DiagnosticAdapter.interpret_bytes([0, 4])

    checks.equal(interpretations["little_endian"], 1024,
                 "little-endian is the ST3215 memory-table order")

    checks.equal(interpretations["big_endian"], 4,
                 "big-endian is offered for comparison")

    checks.equal(interpretations["hex"], "00 04",
                 "and the raw hex is kept")

    checks.ok("not measurements" in interpretations["note"],
              "the interpretations say in words that they are "
              "diagnostics, not measurements")

    checks.equal(DiagnosticAdapter.interpret_bytes([]), None,
                 "no bytes means no interpretation, not a zero")

    swapped = DiagnosticAdapter.interpret_bytes([8, 0])

    checks.equal(swapped["little_endian"], 8,
                 "8 read little-endian is 8")

    checks.equal(swapped["big_endian"], 2048,
                 "and big-endian is 2048 - the H-002 hint that costs "
                 "nothing to print")

    # ------------------------------------------------------------------
    checks.section("the three original blockers unblock on deployment")

    for test_id in ("HW-B2-006", "HW-B3-004", "HW-B6-001"):
        with Bench() as bench:
            result = bench.run(test_id)

            checks.equal(result.status, Status.BLOCKED,
                         "{} is BLOCKED without the agent".format(
                             test_id))

    with _deployed_bench() as bench:
        result = bench.run("HW-B6-001")

        checks.ok(result.status != Status.BLOCKED,
                  "HW-B6-001 runs once the agent is deployed")

        checks.ok(any("AS7265x answers at its configured address"
                      in c.description for c in result.checks),
                  "and checks the AS7265x answered at its address")

        scans = [m for m in result.measurements
                 if m.get("stage") == "i2c_scan"]

        checks.ok(bool(scans) and "0x49" in (scans[0].get("addresses")
                                             or ""),
                  "recording every address the scan found")

    checks.section("an I2C bus with no sensor is diagnosed, not guessed")

    with _deployed_bench(addresses=("0x40", "0x77")) as bench:
        result = bench.run("HW-B6-001")

        checks.ok(bool(result.failed_checks()),
                  "a bus that answers without the sensor fails")

        checks.ok(bool(result.defects),
                  "and raises a defect that says the BUS is working")

        if result.defects:
            checks.ok("bus is alive" in result.defects[0]["title"],
                      "distinguishing 'the sensor is absent' from 'the "
                      "bus is dead'")

    with _deployed_bench(addresses=()) as bench:
        result = bench.run("HW-B6-001")

        checks.ok(bool(result.failed_checks()),
                  "an empty bus fails too, but for a different reason")

    checks.section("the agent's feedback reaches HW-B2-008")

    with _deployed_bench() as bench:
        result = bench.run("HW-B2-008")

        checks.ok(result.status != Status.BLOCKED,
                  "HW-B2-008 runs once the agent is deployed")

        readings = [m for m in result.measurements
                    if m.get("stage") == "telemetry"]

        checks.ok(len(readings) >= 10,
                  "and records every telemetry register it read")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
