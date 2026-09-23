"""Session safety tests using an entirely in-memory hardware backend."""
import unittest

from thermaldeck.control import ControlSession, curve_percent, validate_curve


GPU = "gpu:GPU-test"
MB = "mb:it8696:1"
CASE = "mb:it8696:2"


class FakeDriver:
    def __init__(self, devices):
        self.states = {device: {"id": device, "controllable": True, "min_percent": 30,
                                "max_percent": 100, "mode": "auto", "percent": 40}
                       for device in devices}
        self.writes = []
        self.snapshots = []
        self.fail_set = set()
        self.fail_restore = set()

    def status(self, device):
        return self.states[device].copy()

    def validate_speed(self, device, percent):
        state = self.status(device)
        if type(percent) is not int or not state["min_percent"] <= percent <= state["max_percent"]:
            raise ValueError("Invalid percent")
        return percent

    def snapshot(self, device):
        self.snapshots.append(device)
        return self.status(device)

    def set_speed(self, device, percent):
        self.validate_speed(device, percent)
        self.writes.append(("set", device, percent))
        self.states[device].update(mode="manual", percent=percent)
        if device in self.fail_set:
            raise RuntimeError("Injected partial set failure")
        return self.status(device)

    def restore(self, device, saved):
        self.writes.append(("restore", device))
        if device in self.fail_restore:
            raise RuntimeError("Injected restoration failure")
        self.states[device] = saved.copy()
        return self.status(device)

    def auto(self, device):
        self.writes.append(("auto", device))
        if device in self.fail_restore:
            raise RuntimeError("Injected restoration failure")
        self.states[device]["mode"] = "auto"
        return self.status(device)


class FakeBackend:
    def __init__(self):
        self.mb = FakeDriver((MB, CASE))
        self.gpu = FakeDriver((GPU,))
        self.temperatures = {"cpu": 50, GPU: 45}

    def device_backend(self, device):
        if device in self.mb.states:
            return self.mb
        if device in self.gpu.states:
            return self.gpu
        raise ValueError("Unknown fake device")

    def temperature(self, sensor):
        value = self.temperatures[sensor]
        if isinstance(value, Exception):
            raise value
        return value

    def snapshot(self):
        return {"devices": [state.copy() for driver in (self.mb, self.gpu) for state in driver.states.values()],
                "cpu_temperature": self.temperatures["cpu"], "errors": []}

    @property
    def writes(self):
        return self.mb.writes + self.gpu.writes


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.now = 0
        self.session = ControlSession(self.backend, clock=lambda: self.now)

    def request_set(self, device=MB, percent=50, **extra):
        return self.session.request({"action": "set", "device": device, "percent": percent, **extra})

    def request_curve(self, device=MB, points=None, **extra):
        return self.session.request({"action": "curve", "device": device,
                                     "points": points if points is not None else [[30, 30], [80, 100]], **extra})

    def test_start_status_and_unowned_restore_never_write(self):
        self.backend.mb.states[MB].update(mode="manual", percent=75)
        self.session.status()
        self.session.tick()
        self.session.restore(MB)
        self.session.restore(GPU)
        self.session.restore_all()
        self.session.request({"action": "heartbeat"})
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.mb.states[MB]["percent"], 75)
        self.assertEqual(self.session.baselines, {})

    def test_invalid_manual_requests_do_not_take_ownership(self):
        for percent in (True, False, 55.0, "55", None, 0, 29, 101):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                self.request_set(percent=percent)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.mb.snapshots, [])
        self.assertEqual(self.session.baselines, {})
        self.assertEqual(self.session.owned, {})

    def test_invalid_curves_do_not_take_ownership(self):
        cases = [[], [[40, 40]], [[40, 40], [40, 60]], [[60, 70], [80, 60]],
                 [[20, True], [80, 100]], [[20, 30.0], [80, 100]],
                 [[float("nan"), 40], [80, 100]], [[True, 40], [80, 100]],
                 [[-1, 40], [80, 100]], [[20, 29], [80, 100]], [[20, 40], [101, 100]]]
        for points in cases:
            with self.subTest(points=points), self.assertRaises(ValueError):
                self.request_curve(points=points)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.mb.snapshots, [])
        self.assertEqual(self.session.owned, {})
        self.assertEqual(self.session.baselines, {})

    def test_failed_temperature_at_startup_never_writes_or_owns(self):
        for temperature in (None, True, -1, 126, float("nan"), float("inf"), OSError("Missing sensor")):
            self.backend.temperatures["cpu"] = temperature
            with self.subTest(temperature=temperature), self.assertRaises((ValueError, OSError)):
                self.request_set()
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.backend.mb.snapshots, [])
        self.assertEqual(self.session.baselines, {})
        self.assertEqual(self.session.owned, {})

    def test_runtime_temperature_failure_restores_only_owned_device(self):
        self.request_set()
        self.backend.mb.writes.clear()
        self.backend.temperatures["cpu"] = OSError("Missing sensor")
        self.session.tick()
        self.assertEqual(self.backend.writes, [("restore", MB)])
        self.assertEqual(self.session.owned, {})
        self.assertEqual(self.session.baselines, {})
        self.assertTrue(self.session.events)

    def test_cpu_overtemperature_forces_full_speed_immediately(self):
        self.request_set(percent=40)
        self.backend.temperatures["cpu"] = 90
        self.session.tick()
        self.assertEqual(self.backend.mb.writes[-1], ("set", MB, 100))
        self.assertTrue(self.session.owned[MB]["thermal_override"])
        self.assertEqual(self.session.owned[MB]["requested_percent"], 40)

    def test_gpu_overtemperature_forces_full_speed_at_request(self):
        self.backend.temperatures[GPU] = 85
        result = self.request_set(GPU, 40)
        self.assertEqual(result["state"]["percent"], 100)
        self.assertTrue(self.session.owned[GPU]["thermal_override"])

    def test_manual_thermal_recovery_waits_ten_seconds_before_decrease(self):
        self.backend.temperatures["cpu"] = 91
        self.request_set(percent=40)
        self.backend.temperatures["cpu"] = 45
        self.now = 9.9
        self.session.tick()
        self.assertEqual(self.backend.mb.writes, [("set", MB, 100)])
        self.now = 10
        self.session.tick()
        self.assertEqual(self.backend.mb.writes[-1], ("set", MB, 40))

    def test_curve_decrease_is_delayed_and_increase_is_immediate(self):
        self.backend.temperatures["cpu"] = 80
        self.request_curve(points=[[30, 30], [80, 100]])
        self.backend.temperatures["cpu"] = 30
        self.now = 9
        self.session.tick()
        self.assertEqual(self.backend.mb.writes, [("set", MB, 100)])
        self.now = 10
        self.session.tick()
        self.assertEqual(self.backend.mb.writes[-1], ("set", MB, 30))
        self.backend.temperatures["cpu"] = 55
        self.now = 10.1
        self.session.tick()
        self.assertEqual(self.backend.mb.writes[-1], ("set", MB, 65))

    def test_changing_curve_retains_original_baseline(self):
        self.request_set(percent=60)
        self.request_curve()
        self.assertEqual(self.backend.mb.snapshots, [MB])
        self.session.restore(MB)
        self.assertEqual(self.backend.mb.states[MB]["percent"], 40)
        self.assertEqual(self.backend.mb.states[MB]["mode"], "auto")

    def test_restore_all_continues_after_one_device_fails(self):
        self.request_set(MB)
        self.request_set(GPU)
        self.backend.mb.fail_restore.add(MB)
        with self.assertRaisesRegex(RuntimeError, "일부 장치 복귀 실패"):
            self.session.restore_all()
        self.assertIn(MB, self.session.owned)
        self.assertIn(MB, self.session.baselines)
        self.assertNotIn(GPU, self.session.owned)
        self.assertNotIn(GPU, self.session.baselines)
        self.assertIn(("auto", GPU), self.backend.gpu.writes)

    def test_partial_set_failure_restores_baseline(self):
        self.backend.mb.fail_set.add(MB)
        with self.assertRaisesRegex(RuntimeError, "partial set"):
            self.request_set()
        self.assertEqual(self.backend.mb.writes, [("set", MB, 50), ("restore", MB)])
        self.assertEqual(self.session.owned, {})
        self.assertEqual(self.session.baselines, {})

    def test_failed_partial_set_restore_keeps_ownership_for_retry(self):
        self.backend.mb.fail_set.add(MB)
        self.backend.mb.fail_restore.add(MB)
        with self.assertRaisesRegex(RuntimeError, "partial set"):
            self.request_set()
        self.assertIn(MB, self.session.owned)
        self.assertIn(MB, self.session.baselines)
        self.assertTrue(self.session.events)
        self.backend.mb.fail_restore.clear()
        self.session.restore_all()
        self.assertEqual(self.session.owned, {})

    def test_runtime_restore_failure_is_retained_and_retried(self):
        self.request_set()
        self.backend.temperatures["cpu"] = None
        self.backend.mb.fail_restore.add(MB)
        self.session.tick()
        self.assertIn(MB, self.session.owned)
        self.backend.mb.fail_restore.clear()
        self.session.tick()
        self.assertEqual(self.session.owned, {})
        self.assertEqual(sum(call == ("restore", MB) for call in self.backend.mb.writes), 2)

    def test_gpu_sensor_case_curve_still_protects_hot_cpu(self):
        self.backend.temperatures[GPU] = 35
        self.backend.temperatures["cpu"] = 90
        result = self.request_curve(CASE, sensor=GPU)
        self.assertEqual(result["state"]["percent"], 100)
        self.assertTrue(self.session.owned[CASE]["thermal_override"])

    def test_gpu_sensor_case_curve_requires_cpu_sensor_before_takeover(self):
        self.backend.temperatures["cpu"] = None
        with self.assertRaises(ValueError):
            self.request_curve(CASE, sensor=GPU)
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.session.owned, {})

    def test_gpu_sensor_case_curve_cpu_hot_during_runtime_rises_immediately(self):
        self.backend.temperatures[GPU] = 30
        self.request_curve(CASE, sensor=GPU)
        self.backend.temperatures["cpu"] = 92
        self.now = 0.1
        self.session.tick()
        self.assertEqual(self.backend.mb.writes[-1], ("set", CASE, 100))

    def test_unavailable_device_does_not_take_ownership(self):
        self.backend.mb.states[MB]["controllable"] = False
        with self.assertRaises(RuntimeError):
            self.request_set()
        self.assertEqual(self.backend.writes, [])
        self.assertEqual(self.session.baselines, {})

    def test_curve_interpolation_and_endpoints(self):
        points = validate_curve([[30, 30], [70, 90], [90, 100]], 30)
        self.assertEqual([curve_percent(points, t) for t in (10, 30, 50, 70, 80, 110)],
                         [30, 30, 60, 90, 95, 100])


if __name__ == "__main__":
    unittest.main()
