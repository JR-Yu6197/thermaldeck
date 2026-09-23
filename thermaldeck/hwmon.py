"""Conservative Linux hwmon access for the supported AORUS motherboard.

Only IDs returned by discovery are accepted by the write API.  Paths are never
accepted from callers, and a snapshot can only be restored by its own backend.
The constructor's paths are dependency injection points for offline tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import errno
import os
import re


SUPPORTED_BOARD = "X870E AORUS XTREME AI TOP"
CHANNEL_NAMES = {
    "it8696": {1: "CPU_FAN", 2: "SYS_FAN1", 3: "SYS_FAN2", 4: "SYS_FAN3", 5: "CPU_OPT"},
    "it87952": {
        1: "SYS_FAN5_PUMP", 2: "SYS_FAN6_PUMP", 3: "SYS_FAN4",
        4: "SYS_FAN7_PUMP", 5: "SYS_FAN8_PUMP",
    },
}
# communityit87 includes the verified board's SIV ID in its hwmon names.
# Accept only these exact names, never strip arbitrary suffixes from a chip.
_SIV_CHIP_NAMES = {
    "it8696_a10a090a": "it8696",
    "it87952_a10a090a": "it87952",
}
_FAN_INPUT = re.compile(r"fan([1-9][0-9]*)_input\Z")
_CHIP_NAME = re.compile(r"[A-Za-z0-9_.+-]+\Z")
_MODES = {0: "full", 1: "manual", 2: "hardware"}


class HwmonError(RuntimeError):
    """The hardware cannot safely perform or verify an operation."""


class ValidationError(ValueError):
    """An unsupported ID, speed, or snapshot was supplied."""


@dataclass(frozen=True)
class _Channel:
    id: str
    directory: Path
    chip: str
    number: int
    name: str
    pump: bool
    minimum: int
    identity: tuple[str, int, int]
    reasons: tuple[str, ...] = ()

    def path(self, suffix: str) -> Path:
        return self.directory / f"pwm{self.number}{suffix}"


@dataclass(frozen=True)
class _Snapshot:
    channel_id: str
    identity: tuple[str, int, int]
    mode: int
    pwm: int | None
    token: object = field(repr=False, compare=False)


class HwmonBackend:
    def __init__(
        self,
        root: Path = Path("/sys/class/hwmon"),
        board_path: Path = Path("/sys/class/dmi/id/board_name"),
    ) -> None:
        self.root = Path(root)
        self.board_path = Path(board_path)
        self._snapshot_token = object()

    @staticmethod
    def _integer(path: Path) -> int:
        value = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"-?[0-9]+", value):
            raise ValueError(f"Invalid integer in {path.name}")
        return int(value)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return ""

    def _directories(self) -> list[tuple[Path, str]]:
        try:
            entries = sorted(self.root.iterdir(), key=lambda item: item.name)
        except OSError:
            return []
        found = []
        for directory in entries:
            if not re.fullmatch(r"hwmon[0-9]+", directory.name):
                continue
            chip = self._read_text(directory / "name")
            if chip and _CHIP_NAME.fullmatch(chip):
                found.append((directory, _SIV_CHIP_NAMES.get(chip, chip)))
        return found

    def _channels(self) -> list[_Channel]:
        directories = self._directories()
        counts: dict[str, int] = {}
        for _, chip in directories:
            counts[chip] = counts.get(chip, 0) + 1
        board_ok = self._read_text(self.board_path) == SUPPORTED_BOARD
        channels = []
        for directory, chip in directories:
            try:
                info = directory.stat()
                identity = (str(directory.resolve(strict=True)), info.st_dev, info.st_ino)
                inputs = sorted(directory.glob("fan*_input"))
            except OSError:
                continue
            for input_path in inputs:
                match = _FAN_INPUT.fullmatch(input_path.name)
                if not match:
                    continue
                number = int(match[1])
                names = CHANNEL_NAMES.get(chip, {})
                known = number in names
                name = names.get(number) or self._read_text(directory / f"fan{number}_label") or f"{chip} fan {number}"
                pump = known and (name.endswith("_PUMP") or name == "CPU_OPT")
                reasons = []
                if not board_ok:
                    reasons.append("Motherboard model is not supported for control")
                if not known:
                    reasons.append("Unverified chip or fan channel; read-only")
                if counts[chip] != 1:
                    reasons.append("Duplicate hwmon chip; control is ambiguous")
                fan_enable = directory / f"fan{number}_enable"
                if fan_enable.exists():
                    try:
                        if self._integer(fan_enable) != 1:
                            reasons.append("Fan channel is disabled")
                    except (OSError, ValueError, UnicodeError):
                        reasons.append("Cannot verify whether the fan channel is enabled")
                # Missing controls (including skipped driver channels) stay visible.
                if not (directory / f"pwm{number}").is_file():
                    reasons.append("PWM control is unavailable")
                if not (directory / f"pwm{number}_enable").is_file():
                    reasons.append("PWM mode control is unavailable")
                identifier = f"mb:{chip}:{number}"
                if counts[chip] != 1:
                    identifier += f"@{directory.name}"
                channels.append(_Channel(
                    identifier, directory, chip, number, name, bool(pump),
                    70 if pump else 30, identity, tuple(reasons),
                ))
        return channels

    def _state(self, channel: _Channel) -> dict:
        errors = list(channel.reasons)
        rpm = None
        pwm = None
        mode = None
        try:
            rpm = self._integer(channel.directory / f"fan{channel.number}_input")
            if rpm < 0:
                raise ValueError("Negative fan RPM")
        except (OSError, ValueError, UnicodeError):
            rpm = None
            errors.append("Fan RPM could not be read")
        try:
            mode = self._integer(channel.path("_enable"))
            if mode not in _MODES:
                raise ValueError("Unknown PWM mode")
        except (OSError, ValueError, UnicodeError):
            mode = None
            errors.append("PWM mode could not be verified")
        try:
            pwm = self._read_pwm(channel, mode)
        except (OSError, ValueError, UnicodeError):
            errors.append("PWM value could not be verified")
        if mode == 0 and pwm != 255:
            errors.append("Full-speed PWM value could not be verified")
        state = {
            "id": channel.id, "kind": "motherboard", "name": channel.name,
            "chip": channel.chip, "rpm": rpm,
            # In firmware mode this register may be a curve/start value rather
            # than the fan's instantaneous duty; do not present it as a speed.
            "percent": (100 if mode == 0 else round(pwm * 100 / 255)
                        if mode == 1 and pwm is not None else None),
            "reported_pwm": pwm,
            "mode": _MODES.get(mode, "unavailable"),
            "min_percent": channel.minimum, "max_percent": 100,
            "controllable": not errors, "pump": channel.pump, "sensor": "cpu",
        }
        if errors:
            state["error"] = "; ".join(dict.fromkeys(errors))
        return state

    def discover(self) -> list[dict]:
        """List every fan input, including channels that cannot be controlled."""
        return [self._state(channel) for channel in self._channels()]

    def _resolve(self, identifier: str, *, write: bool = False) -> _Channel:
        if not isinstance(identifier, str):
            raise ValidationError("Fan ID must be a string returned by discovery")
        matches = [channel for channel in self._channels() if channel.id == identifier]
        if len(matches) != 1:
            raise ValidationError("Fan ID is unavailable or ambiguous; discover devices again")
        channel = matches[0]
        if write:
            state = self._state(channel)
            if not state["controllable"]:
                raise HwmonError(state.get("error", "Fan is read-only"))
        return channel

    def status(self, identifier: str) -> dict:
        return self._state(self._resolve(identifier))

    def validate_speed(self, identifier: str, percent: int) -> int:
        """Validate an integer percentage without touching any control file."""
        if type(percent) is not int:
            raise ValidationError("Speed must be an integer percentage")
        channel = self._resolve(identifier, write=True)
        if not channel.minimum <= percent <= 100:
            raise ValidationError(f"Speed must be between {channel.minimum} and 100 percent")
        return percent

    def _read_pwm(self, channel: _Channel, mode: int | None) -> int | None:
        try:
            pwm = self._integer(channel.path(""))
        except OSError as error:
            # IT57xx has no instantaneous PWM feedback in firmware mode. The
            # pinned driver's mode=2 operation restores its own exact snapshot.
            if channel.chip == "it87952" and mode == 2 and error.errno == errno.ENODATA:
                return None
            raise
        if not 0 <= pwm <= 255:
            raise ValueError("PWM value outside 0..255")
        return pwm

    def _baseline(self, channel: _Channel) -> tuple[int, int | None]:
        try:
            mode = self._integer(channel.path("_enable"))
            pwm = self._read_pwm(channel, mode)
            if mode not in _MODES or (mode == 0 and pwm != 255):
                raise ValueError("Unrecognized mode or PWM value")
            return mode, pwm
        except (OSError, ValueError, UnicodeError) as error:
            raise HwmonError("Cannot verify original fan mode and PWM value; refusing to write") from error

    def snapshot(self, identifier: str) -> object:
        channel = self._resolve(identifier, write=True)
        mode, pwm = self._baseline(channel)
        return _Snapshot(channel.id, channel.identity, mode, pwm, self._snapshot_token)

    def _write_verified(self, path: Path, value: int) -> None:
        # Never use Path.write_text: it creates missing files on ordinary filesystems.
        # Existing-only opens also make disappearing driver channels fail closed.
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(f"{value}\n")
            output.flush()
        actual = self._integer(path)
        if actual != value:
            # Conventional IT8696 channels report mode 0 whenever duty is 255,
            # even after a successful mode=1 write. Verify the safe full duty.
            if path.name.endswith("_enable") and value == 1 and actual == 0:
                if self._integer(path.with_name(path.name.removesuffix("_enable"))) == 255:
                    return
            raise HwmonError(f"{path.name} readback mismatch: wanted {value}, got {actual}")

    def _full_speed_fallback(self, channel: _Channel) -> str:
        """Mode 0 forces 255 in the supported driver without a manual-mode dip."""
        failures = []
        try:
            self._write_verified(channel.path("_enable"), 0)
            if self._baseline(channel) == (0, 255):
                return "full-speed fallback verified (full mode)"
        except (OSError, ValueError, UnicodeError, HwmonError) as error:
            failures.append(f"{channel.path('_enable').name}: {error}")
        try:
            mode, pwm = self._baseline(channel)
            if mode in (0, 1) and pwm == 255:
                return "full-speed fallback verified"
            else:
                failures.append("Full-speed state was not verified")
        except HwmonError as error:
            failures.append(str(error))
        return "full-speed fallback could not be verified: " + "; ".join(failures)

    def _take_manual(self, channel: _Channel) -> None:
        # The supported community driver rejects PWM writes in mode 0/2.
        # Mode 0 establishes 255 and snapshots firmware before enabling manual.
        self._write_verified(channel.path("_enable"), 0)
        if self._baseline(channel) != (0, 255):
            raise HwmonError("Full speed was not verified before taking manual control")
        self._write_verified(channel.path("_enable"), 1)

    def set_speed(self, identifier: str, percent: int) -> dict:
        self.validate_speed(identifier, percent)
        channel = self._resolve(identifier, write=True)
        original_mode, _ = self._baseline(channel)
        pwm = (percent * 255 + 50) // 100
        try:
            if original_mode != 1:
                self._take_manual(channel)
            self._write_verified(channel.path(""), pwm)
            mode, actual_pwm = self._baseline(channel)
            if (mode != 1 and not (mode == 0 and pwm == 255)) or actual_pwm != pwm:
                raise HwmonError("Fan state changed before verification")
        except (OSError, ValueError, UnicodeError, HwmonError) as error:
            fallback = self._full_speed_fallback(channel)
            raise HwmonError(f"Setting fan speed failed: {error}; {fallback}") from error
        return self.status(identifier)

    def full(self, identifier: str) -> dict:
        """Set verified 100% duty in manual mode."""
        return self.set_speed(identifier, 100)

    def restore(self, identifier: str, snapshot: object) -> dict:
        if not isinstance(snapshot, _Snapshot) or snapshot.token is not self._snapshot_token:
            raise ValidationError("Snapshot was not created by this backend")
        if snapshot.channel_id != identifier:
            raise ValidationError("Snapshot belongs to a different fan")
        # An owned fan still needs recovery if its tachometer becomes unreadable.
        # Revalidate discovery/board/chip/control paths, then independently check
        # the snapshot identity and control registers; RPM is not a prerequisite.
        channel = self._resolve(identifier)
        if channel.reasons:
            raise HwmonError("; ".join(channel.reasons))
        if snapshot.identity != channel.identity:
            raise HwmonError("Fan device changed since the snapshot; refusing to restore")
        if snapshot.mode not in _MODES or not (
            (snapshot.mode == 2 and snapshot.pwm is None)
            or (type(snapshot.pwm) is int and 0 <= snapshot.pwm <= 255)
        ):
            raise ValidationError("Invalid original fan state")
        try:
            current_mode, _ = self._baseline(channel)
            if snapshot.mode == 1:
                if current_mode != 1:
                    self._take_manual(channel)
                self._write_verified(channel.path(""), snapshot.pwm)
            else:
                # Mode 2 restores the driver's exact firmware vector snapshot;
                # writing a duty afterward would fail EBUSY or corrupt firmware
                # ownership. Mode 0 itself restores the verified full duty.
                self._write_verified(channel.path("_enable"), snapshot.mode)
            actual_mode, actual_pwm = self._baseline(channel)
            if actual_mode != snapshot.mode or (snapshot.mode != 2 and actual_pwm != snapshot.pwm):
                raise HwmonError("Restored fan state did not match the original state")
        except (OSError, ValueError, UnicodeError, HwmonError) as error:
            fallback = self._full_speed_fallback(channel)
            raise HwmonError(f"Restoring fan control failed: {error}; {fallback}") from error
        return self.status(identifier)

    def cpu_temperature(self) -> float | None:
        """Read Tctl from k10temp, rejecting missing or implausible measurements."""
        temperatures = []
        for directory, chip in self._directories():
            if chip != "k10temp":
                continue
            # Tctl normally uses temp1_input; respect an explicit Tctl label.
            try:
                labels = sorted(directory.glob("temp*_label"))
            except OSError:
                labels = []
            inputs = [label.with_name(label.name.removesuffix("_label") + "_input")
                      for label in labels if self._read_text(label) == "Tctl"]
            if not inputs:
                label = self._read_text(directory / "temp1_label")
                if label and label != "Tctl":
                    continue
                inputs = [directory / "temp1_input"]
            for input_path in inputs:
                try:
                    value = self._integer(input_path) / 1000
                except (OSError, ValueError, UnicodeError):
                    continue
                if 0 <= value <= 125:
                    temperatures.append(value)
        return max(temperatures) if temperatures else None
