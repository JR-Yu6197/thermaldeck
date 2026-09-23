# Validation record

Date: 2026-09-23

Development target: Ubuntu 24.04, kernel 7.0.0-31-generic, X870E AORUS XTREME AI TOP,
Ryzen 9 9950X3D, RTX PRO 6000 Blackwell Workstation Edition and GeForce RTX 5090.

## Completed read-only checks

- Community it87 commit `bc06d3488439e5fcd725c1bdcfcac994d6d95cac` builds against the running kernel headers.
- Both NVIDIA GPUs expose two controllable NVML fan channels with a 30–100% manual range.
- CPU Tctl and GPU temperatures are readable by the unprivileged application.
- Device UUIDs are discovered at runtime; no local GPU identifiers or diagnostic logs are included in the repository.

## Pending hardware checks

At the time this record was created, the system administrator authentication dialog
for it87 installation was pending. A successful build does not demonstrate actual
motherboard PWM control. This record will be updated after installation and testing.
