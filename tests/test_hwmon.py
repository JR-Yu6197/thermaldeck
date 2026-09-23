"""Offline fake-sysfs tests: never access real hardware control files."""

from pathlib import Path
import errno
import re
import tempfile
import unittest

from thermaldeck.hwmon import HwmonBackend, HwmonError, SUPPORTED_BOARD, ValidationError


class RecordingBackend(HwmonBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes = []
        self.fail_once = None
        self.fail_always = None
        self.mismatch_once = None
        self.firmware = {}

    def _integer(self, path):
        # Model the pinned driver's documented sysfs semantics, including the
        # IT8696 full-duty mode alias and IT57xx missing firmware PWM feedback.
        chip = (path.parent / "name").read_text().strip() if (path.parent / "name").exists() else ""
        if re.fullmatch(r"pwm[0-9]+", path.name):
            mode = path.with_name(path.name + "_enable")
            if chip in ("it87952", "it87952_a10a090a") and mode.exists() and mode.read_text().strip() == "2":
                raise OSError(errno.ENODATA, "No live PWM feedback in firmware mode")
        value = super()._integer(path)
        if chip in ("it8696", "it8696_a10a090a") and path.name.endswith("_enable") and value == 1:
            pwm = path.with_name(path.name.removesuffix("_enable"))
            if super()._integer(pwm) == 255:
                return 0
        return value

    def _write_verified(self, path, value):
        self.writes.append((path.name, value))
        if self.fail_once == (path.name, value):
            self.fail_once = None
            raise OSError("Injected write failure")
        if self.fail_always == path.name:
            raise OSError("Injected persistent write failure")
        if self.mismatch_once == (path.name, value):
            self.mismatch_once = None
            path.write_text("42\n", encoding="ascii")
            raise HwmonError("Injected readback mismatch")
        if path.name.endswith("_enable"):
            pwm_path = path.with_name(path.name.removesuffix("_enable"))
            if value == 0:
                if path not in self.firmware:
                    self.firmware[path] = (int(path.read_text()), int(pwm_path.read_text()))
                pwm_path.write_text("255\n", encoding="ascii")
            elif value == 2 and path in self.firmware:
                _, old_pwm = self.firmware.pop(path)
                pwm_path.write_text(str(old_pwm), encoding="ascii")
        elif re.fullmatch(r"pwm[0-9]+", path.name):
            mode = int(path.with_name(path.name + "_enable").read_text())
            if mode != 1:
                raise OSError(errno.EBUSY, "Duty is only writable in manual mode")
        return super()._write_verified(path, value)


class HwmonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "hwmon"
        self.root.mkdir()
        self.board = self.base / "board_name"
        self.board.write_text(SUPPORTED_BOARD + "\n", encoding="ascii")
        self.backend = RecordingBackend(self.root, self.board)
        self.chip = self.add_chip("hwmon0", "it8696", {1: (1200, 102, 2), 5: (2000, 204, 2)})
        self.fan_id = "mb:it8696:1"

    def add_chip(self, entry, chip, fans=None):
        directory = self.root / entry
        directory.mkdir()
        (directory / "name").write_text(chip + "\n", encoding="ascii")
        for number, (rpm, pwm, mode) in (fans or {}).items():
            (directory / f"fan{number}_input").write_text(str(rpm), encoding="ascii")
            if pwm is not None:
                (directory / f"pwm{number}").write_text(str(pwm), encoding="ascii")
            if mode is not None:
                (directory / f"pwm{number}_enable").write_text(str(mode), encoding="ascii")
        return directory

    def test_discovery_mapping_and_conservative_limits(self):
        self.add_chip("hwmon1", "it87952", {n: (900, 230, 1) for n in range(1, 6)})
        states = {item["id"]: item for item in self.backend.discover()}
        self.assertEqual(states[self.fan_id]["name"], "CPU_FAN")
        self.assertEqual(states[self.fan_id]["mode"], "hardware")
        self.assertIsNone(states[self.fan_id]["percent"])
        self.assertEqual(states[self.fan_id]["reported_pwm"], 102)
        self.assertTrue(states[self.fan_id]["controllable"])
        self.assertEqual(states["mb:it8696:5"]["min_percent"], 70)
        self.assertTrue(states["mb:it8696:5"]["pump"])
        self.assertEqual(states["mb:it87952:1"]["name"], "SYS_FAN5_PUMP")
        self.assertEqual(states["mb:it87952:3"]["name"], "SYS_FAN4")
        self.assertEqual(states["mb:it87952:3"]["min_percent"], 30)
        self.assertEqual(states["mb:it87952:5"]["name"], "SYS_FAN8_PUMP")
        self.assertEqual(self.backend.writes, [])

    def test_unsupported_board_remains_read_only(self):
        self.board.write_text(SUPPORTED_BOARD + " REV 2", encoding="ascii")
        state = self.backend.status(self.fan_id)
        self.assertEqual(state["rpm"], 1200)
        self.assertFalse(state["controllable"])
        with self.assertRaises(HwmonError):
            self.backend.set_speed(self.fan_id, 50)
        self.assertEqual(self.backend.writes, [])

    def test_unknown_chip_and_missing_controls_still_show_rpm(self):
        self.add_chip("hwmon3", "otherchip", {1: (444, None, None)})
        states = {state["id"]: state for state in self.backend.discover()}
        other = states["mb:otherchip:1"]
        self.assertEqual(other["rpm"], 444)
        self.assertEqual(other["mode"], "unavailable")
        self.assertFalse(other["controllable"])
        (self.chip / "pwm1_enable").unlink()
        self.assertFalse(self.backend.status(self.fan_id)["controllable"])
        with self.assertRaises(HwmonError):
            self.backend.full(self.fan_id)
        self.assertEqual(self.backend.writes, [])

    def test_disabled_channel_is_read_only_but_zero_rpm_is_not_disabled(self):
        (self.chip / "fan1_input").write_text("0", encoding="ascii")
        self.assertTrue(self.backend.status(self.fan_id)["controllable"])
        (self.chip / "fan1_enable").write_text("0", encoding="ascii")
        self.assertFalse(self.backend.status(self.fan_id)["controllable"])
        self.assertEqual(self.backend.status(self.fan_id)["rpm"], 0)

    def test_duplicate_chips_have_distinct_read_only_ids(self):
        self.add_chip("hwmon2", "it8696", {1: (600, 90, 1)})
        states = self.backend.discover()
        self.assertEqual(len({state["id"] for state in states}), len(states))
        self.assertTrue(all(not state["controllable"] for state in states))
        with self.assertRaises(ValidationError):
            self.backend.set_speed(self.fan_id, 50)
        with self.assertRaises(HwmonError):
            self.backend.set_speed("mb:it8696:1@hwmon0", 50)
        self.assertEqual(self.backend.writes, [])

    def test_verified_board_siv_names_use_canonical_ids_and_mapping(self):
        (self.chip / "name").write_text("it8696_a10a090a", encoding="ascii")
        self.add_chip("hwmon1", "it87952_a10a090a", {4: (5000, 200, 2)})
        states = {state["id"]: state for state in self.backend.discover()}
        self.assertEqual(states[self.fan_id]["chip"], "it8696")
        self.assertEqual(states[self.fan_id]["name"], "CPU_FAN")
        self.assertTrue(states[self.fan_id]["controllable"])
        pump = states["mb:it87952:4"]
        self.assertEqual(pump["chip"], "it87952")
        self.assertEqual(pump["name"], "SYS_FAN7_PUMP")
        self.assertEqual(pump["min_percent"], 70)
        self.assertTrue(pump["controllable"])
        self.assertIsNone(pump["reported_pwm"])
        self.assertEqual(self.backend.writes, [])

    def test_unverified_siv_suffix_is_read_only(self):
        for name in ("it8696_other", "it8696_a10a090b", "it8696_a10a090a_extra"):
            with self.subTest(name=name):
                (self.chip / "name").write_text(name, encoding="ascii")
                state = self.backend.status(f"mb:{name}:1")
                self.assertEqual(state["rpm"], 1200)
                self.assertFalse(state["controllable"])
                with self.assertRaises(HwmonError):
                    self.backend.full(state["id"])
        self.assertEqual(self.backend.writes, [])

    def test_siv_and_plain_names_count_as_duplicate_canonical_chip(self):
        self.add_chip("hwmon1", "it8696_a10a090a", {1: (1200, 102, 2)})
        states = self.backend.discover()
        self.assertEqual(len(states), 3)
        self.assertEqual(len({state["id"] for state in states}), 3)
        self.assertTrue(all(state["chip"] == "it8696" for state in states))
        self.assertTrue(all(not state["controllable"] for state in states))
        self.assertEqual(self.backend.writes, [])

    def test_verified_siv_name_still_requires_exact_board(self):
        (self.chip / "name").write_text("it8696_a10a090a", encoding="ascii")
        self.board.write_text("Unverified board", encoding="ascii")
        self.assertFalse(self.backend.status(self.fan_id)["controllable"])
        with self.assertRaises(HwmonError):
            self.backend.snapshot(self.fan_id)
        self.assertEqual(self.backend.writes, [])

    def test_invalid_input_never_writes(self):
        for percent in (True, False, 50.0, "50", None, -1, 0, 29, 101):
            with self.subTest(percent=percent), self.assertRaises(ValidationError):
                self.backend.set_speed(self.fan_id, percent)
        for identifier in ("../../outside", str(self.chip / "pwm1"), None, 1):
            with self.subTest(identifier=identifier), self.assertRaises(ValidationError):
                self.backend.full(identifier)
        with self.assertRaises(ValidationError):
            self.backend.set_speed("mb:it8696:5", 69)
        self.assertEqual(self.backend.writes, [])

    def test_hardware_to_manual_orders_full_duty_before_enabling(self):
        state = self.backend.set_speed(self.fan_id, 50)
        self.assertEqual(self.backend.writes, [("pwm1_enable", 0), ("pwm1_enable", 1), ("pwm1", 128)])
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["percent"], 50)

    def test_manual_speed_change_does_not_toggle_mode(self):
        (self.chip / "pwm1_enable").write_text("1", encoding="ascii")
        self.backend.set_speed(self.fan_id, 30)
        self.assertEqual(self.backend.writes, [("pwm1", 77)])

    def test_auto_firmware_register_is_not_presented_as_instantaneous_duty(self):
        for raw_pwm in (0, 102, 255):
            with self.subTest(raw_pwm=raw_pwm):
                (self.chip / "pwm1").write_text(str(raw_pwm), encoding="ascii")
                state = self.backend.status(self.fan_id)
                self.assertEqual(state["mode"], "hardware")
                self.assertIsNone(state["percent"])
                self.assertEqual(state["reported_pwm"], raw_pwm)
                self.assertEqual(state["rpm"], 1200)

    def test_full_and_full_mode_reporting(self):
        (self.chip / "pwm1_enable").write_text("0", encoding="ascii")
        (self.chip / "pwm1").write_text("255", encoding="ascii")
        self.assertEqual(self.backend.status(self.fan_id)["mode"], "full")
        self.assertEqual(self.backend.status(self.fan_id)["percent"], 100)
        state = self.backend.full(self.fan_id)
        self.assertEqual(state["percent"], 100)
        self.assertEqual((self.chip / "pwm1").read_text().strip(), "255")

    def test_unknown_original_mode_refuses_all_writes(self):
        (self.chip / "pwm1_enable").write_text("3", encoding="ascii")
        self.assertFalse(self.backend.status(self.fan_id)["controllable"])
        for action in (lambda: self.backend.full(self.fan_id), lambda: self.backend.snapshot(self.fan_id)):
            with self.assertRaises(HwmonError):
                action()
        self.assertEqual(self.backend.writes, [])

    def test_exact_restore_including_zero_manual_pwm_and_modes_zero_one_two(self):
        for original_mode in (0, 1, 2):
            with self.subTest(mode=original_mode):
                original_pwm = {0: 255, 1: 0, 2: 102}[original_mode]
                self.backend.firmware.clear()
                (self.chip / "pwm1").write_text(str(original_pwm), encoding="ascii")
                (self.chip / "pwm1_enable").write_text(str(original_mode), encoding="ascii")
                saved = self.backend.snapshot(self.fan_id)
                self.backend.set_speed(self.fan_id, 70)
                state = self.backend.restore(self.fan_id, saved)
                self.assertEqual((self.chip / "pwm1").read_text().strip(), str(original_pwm))
                self.assertEqual((self.chip / "pwm1_enable").read_text().strip(), str(original_mode))
                self.assertEqual(state["mode"], {0: "full", 1: "manual", 2: "hardware"}[original_mode])

    def test_restore_to_manual_uses_safe_transition_order(self):
        (self.chip / "pwm1_enable").write_text("1", encoding="ascii")
        saved = self.backend.snapshot(self.fan_id)
        (self.chip / "pwm1_enable").write_text("2", encoding="ascii")
        self.backend.restore(self.fan_id, saved)
        self.assertEqual(self.backend.writes, [("pwm1_enable", 0), ("pwm1_enable", 1), ("pwm1", 102)])

    def test_snapshot_rejects_foreign_backend_or_channel(self):
        saved = self.backend.snapshot(self.fan_id)
        other_backend = RecordingBackend(self.root, self.board)
        for value in ({"mode": 2, "pwm": 102}, None):
            with self.assertRaises(ValidationError):
                self.backend.restore(self.fan_id, value)
        with self.assertRaises(ValidationError):
            other_backend.restore(self.fan_id, saved)
        with self.assertRaises(ValidationError):
            self.backend.restore("mb:it8696:5", saved)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(other_backend.writes, [])

    def test_replaced_device_cannot_receive_old_snapshot(self):
        saved = self.backend.snapshot(self.fan_id)
        self.chip.rename(self.root / "removed-device")
        self.add_chip("hwmon0", "it8696", {1: (1300, 200, 2)})
        with self.assertRaises(HwmonError):
            self.backend.restore(self.fan_id, saved)
        self.assertEqual(self.backend.writes, [])

    def test_partial_failure_raises_and_falls_back_to_full(self):
        self.backend.fail_once = ("pwm1", 128)
        with self.assertRaisesRegex(HwmonError, "full-speed fallback verified"):
            self.backend.set_speed(self.fan_id, 50)
        self.assertEqual(self.backend.status(self.fan_id)["percent"], 100)
        self.assertEqual((self.chip / "pwm1_enable").read_text().strip(), "0")

    def test_readback_mismatch_is_not_reported_as_success(self):
        self.backend.mismatch_once = ("pwm1", 128)
        with self.assertRaisesRegex(HwmonError, "readback mismatch"):
            self.backend.set_speed(self.fan_id, 50)
        self.assertEqual(self.backend.status(self.fan_id)["percent"], 100)

    def test_persistent_duty_failure_enters_manual_only_after_full_speed(self):
        self.backend.fail_always = "pwm1"
        with self.assertRaisesRegex(HwmonError, "full-speed fallback verified"):
            self.backend.set_speed(self.fan_id, 50)
        self.assertEqual(self.backend.writes[:2], [("pwm1_enable", 0), ("pwm1_enable", 1)])
        self.assertEqual(self.backend.status(self.fan_id)["mode"], "full")

    def test_restore_failure_raises_after_full_speed_fallback(self):
        saved = self.backend.snapshot(self.fan_id)
        self.backend.full(self.fan_id)
        self.backend.fail_once = ("pwm1_enable", 2)
        with self.assertRaisesRegex(HwmonError, "Restoring fan control failed.*full-speed fallback verified"):
            self.backend.restore(self.fan_id, saved)
        self.assertEqual(self.backend.status(self.fan_id)["percent"], 100)

    def test_owned_fan_restores_when_rpm_becomes_unreadable(self):
        saved = self.backend.snapshot(self.fan_id)
        self.backend.set_speed(self.fan_id, 30)
        (self.chip / "fan1_input").write_text("unreadable", encoding="ascii")
        self.backend.writes.clear()
        restored = self.backend.restore(self.fan_id, saved)
        self.assertEqual(restored["mode"], "hardware")
        self.assertIsNone(restored["rpm"])
        self.assertEqual(self.backend.writes, [("pwm1_enable", 2)])
        # A new takeover still requires readable RPM.
        with self.assertRaises(HwmonError):
            self.backend.set_speed(self.fan_id, 30)

    def test_unverifiable_current_control_attempts_full_speed_during_restore(self):
        saved = self.backend.snapshot(self.fan_id)
        self.backend.set_speed(self.fan_id, 30)
        (self.chip / "pwm1").write_text("unreadable", encoding="ascii")
        self.backend.writes.clear()
        with self.assertRaisesRegex(HwmonError, "full-speed fallback verified"):
            self.backend.restore(self.fan_id, saved)
        self.assertEqual(self.backend.writes, [("pwm1_enable", 0)])
        self.assertEqual(self.backend.status(self.fan_id)["percent"], 100)

    def test_restore_with_rpm_failure_still_rejects_changed_board(self):
        saved = self.backend.snapshot(self.fan_id)
        self.backend.set_speed(self.fan_id, 30)
        (self.chip / "fan1_input").write_text("unreadable", encoding="ascii")
        self.board.write_text("Different board", encoding="ascii")
        self.backend.writes.clear()
        with self.assertRaises(HwmonError):
            self.backend.restore(self.fan_id, saved)
        self.assertEqual(self.backend.writes, [])

    def test_all_writes_fail_reports_unverified_fallback(self):
        self.backend._write_verified = lambda *_: (_ for _ in ()).throw(OSError("Denied"))
        with self.assertRaisesRegex(HwmonError, "fallback could not be verified"):
            self.backend.set_speed(self.fan_id, 50)

    def test_disappearing_control_is_not_recreated(self):
        path = self.chip / "pwm1"
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            HwmonBackend._write_verified(self.backend, path, 255)
        self.assertFalse(path.exists())

    def test_it57xx_missing_auto_duty_supports_snapshot_and_firmware_restore(self):
        self.add_chip("hwmon1", "it87952", {1: (1200, 200, 2)})
        identifier = "mb:it87952:1"
        state = self.backend.status(identifier)
        self.assertTrue(state["controllable"])
        self.assertIsNone(state["percent"])
        original = self.backend.snapshot(identifier)
        self.backend.set_speed(identifier, 80)
        self.backend.writes.clear()
        restored = self.backend.restore(identifier, original)
        self.assertEqual(restored["mode"], "hardware")
        self.assertIsNone(restored["percent"])
        self.assertEqual(self.backend.writes, [("pwm1_enable", 2)])

    def test_full_mode_with_nonfull_duty_is_not_trusted(self):
        (self.chip / "pwm1_enable").write_text("0", encoding="ascii")
        self.assertFalse(self.backend.status(self.fan_id)["controllable"])
        with self.assertRaises(HwmonError):
            self.backend.snapshot(self.fan_id)
        self.assertEqual(self.backend.writes, [])

    def test_temperature_prefers_tctl_label_and_rejects_bad_values(self):
        temperature = self.add_chip("hwmon4", "k10temp")
        (temperature / "temp1_input").write_text("60000", encoding="ascii")
        (temperature / "temp1_label").write_text("Tdie", encoding="ascii")
        (temperature / "temp3_label").write_text("Tctl", encoding="ascii")
        (temperature / "temp3_input").write_text("80125", encoding="ascii")
        self.assertEqual(self.backend.cpu_temperature(), 80.125)
        for value in ("126000", "-1000", "nan", "80000oops"):
            (temperature / "temp3_input").write_text(value, encoding="ascii")
            self.assertIsNone(self.backend.cpu_temperature())

    def test_temperature_standard_unlabelled_tctl_and_missing_hwmon(self):
        temperature = self.add_chip("hwmon4", "k10temp")
        (temperature / "temp1_input").write_text("79000", encoding="ascii")
        self.assertEqual(self.backend.cpu_temperature(), 79.0)
        missing = HwmonBackend(self.base / "missing", self.board)
        self.assertEqual(missing.discover(), [])
        self.assertIsNone(missing.cpu_temperature())


if __name__ == "__main__":
    unittest.main()
