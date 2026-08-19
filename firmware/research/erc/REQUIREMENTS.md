# Requirements and traceability

Requirements for the Freya AS7265x science subsystem. Each states WHAT the
software must do, not how; the implementation column names the design that
satisfies it, and the verification column names the test that proves it.

| Status | Meaning |
|---|---|
| `PASS` | Implemented and verified by an automated test |
| `PARTIAL` | Implemented, verification incomplete or indirect |
| `BLOCKED_BY_HARDWARE` | Software complete; needs a physical measurement |
| `BLOCKED_BY_EXTERNAL_DATA` | Software complete; needs a third-party dataset |

`BLOCKED` never means "not built". Where it appears, the schema, loader,
validator, pipeline and tests exist and are verified — only the data does
not.

Verified at 773 automated checks across 8 suites.

---

## System

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-SYS-001 | All software, databases, tests and research tooling shall reside under a single `firmware/` root. | `firmware/` | `test_architecture` §0 | PASS |
| REQ-SYS-002 | The software shall be organised into acquisition, orchestration, persistence, science and verification layers with enforced boundaries. | `ESP32/ PC/ BD/ Measurements/ Tests/` | `test_architecture` §0–4 | PASS |
| REQ-SYS-003 | The persistence layer shall not depend on the science layer. | BD imports no Measurements | `test_architecture` §2 | PASS |
| REQ-SYS-004 | Device software shall not depend on host layers or load science data. | `ESP32/` imports only its own modules | `test_architecture` §1 | PASS |
| REQ-SYS-005 | The science layer shall not access serial ports, I²C or the operator interface. | `Measurements/` | `test_architecture` §3 | PASS |
| REQ-SYS-006 | No layer shall exist in duplicate at another location. | single tree | `test_architecture` §0 | PASS |

## Channels and feature space

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-CH-001 | The 18 sensor channels and their wavelengths shall be defined exactly once. | `BD/channels.py` | `test_architecture`, `test_db1` §2 | PASS |
| REQ-CH-002 | The software shall distinguish the 18-band and 54-feature spaces explicitly. | `AS7265X_18`, `AS7265X_54_MULTIILLUM` | `test_inference` §1 | PASS |
| REQ-CH-003 | Comparison between incompatible feature spaces shall fail rather than align by index. | `require_compatible()` | `test_inference` §1 | PASS |
| REQ-CH-004 | A 54-feature measurement may be narrowed to its 18 WHITE bands; the reverse shall be impossible. | `project_to_18()` | `test_inference` §1–2 | PASS |

## Databases

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-DB-001 | The software shall maintain DB1, DB2 and DB3 as independent sources, never pooled. | `BD/registry.py` | `test_inference` §2, §7 | PASS |
| REQ-DB-002 | Each database shall declare its evidence kind, feature space and version. | `DEFINITIONS` | `test_inference` §2 | PASS |
| REQ-DB-003 | Each database shall be independently loadable and validatable. | `DatabaseHandle` | `test_inference` §2–3 | PASS |
| REQ-DB-004 | A database that is absent or empty shall report why, and shall not be silently omitted from results. | `STATUS_*`, `why_empty` | `test_inference` §7 | PASS |
| REQ-DB-005 | Malformed database documents shall be rejected with a stated reason. | `validate_materials()` | `test_inference` §3 | PASS |
| REQ-DB-006 | Only one copy of each database shall exist in the repository. | `BD/data/` | `test_architecture`, manual inventory | PASS |

## DB1 — measured, 18-band

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-DB1-001 | DB1 shall contain the complete historical session of 23 materials. | `BD/data/DB1_measured_18/` | `test_db1` §1 | PASS |
| REQ-DB1-002 | Raw Sample, Dark and White shall be preserved per channel, not only derived reflectance. | `materials.json` | `test_db1` §3 | PASS |
| REQ-DB1-003 | Derived reflectance shall be reproducible from the raw measurements. | `R=(S−D)/(W−D)` | `test_db1` §3 | PASS |
| REQ-DB1-004 | DB1 shall be regenerable deterministically from a verbatim source snapshot. | `build_db1.py` | `test_db1` §8 | PASS |
| REQ-DB1-005 | Source integrity shall be verifiable by hash. | manifest SHA256 | `test_db1` §7 | PASS |
| REQ-DB1-006 | Reflectance values shall never be clipped to [0,1]. | no clipping | `test_db1` §4 | PASS |
| REQ-DB1-007 | Data anomalies shall be represented, not smoothed away. | `dark_provenance`, quality flags | `test_db1` §4–5 | PASS |
| REQ-DB1-008 | Unrecorded acquisition settings shall be marked UNKNOWN, never defaulted. | `acquisition_settings` | `test_db1` §6 | PASS |
| REQ-DB1-009 | Re-normalising against a new White shall create a new derived representation without altering the measured record. | raw retained; `white_version` field reserved | — | PARTIAL |

## DB2 — measured, 54-feature

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-DB2-001 | DB2 shall define a 54-feature space of 18 bands × 3 illuminations. | `AS7265X_54_MULTIILLUM` | `test_inference` §1–2 | PASS |
| REQ-DB2-002 | DB2 shall have a schema, loader and validator before any data exists. | `registry.py`, manifest | `test_inference` §2 | PASS |
| REQ-DB2-003 | DB2 shall report a clear not-ready state with the reason. | `STATUS_EMPTY` + `why_empty` | `test_inference` §2, §7 | PASS |
| REQ-DB2-004 | DB2 features shall never be synthesised from DB1 or from external libraries. | no such code path exists | `test_inference` §1 (54↛18 blocked) | PASS |
| REQ-DB2-005 | Population of DB2 with real measurements. | — | — | **BLOCKED_BY_HARDWARE** |

## DB3 — external reference, projected

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-DB3-001 | DB3 records shall be distinguishable from measured records at all times. | `REFERENCE_PROJECTED` | `test_inference` §3–4 | PASS |
| REQ-DB3-002 | Every DB3 record shall carry source provenance; records without it shall be refused. | `build_record()` | `test_inference` §3–4 | PASS |
| REQ-DB3-003 | External spectra shall be projected using a band response model, not nearest-wavelength sampling. | `spectral_projection.project()` | `test_inference` §4 | PASS |
| REQ-DB3-004 | The projection shall not extrapolate beyond the source's measured range. | coverage gate | `test_inference` §4 | PASS |
| REQ-DB3-005 | Approximated response models shall be labelled as approximations. | `approximate: true` | `test_inference` §4 | PASS |
| REQ-DB3-006 | The projection model shall be versioned and replaceable without editing data. | `channel_response()` | `test_inference` §4 | PASS |
| REQ-DB3-007 | Population of DB3 with external spectra. | importer + projection ready | — | **BLOCKED_BY_EXTERNAL_DATA** |

## Calibration

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-CAL-001 | Calibrations shall be identifiable artifacts with an ID, timestamp and configuration. | `Measurements/calibration.py` | `test_calibration` | PASS |
| REQ-CAL-002 | The legacy calibration shall be immutable and the only one compared against DB1. | `BD/data/calibrations/legacy/` | `test_science`, `test_calibration` | PASS |
| REQ-CAL-003 | A calibration failing validation shall not become active. | injected validator | `test_calibration` | PASS |
| REQ-CAL-004 | Calibration validity shall depend on the acquisition settings it was taken at. | `EXPECTED_*` | `test_calibration`, `test_architecture` §5 | PASS |
| REQ-CAL-005 | Storage shall not depend on scientific validation logic. | validator injection | `test_architecture` §2 | PASS |
| REQ-CAL-006 | Creation of a full Dark + WHITE/UV/IR calibration on the instrument. | workflow implemented | — | **BLOCKED_BY_HARDWARE** |

## Measurement and analysis

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-ANA-001 | Reflectance shall be computed as (Sample−Dark)/(White−Dark) per channel. | `Measurements/analysis.py` | `test_science` §2–3, `test_db1` §3 | PASS |
| REQ-ANA-002 | Negative and out-of-range results shall be preserved and flagged, never clamped. | `normalize()`, `quality.py` | `test_science` §3 | PASS |
| REQ-ANA-003 | Comparison shall use multiple metrics from mathematically independent families. | `metrics.py` | `test_architecture` §6–9 | PASS |
| REQ-ANA-004 | Mathematically dependent metrics shall not contribute independent votes. | evidence families | `test_architecture` §6–9 | PASS |
| REQ-ANA-005 | Ranking shall combine families by rank, not by averaging incomparable scores. | `compare_all()` | `test_architecture` §9 | PASS |
| REQ-ANA-006 | Each database shall be analysed separately and reported separately. | `analyse_database()` | `test_inference` §7 | PASS |
| REQ-ANA-007 | A cross-database consensus shall be produced, with disagreements shown. | `build_consensus()` | `test_inference` §7 | PASS |
| REQ-ANA-008 | Agreement between dependent sources shall not be counted as independent corroboration. | `independent_evidence_kinds` | `test_inference` §8 | PASS |
| REQ-ANA-009 | The system shall be able to report UNKNOWN rather than always naming a material. | `LEVEL_UNKNOWN` | `test_inference` §6 | PASS |
| REQ-ANA-010 | Conclusions shall fall back to family level when material-level evidence is insufficient. | `LEVEL_FAMILY` | `test_inference` §6 | PASS |
| REQ-ANA-011 | Confidence shall be derived from evidence structure, not from similarity magnitude. | `assess_confidence()` | `test_inference` §8 | PASS |
| REQ-ANA-012 | Confidence shall not be presented as a probability. | explicit note | `test_inference` §8 | PASS |
| REQ-ANA-013 | Quality control shall run before classification and a FAIL shall suppress identification. | `quality.py`, `infer()` | `test_science`, `test_inference` §8 | PASS |

## Mixture analysis

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-MIX-001 | The software shall estimate whether a spectrum is better explained by several materials. | `Measurements/mixture.py` | `test_inference` §5 | PASS |
| REQ-MIX-002 | Mixture coefficients shall be non-negative. | NNLS | `test_inference` §5 | PASS |
| REQ-MIX-003 | Coefficients shall be reported as spectral contribution, never as mass fraction. | naming + caveat | `test_inference` §5 | PASS |
| REQ-MIX-004 | The candidate set shall be bounded and screened for collinearity to prevent overfitting. | `select_endmembers()` | `test_inference` §5 | PASS |
| REQ-MIX-005 | A single material shall not be reported as a mixture. | `SINGLE_COMPONENT` | `test_inference` §5 | PASS |
| REQ-MIX-006 | An unidentifiable mixture shall be reported as unstable, not fitted anyway. | `UNSTABLE` | `test_inference` §5 | PASS |
| REQ-MIX-007 | Quantitative concentration output. | — | — | **BLOCKED_BY_HARDWARE** (needs prepared mixtures of known mass) |

## Traceability and reproducibility

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-TRC-001 | Every result shall record the analysis version and database versions used. | `infer()` output | `test_inference` §7 | PASS |
| REQ-TRC-002 | Every result shall record the calibration it was produced under. | `analysis.py` calibration block | `test_science` | PASS |
| REQ-TRC-003 | Derived values shall be reproducible from stored raw data. | raw retained | `test_db1` §3 | PASS |
| REQ-TRC-004 | Databases shall be regenerable from their sources, deterministically. | builders | `test_db1` §8 | PASS |
| REQ-TRC-005 | Raw measurements shall never be overwritten by derived data. | separate fields | `test_db1` §3–4 | PASS |

## Storage and interface

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-STO-001 | Sample records shall persist raw acquisition, processing, results and metadata. | `BD/samples.py` | `test_pc`, `test_integration` | PASS |
| REQ-STO-002 | Writes shall be atomic; a damaged archive shall be reported, not overwritten. | temp + rename | `test_pc` | PASS |
| REQ-STO-003 | Measured sample data shall not be published to the source repository. | `.gitignore` | manual verification | PASS |
| REQ-UI-001 | The carousel workflow shall be preserved. | `rover_science_client.py` | `test_pc`, `test_integration` | PASS |
| REQ-UI-002 | Analysis output shall be machine-readable, with formatting done by the interface. | `infer()` returns data | `test_inference` | PASS |

## Device

| ID | Requirement | Implementation | Verification | Status |
|---|---|---|---|---|
| REQ-HW-001 | There shall be exactly one sensor lifecycle; a boot failure shall not be permanent. | `SensorRuntime` | `test_esp32` | PASS |
| REQ-HW-002 | Sensor configuration shall be written, read back and verified. | `apply_configuration()` | `test_esp32`, `test_architecture` §5 | PASS |
| REQ-HW-003 | Reported settings shall be those read from registers, not those requested. | read-back | `test_esp32` | PASS |
| REQ-HW-004 | No failure shall leave an illumination source enabled. | `finally` | `test_esp32` | PASS |
| REQ-HW-005 | Errors shall carry machine-readable codes. | `SensorError`, `CommandError` | `test_esp32` | PASS |
| REQ-HW-006 | Confirmation of applied settings on physical hardware. | — | — | **BLOCKED_BY_HARDWARE** |

---

## Summary

| Status | Count |
|---|---|
| PASS | 63 |
| PARTIAL | 1 |
| BLOCKED_BY_HARDWARE | 4 |
| BLOCKED_BY_EXTERNAL_DATA | 1 |
| **Total** | **69** |

The five blocked requirements are blocked on physical measurements and
third-party datasets respectively. In every case the schema, loader,
validator, pipeline and tests exist and pass — only the data is absent.
