# Deploying the diagnostic agent

> ## This is not competition firmware
>
> `diagnostic_agent.py` exists to answer three questions the shipped
> firmware cannot: what bytes the ST3215 actually returned, what its
> full telemetry says, and which addresses answer on the I2C bus right
> now. It unblocks `HW-B2-006`, `HW-B3-004` and `HW-B6-001`.
>
> **It has never been deployed.** Those three tests stay `NOT_RUN` and
> `BLOCKED` until it has been, on real hardware, by a human following
> this page.

---

## What it can and cannot do

| | |
| --- | --- |
| Moves the carousel | **No.** There is no movement command in the file. |
| Writes servo registers | **No.** Reads are whitelisted to 14 named registers. |
| Changes torque | **No.** |
| Writes anything | Only `diag_lamps_off`, so a session can leave the UV source off. |
| Runs at boot | **No.** No `main` guard, no module-level call. |
| Reuses production drivers | Yes — `servo.ST3215` and `sensor.scan_bus`, imported not reimplemented. |

The read whitelist and the 1–4 byte length bound are enforced twice: in
`adapters/diagnostic.py` on the PC and again in the agent. Two checks of
one rule is not redundancy when one of them runs on a microcontroller
that may be a version behind.

---

## Before you start

1. The module is on the bench, not on the rover.
2. `B0` and `B1` have passed, so you know which device you are talking
   to and that the link is sound.
3. Record the production firmware hash **first** — you will check it
   back afterwards:

```bash
python3 firmware/tools/device.py --port <device> --hash
```

If that tool has no `--hash` mode on your build, take the hash of the
deployed files from the working tree you deployed from, and record which
commit that was.

---

## Deploy

The agent is a single file and needs no production change.

```bash
python3 -m mpremote connect <device> fs cp firmware/Tests/hardware/test_side_firmware/diagnostic_agent.py :diagnostic_agent.py
```

Then start it **by hand**, from the REPL:

```bash
python3 -m mpremote connect <device> repl
```

```python
import diagnostic_agent
diagnostic_agent.DiagnosticAgent().serve_forever()
```

It prints one banner frame identifying itself, switches every lamp off,
and then waits for requests.

> Note the deliberate friction. The agent does not start itself, is not
> imported by `main.py`, and has no `__main__` guard. Copying it to the
> board — or forgetting it there — cannot make it run.

---

## Record the deployment in the bench profile

The harness will not use the agent until the profile says it is there,
and the run fingerprint records which build answered:

```json
"diagnostic_firmware": {
  "deployed": true,
  "version": "1.0.0",
  "sha256": "<sha256 of diagnostic_agent.py>",
  "deployed_utc": "2026-08-26T10:00:00Z",
  "production_firmware_sha256": "<the hash you recorded above>"
}
```

Get the file hash with:

```bash
sha256sum firmware/Tests/hardware/test_side_firmware/diagnostic_agent.py
```

---

## Verify before trusting it

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile <your-profile> --run HW-B2-011 --confirm-hardware
```

`HW-B2-011` checks the identity handshake, that the agent reports
`moves: false`, that its register whitelist is the one this adapter
expects, and that a non-whitelisted register is refused. If any of that
disagrees, stop: the thing answering is not the build you think it is.

---

## Restore production firmware afterwards

**This is part of the procedure, not an afterthought.**

```bash
python3 -m mpremote connect <device> fs rm :diagnostic_agent.py
```

Then redeploy the competition firmware the normal way and confirm it:

```bash
python3 firmware/tools/device.py --port <device> --deploy
```

Finally, clear the profile:

```json
"diagnostic_firmware": { "deployed": false, "version": null, "sha256": null }
```

and run `HW-B2-012`, which verifies the restoration and records the
restored hash.

> ### Any prerequisite PASS earned while the agent was deployed is void
>
> The run fingerprint includes the diagnostic firmware, so the layer
> gate will not accept those results for a production run — by design.
> Re-run the campaigns you care about on the restored firmware.

---

## If a command cannot be made safe

Leave the test `BLOCKED` and say exactly what is missing. A `BLOCKED`
test with a precise reason is worth more than a command that quietly
does something nobody bounded — which is the whole reason this agent has
no movement command at all, not even a gated one.
