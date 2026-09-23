# GIGABYTE fan driver

ThermalDeck uses Linux hwmon controls exposed by a separate kernel driver. It does not ship or maintain the driver itself. The integration currently targets **GIGABYTE X870E AORUS XTREME AI TOP** and the upstream [`frankcrawford/it87`](https://github.com/frankcrawford/it87) driver at commit `bc06d3488439e5fcd725c1bdcfcac994d6d95cac` (2026-09-13), module version `v2.0-4-gbc06d34.20260913`.

The driver's source lists this exact motherboard under SIV ID `0xA10A090A`. This is intended support, not a guarantee that every BIOS version and wiring arrangement works. Readback of a PWM value alone does not prove the fan changed speed.

| Controller | Expected headers |
| --- | --- |
| IT8696E | CPU_FAN, SYS_FAN1, SYS_FAN2, SYS_FAN3, CPU_OPT |
| IT87952E and its additional channels | FAN5_PUMP, FAN6_PUMP, SYS_FAN4, FAN7_PUMP, FAN8_PUMP |

Sources: [primary mapping](https://github.com/frankcrawford/it87/blob/bc06d3488439e5fcd725c1bdcfcac994d6d95cac/Sensors%20configs/Gigabyte/configs/gigabyte-it87-amd.conf#L2477), [secondary mapping](https://github.com/frankcrawford/it87/blob/bc06d3488439e5fcd725c1bdcfcac994d6d95cac/Sensors%20configs/Gigabyte/configs/gigabyte-it87-amd.conf#L2585).

## Verification status

On Ubuntu 24.04 with running kernel `7.0.0-31-generic`, this pinned driver passed an unprivileged build using the installed kernel headers and GCC 13.3.0. `modinfo` reported the expected version, `mmio` parameter, and matching kernel vermagic. BTF generation was skipped because the distribution's `vmlinux` was unavailable; the kernel module built successfully.

**Building successfully is not hardware verification.** Installation, device detection, header mapping, and actual RPM response must be recorded separately. This document does not claim that a successful hardware test has occurred.

## Installation

Prerequisites are `curl`, `dkms`, `make`, GCC, `mokutil` for an EFI system, and headers matching `uname -r`. The installer does not install system packages. Secure Boot must be confirmed disabled; automatic key enrollment is outside this installer.

Run from the project directory:

```bash
sudo ./scripts/install-it87.sh
```

To install without loading the module immediately:

```bash
sudo ./scripts/install-it87.sh --no-load
```

The installer downloads only five source/license files from the pinned upstream commit, verifies each SHA256 checksum, stages root-owned source under `/usr/src/it87-v2.0-4-gbc06d34.20260913`, and installs through DKMS. This preserves the distribution's original driver. It refuses to replace a different registered it87 DKMS version or a different loaded it87 module. A repeat invocation with this version already installed and loaded reports that state without changes.

Default installation ends by loading `it87`. `--no-load` skips that step. No startup module configuration, fan curve, sensor scan, or fan calibration is created. Logs and before/after readings go to `/var/log/thermaldeck/it87-install-*.log`. DKMS rebuilds the driver during future kernel updates; compatibility with future kernels still needs checking.

Upstream's convenience installer is intentionally not used: it derives the module name from the checkout directory name, loads the module automatically, and does not reliably propagate installation failure. ThermalDeck checks each installation step explicitly.

## What module loading does

Loading a hardware driver is **not a read-only check**. This driver reads Gigabyte firmware IDs through SMI, sets up MMIO bridge access, probes Super-I/O devices, temporarily changes SMBus shadowing, initializes monitoring and tachometer registers, and may configure noise-sensor routing. Its default probe path does not explicitly take over fan PWM duty or replace the BIOS fan curves; that source review is not a promise of zero hardware side effects.

Use the default module parameters. Do not add `force_id`, `ignore_resource_conflict`, `fix_pwm_polarity`, or `acpi_enforce_resources=lax` to make a failed probe appear successful. A failed probe needs its kernel log inspected first.

The reviewed driver takes snapshots when supported channels are first switched to software control. Returning those channels to `pwmN_enable=2` restores their captured firmware state. The driver also attempts restoration on module removal and suspend; resume can reapply a previous software setting. Restoration can fail if hardware access fails, so a controller must verify the outcome and retain a recovery path.

## Checking detection

These commands inspect the selected driver and sensor interfaces:

```bash
modinfo -n it87
modinfo -F version it87
journalctl -k -b --no-pager | rg 'it87|H2RAM|Gigabyte SIV|IT57xx'
sensors
```

Look for `it8696_…` and `it87952_…` hwmon devices, fan RPMs, and `pwmN_enable` controls. Device indices such as `hwmon5` change across reboots: identify the chip by its name and device path. Some added H2RAM/IT57xx channels return no live duty value while firmware controls them; `ENODATA` is expected on those channels and does not mean the fan is missing.

Keep the BIOS settings available as a fallback. Map one connected header at a time by a brief, bounded speed change and an observed RPM response, then restore firmware control. The installer performs none of those speed tests. A splitter may report one fan's RPM for multiple connected fans, and an AIO may power its pump separately; header names alone cannot establish the physical wiring.

## PWM integration notes

For the reviewed snapshot-backed controllers, `pwmN_enable=0` selects full speed, `1` selects manual control, and `2` restores the captured firmware state. `pwmN` values span 0–255.

H2RAM channels reject writes to `pwmN` while firmware control or full-speed mode is selected. A controller cannot assume that writing the target duty before changing mode will work. For an intentional full-speed takeover, set enable to `0`, then `1`, then write the desired duty. This starts from full speed; directly selecting manual mode on an additional channel may initially use a BIOS Start PWM value instead of a live duty reading.

These semantics are specific to the reviewed driver and chip paths. Never apply them automatically to arbitrary hwmon devices. Do not use `pwmconfig` to identify a running AIO pump: its calibration changes and may stop fans.

## Removal and rollback

First stop any controller and restore the channels it owns to firmware control. Then remove this version explicitly:

```bash
sudo modprobe -r it87
sudo dkms remove -m it87 -v v2.0-4-gbc06d34.20260913 --all
sudo depmod -a
modinfo -n it87
```

The final path should resolve to the distribution's original module rather than `updates/dkms`. Only after confirming successful removal, delete the dedicated source directory if desired. Remove any startup configuration you added separately; this installer creates none. If module unloading or firmware restoration fails, inspect the logs and use a normal reboot as recovery rather than forcing module removal.

## License

Upstream it87 is licensed under **GPL-2.0**; its original `COPYING` accompanies the installed source. ThermalDeck's installer downloads upstream source and does not change that license. Any redistribution of the driver or a built module must satisfy the upstream license independently of the application's license.
