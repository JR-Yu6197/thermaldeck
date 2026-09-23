"""Offline NVML contract tests; no library is loaded and no GPU is touched."""
import unittest

from thermaldeck.nvidia import NvidiaBackend, NvidiaError


class FakeNvml:
    def __init__(self):
        self.cards = {
            "GPU-second": {"name": "RTX Second", "handle": object(), "temperature": 45,
                           "fans": [{"policy": 0, "percent": 40, "target": 40} for _ in range(2)]},
            "GPU-first": {"name": "RTX First", "handle": object(), "temperature": 50,
                          "fans": [{"policy": 0, "percent": 35, "target": 35} for _ in range(2)]},
        }
        self.writes = []
        self.handle_requests = []
        self.fail_set = set()
        self.fail_auto = set()
        self.ignore_set = set()
        self.maximum = 100
        self.minimum = 30
        self.closed = False

    def devices(self):
        return [(uuid, data["name"]) for uuid, data in self.cards.items()]

    def handle(self, uuid):
        self.handle_requests.append(uuid)
        return self.cards[uuid]["handle"]

    def _card(self, handle):
        for uuid, data in self.cards.items():
            if data["handle"] is handle:
                return uuid, data
        raise NvidiaError("Unknown fake handle")

    def string(self, name, handle):
        self.assert_function(name, "nvmlDeviceGetName")
        return self._card(handle)[1]["name"]

    @staticmethod
    def assert_function(actual, expected):
        if actual != expected:
            raise AssertionError(f"Unexpected NVML function: {actual}")

    def uint(self, name, handle, *args):
        _, card = self._card(handle)
        if name == "nvmlDeviceGetTemperature":
            return card["temperature"]
        if name == "nvmlDeviceGetNumFans":
            return len(card["fans"])
        fields = {"nvmlDeviceGetFanControlPolicy_v2": "policy",
                  "nvmlDeviceGetFanSpeed_v2": "percent", "nvmlDeviceGetTargetFanSpeed": "target"}
        return card["fans"][args[0]][fields[name]]

    def limits(self, handle):
        self._card(handle)
        return self.minimum, self.maximum

    def call(self, name, handle, index, *args):
        uuid, card = self._card(handle)
        self.writes.append((name, uuid, handle, index, *args))
        key = (uuid, index)
        if name == "nvmlDeviceSetFanSpeed_v2":
            if key in self.fail_set:
                raise NvidiaError("Injected set failure")
            if key not in self.ignore_set:
                card["fans"][index].update(policy=1, target=args[0], percent=args[0])
        elif name == "nvmlDeviceSetDefaultFanSpeed_v2":
            if key in self.fail_auto:
                raise NvidiaError("Injected automatic-restore failure")
            card["fans"][index]["policy"] = 0
        else:
            raise AssertionError(f"Unexpected write function: {name}")

    def close(self):
        self.closed = True


class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.nvml = FakeNvml()
        self.backend = NvidiaBackend(self.nvml)
        self.device = "gpu:GPU-first"

    def test_discovery_and_temperature_never_write(self):
        states = self.backend.discover()
        self.assertEqual([state["id"] for state in states], ["gpu:GPU-second", self.device])
        self.assertEqual(self.backend.temperature(self.device), 50)
        self.assertTrue(all(state["controllable"] for state in states))
        self.assertTrue(all(state["mode"] == "auto" for state in states))
        self.assertEqual(self.nvml.writes, [])

    def test_invalid_speed_types_and_bounds_never_write(self):
        for percent in (True, False, 45.0, "45", None, -1, 0, 29, 101):
            with self.subTest(percent=percent), self.assertRaises(NvidiaError):
                self.backend.set_speed(self.device, percent)
        self.assertEqual(self.nvml.writes, [])

    def test_driver_limits_are_respected_without_writes(self):
        self.nvml.minimum, self.nvml.maximum = 40, 90
        for percent in (39, 91):
            with self.assertRaises(NvidiaError):
                self.backend.set_speed(self.device, percent)
        self.assertEqual(self.nvml.writes, [])

    def test_unknown_and_changed_identity_never_write(self):
        for device in (None, 0, "gpu:0", "gpu:GPU-missing", "mb:it8696:1"):
            with self.subTest(device=device), self.assertRaises(NvidiaError):
                self.backend.set_speed(device, 60)
        self.nvml.cards["GPU-first"]["name"] = "Replacement GPU"
        with self.assertRaises(NvidiaError):
            self.backend.set_speed(self.device, 60)
        self.assertEqual(self.nvml.writes, [])

    def test_uuid_target_uses_same_handle_for_both_fans(self):
        state = self.backend.set_speed(self.device, 60)
        self.assertEqual(state["mode"], "manual")
        self.assertEqual(state["target_percent"], 60)
        self.assertEqual(len(self.nvml.writes), 2)
        for index, call in enumerate(self.nvml.writes):
            name, uuid, handle, fan, percent = call
            self.assertEqual((name, uuid, fan, percent), ("nvmlDeviceSetFanSpeed_v2", "GPU-first", index, 60))
            self.assertIs(handle, self.nvml.cards["GPU-first"]["handle"])
        self.assertTrue(all(uuid == "GPU-first" for uuid in self.nvml.handle_requests))
        self.assertTrue(all(fan["policy"] == 0 for fan in self.nvml.cards["GPU-second"]["fans"]))

    def test_second_fan_failure_restores_every_fan_on_target_only(self):
        self.nvml.fail_set.add(("GPU-first", 1))
        with self.assertRaisesRegex(NvidiaError, "자동 제어로 복귀"):
            self.backend.set_speed(self.device, 65)
        calls = [(call[0], call[1], call[3]) for call in self.nvml.writes]
        self.assertEqual(calls, [("nvmlDeviceSetFanSpeed_v2", "GPU-first", 0),
                                 ("nvmlDeviceSetFanSpeed_v2", "GPU-first", 1),
                                 ("nvmlDeviceSetDefaultFanSpeed_v2", "GPU-first", 0),
                                 ("nvmlDeviceSetDefaultFanSpeed_v2", "GPU-first", 1)])
        self.assertTrue(all(fan["policy"] == 0 for fan in self.nvml.cards["GPU-first"]["fans"]))

    def test_failed_readback_is_not_success_and_restores_target(self):
        self.nvml.ignore_set.add(("GPU-first", 1))
        with self.assertRaises(NvidiaError):
            self.backend.set_speed(self.device, 55)
        self.assertEqual([call[3] for call in self.nvml.writes if call[0].endswith("DefaultFanSpeed_v2")], [0, 1])

    def test_auto_attempts_other_fans_after_failure(self):
        self.backend.set_speed(self.device, 60)
        self.nvml.writes.clear()
        self.nvml.fail_auto.add(("GPU-first", 0))
        with self.assertRaisesRegex(NvidiaError, "자동 복귀 확인 실패"):
            self.backend.auto(self.device)
        self.assertEqual([call[3] for call in self.nvml.writes], [0, 1])
        self.assertEqual(self.nvml.cards["GPU-first"]["fans"][1]["policy"], 0)

    def test_partial_set_and_restore_failure_are_both_reported(self):
        self.nvml.fail_set.add(("GPU-first", 1))
        self.nvml.fail_auto.add(("GPU-first", 0))
        with self.assertRaisesRegex(NvidiaError, "설정 실패:.*자동 복귀 실패"):
            self.backend.set_speed(self.device, 70)
        self.assertEqual([call[3] for call in self.nvml.writes if call[0].endswith("DefaultFanSpeed_v2")], [0, 1])

    def test_invalid_temperature_prevents_control(self):
        for temperature in (-1, 126, float("nan"), float("inf")):
            self.nvml.cards["GPU-first"]["temperature"] = temperature
            with self.subTest(temperature=temperature), self.assertRaises(NvidiaError):
                self.backend.set_speed(self.device, 50)
        self.assertEqual(self.nvml.writes, [])

    def test_no_fans_refuses_control_and_auto_without_writes(self):
        self.nvml.cards["GPU-first"]["fans"] = []
        self.assertFalse(self.backend.status(self.device)["controllable"])
        with self.assertRaises(NvidiaError):
            self.backend.auto(self.device)
        with self.assertRaises(NvidiaError):
            self.backend.set_speed(self.device, 50)
        self.assertEqual(self.nvml.writes, [])

    def test_close_only_shuts_down_nvml(self):
        self.backend.close()
        self.assertTrue(self.nvml.closed)
        self.assertEqual(self.nvml.writes, [])


if __name__ == "__main__":
    unittest.main()
