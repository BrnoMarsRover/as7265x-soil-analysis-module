# Freya Science Module

Science and sensor electronics for the **Freya Mars Rover**, developed by the Brno Mars Rover team.

This repository contains the complete editable Altium Designer project, ESP32 firmware, technical documentation and release files for the rover science module.

## Project overview

The module is designed to support sample analysis during rover science tasks.

Current functionality includes:

- ESP32-based sensor and actuator control
- AS7265x multispectral sensor interface
- Servo control for the sample-analysis mechanism
- Controlled illumination for spectral measurements
- UART communication with the rover computer
- Dedicated power distribution and protection
- JST-XH connectors for external devices
- XT30 main power input

Planned hardware extensions include:

- VL53L4CD time-of-flight distance sensor
- Load cell interface for optional sample-mass measurements
- Additional sensor connectors and improved expandability

## Repository structure

```text
.
├── hardware/
│   └── altium/
│       ├── MarsRover_ScienceTeam.PrjPcb
│       ├── PCB1.PcbDoc
│       ├── 01_POWER.SchDoc
│       ├── 02_ESP32.SchDoc
│       ├── 03_CONNECTION.SchDoc
│       └── MarsRover_ScienceTeam.BomDoc
│
├── firmware/
│   └── ...
│
├── docs/
│   └── ...
│
├── .gitignore
└── README.md
```

## Opening the Altium project

Open the complete project using:

```text
hardware/altium/MarsRover_ScienceTeam.PrjPcb
```

Do not normally open individual `.SchDoc` or `.PcbDoc` files directly from Windows Explorer. Open them from the **Projects** panel after loading the `.PrjPcb` file.

## Development workflow

Before starting work:

1. Pull the latest changes from GitHub.
2. Open `MarsRover_ScienceTeam.PrjPcb`.
3. Make the required schematic, PCB or firmware changes.
4. Use **File → Save All** in Altium.
5. Review changed files in GitHub Desktop.
6. Create a clear commit.
7. Push the commit to GitHub.

Example commit messages:

```text
Add VL53L4CD distance sensor interface
Update servo power routing
Fix ESP32 UART connection
Add load cell connector
Update AS7265x calibration firmware
```

## Versioning

Normal development changes are stored as Git commits.

Important hardware milestones should be published as GitHub Releases, for example:

```text
v0.1.0 - Initial prototype
v0.2.0 - PCB Rev A
v0.3.0 - Distance and weight sensor revision
v1.0.0 - Competition release
```

Release attachments may include:

- Altium Project Package
- Gerber files
- NC Drill files
- BOM
- Pick-and-place files
- Schematic PDF
- Assembly drawings
- Compiled firmware

Generated manufacturing outputs should normally be attached to a GitHub Release rather than committed repeatedly to the main repository.

## Main development tools

- Altium Designer
- ESP32
- MicroPython / Python tools
- GitHub Desktop
- Git

## Project status

The first custom carrier PCB has been manufactured and assembled. The next revision is being prepared with improved sensor integration, including distance measurement and optional sample-weight sensing.

## Team

Developed as part of the **Brno Mars Rover – Freya** project.
