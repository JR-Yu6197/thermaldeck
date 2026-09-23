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

- 72 offline tests cover NVML fan operations, hwmon transitions, exact SIV chip names, restoration failures, curves, thermal guards, transport failures, EOF, heartbeat timeout, and shutdown error reporting.
- GTK dashboard was rendered and visually reviewed using the current read-only sensor data.
- The initial public repository's GitHub Actions checks passed. CI repeats the offline tests, compilation, and shell syntax checks for later commits.

## Completed hardware checks

The pinned driver was installed through DKMS and loaded with default parameters.
The kernel identified SIV `A10A090A`, IT8696E at `0xa40`, IT87952E at `0xa60`, and
two IT57xx extension channels. The hwmon names include the SIV suffix:
`it8696_a10a090a` and `it87952_a10a090a`. ThermalDeck accepts these exact aliases.

All ten motherboard headers appeared with readable fan counters and writable PWM
interfaces. Five had nonzero RPM. Only these connected channels and the two GPUs
were changed during the bounded test; the other five were left alone.

| Device/header | Bounded check | Observed response | Restoration |
| --- | --- | --- | --- |
| RTX PRO 6000 Blackwell | Automatic → 60% | Approx. 1200 → 1577 reported RPM during ramp-up | NVIDIA automatic |
| RTX 5090 | Automatic → 60% | 0 → 1486 reported RPM during ramp-up | NVIDIA automatic |
| CPU_FAN | Firmware → 100%, then 90% | Approx. 1520 → 1962 RPM; initial 100% sample was still ramping | Firmware mode 2 |
| SYS_FAN1 | 100% → 90% | Approx. 1490 → 1445 RPM | Firmware mode 2 |
| SYS_FAN4 | 100% → 90% | Approx. 1516 → 1477 RPM | Firmware mode 2 |
| SYS_FAN7_PUMP | 100% → 95% | Approx. 5040 → 4820 RPM | Firmware mode 2 |
| SYS_FAN8_PUMP | 100% → 90% → 80% | Approx. 2884 → 2757 → 2500 RPM after 8/12/12 seconds | Firmware mode 2 after worker EOF |

The initial 100→95% four-second FAN8 check showed no clear RPM change. The longer
bounded check above resolved that uncertainty. CPU temperature stayed approximately
45–47°C during the FAN8 check. The worker exited successfully after stdin EOF and
the channel returned to its original firmware mode. Every tested motherboard
channel was subsequently in firmware mode and both GPUs were in automatic mode.

The exact physical fan/pump plugged into each named header was not visually
inspected. Header control and observed RPM response are verified; physical wiring,
the disconnected headers' response, long stress tests, and reboot persistence have
not been independently tested. NVML reports driver RPM, which is not an independent
measurement of physical fan obstruction.

The installed module is selected from `updates/dkms`. The optional
`/etc/modules-load.d/thermaldeck-it87.conf` was created with only `it87` so the
driver loads on the next boot; no software fan profile is applied at startup.
No reboot was performed as part of validation.
