# Validation record

Date: 2026-09-23

Development target: Ubuntu 24.04, kernel 7.0.0-31-generic, X870E AORUS XTREME AI TOP,
Ryzen 9 9950X3D, RTX PRO 6000 Blackwell Workstation Edition and GeForce RTX 5090.

## Completed read-only checks

- Community it87 commit `bc06d3488439e5fcd725c1bdcfcac994d6d95cac` builds against the running kernel headers.
- Both NVIDIA GPUs expose two controllable NVML fan channels with a 30–100% manual range.
- CPU Tctl and GPU temperatures are readable by the unprivileged application.
- NVML's optional RPM API works on both installed GPUs. One idle sample reported approximately 1200 RPM for the RTX PRO 6000 and 0 RPM for the RTX 5090 in automatic mode.
- Device UUIDs are discovered at runtime; no local GPU identifiers or diagnostic logs are included in the repository.

## Software checks

- 67 offline tests cover NVML fan operations, hwmon transitions, restoration failures, curves, thermal guards, transport failures, EOF, heartbeat timeout, and shutdown error reporting.
- GTK dashboard was rendered and visually reviewed using the current read-only sensor data.
- The initial public repository's GitHub Actions checks passed. CI repeats the offline tests, compilation, and shell syntax checks for later commits.

## Pending hardware checks

At the time this record was created, the system administrator authentication dialog
for it87 installation was pending. A successful build does not demonstrate actual
motherboard PWM control. This record will be updated after installation and testing.
