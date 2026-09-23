"""Session ownership, software fan curves and recovery policy.

No settings are applied on startup. Only explicitly selected devices are owned.
"""
import math
import time


def valid_temperature(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 125


def validate_curve(points, minimum, maximum=100):
    if not isinstance(points, list) or not 2 <= len(points) <= 12:
        raise ValueError("곡선은 2–12개의 [온도, 속도] 지점이 필요합니다.")
    previous = (-1, -1)
    clean = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("각 지점은 [온도, 속도]여야 합니다.")
        temperature, percent = point
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 100:
            raise ValueError("곡선 온도 범위는 0–100°C입니다.")
        if type(percent) is not int or not minimum <= percent <= maximum:
            raise ValueError(f"곡선 속도 범위는 {minimum}–{maximum}%입니다.")
        if temperature <= previous[0] or percent < previous[1]:
            raise ValueError("온도는 증가하고, 팬 속도는 감소하지 않아야 합니다.")
        clean.append([temperature, percent])
        previous = (temperature, percent)
    return clean


def curve_percent(points, temperature):
    if temperature <= points[0][0]:
        return points[0][1]
    for (a, x), (b, y) in zip(points, points[1:]):
        if temperature <= b:
            return int(round(x + (y - x) * (temperature - a) / (b - a)))
    return points[-1][1]


class ControlSession:
    def __init__(self, backend, clock=time.monotonic):
        self.backend = backend
        self.clock = clock
        self.owned = {}
        self.baselines = {}
        self.events = []

    def _record(self, text):
        self.events.append(text)
        self.events = self.events[-20:]

    def _temperature(self, sensor):
        value = self.backend.temperature(sensor)
        if not valid_temperature(value):
            raise ValueError(f"{sensor}: 유효한 최신 온도를 읽을 수 없습니다.")
        return value

    @staticmethod
    def _critical(sensor):
        return 90 if sensor == "cpu" else 85

    def _guard_temperature(self, device, sensor):
        # A case-fan curve may follow GPU temperature, but still protects the CPU.
        main = "cpu" if device.startswith("mb:") else device
        temperature = self._temperature(sensor)
        own_temperature = temperature if sensor == main else self._temperature(main)
        return temperature, temperature >= self._critical(sensor) or own_temperature >= self._critical(main)

    def _own(self, device):
        driver = self.backend.device_backend(device)
        if device not in self.baselines:
            self.baselines[device] = driver.snapshot(device) if device.startswith("mb:") else None
        return driver

    def restore(self, device):
        driver = self.backend.device_backend(device)
        if device not in self.baselines:
            # Restoration is scoped to this session, even when another program
            # has left a device in manual mode.
            return driver.status(device)
        state = driver.restore(device, self.baselines[device]) if device.startswith("mb:") else driver.auto(device)
        self.owned.pop(device, None)
        self.baselines.pop(device, None)
        return state

    def restore_all(self):
        errors = []
        for device in list(self.baselines):
            try:
                self.restore(device)
            except Exception as exc:
                errors.append(f"{device}: {exc}")
        if errors:
            raise RuntimeError("일부 장치 복귀 실패: " + "; ".join(errors))

    def request(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
            raise ValueError("잘못된 요청입니다.")
        action, device = request["action"], request.get("device")
        if action in ("status", "heartbeat"):
            return self.status()
        if action == "restore_all":
            self.restore_all()
            return self.status()
        if action not in ("set", "curve", "restore"):
            raise ValueError("지원하지 않는 동작입니다.")
        driver = self.backend.device_backend(device)
        if action == "restore":
            return {"ok": True, "state": self.restore(device), "owned": self.owned.copy()}
        state = driver.status(device)
        if not state["controllable"]:
            raise RuntimeError(state.get("error", "제어할 수 없는 장치입니다."))
        sensor = request.get("sensor", "cpu" if device.startswith("mb:") else device)
        if not isinstance(sensor, str):
            raise ValueError("온도 센서가 필요합니다.")
        if action == "curve":
            points = validate_curve(request.get("points"), state["min_percent"], state["max_percent"])
        else:
            points = None
            driver.validate_speed(device, request.get("percent"))
        temperature, hot = self._guard_temperature(device, sensor)
        percent = curve_percent(points, temperature) if points else request["percent"]
        if hot:
            percent = state["max_percent"]
        driver.validate_speed(device, percent)
        self._own(device)
        self.owned[device] = {"mode": "curve" if points else "manual", "points": points,
                              "sensor": sensor, "percent": percent, "requested_percent": request.get("percent"),
                              "last_change": self.clock(), "thermal_override": hot}
        try:
            state = driver.set_speed(device, percent)
        except Exception:
            # Retain ownership if restoration fails, so close/tick can retry.
            try:
                self.restore(device)
            except Exception as restore:
                self._record(f"복귀 실패: {device}: {restore}")
            raise
        return {"ok": True, "state": state, "owned": self.owned.copy()}

    def tick(self):
        for device, control in list(self.owned.items()):
            try:
                temperature, hot = self._guard_temperature(device, control["sensor"])
                driver = self.backend.device_backend(device)
                state = driver.status(device)
                if not state["controllable"]:
                    raise RuntimeError(state.get("error", "장치가 응답하지 않습니다."))
                target = curve_percent(control["points"], temperature) if control["mode"] == "curve" else control["requested_percent"]
                if hot:
                    target = state["max_percent"]
                # Rise immediately; wait ten seconds between decreases to limit
                # audible oscillation from short CPU temperature spikes.
                changed = target != control["percent"]
                if changed and (target > control["percent"] or self.clock() - control["last_change"] >= 10):
                    driver.set_speed(device, target)
                    control.update(percent=target, last_change=self.clock())
                control["thermal_override"] = hot
            except Exception as exc:
                self._record(f"{device}: 보호 복귀 — {exc}")
                try:
                    self.restore(device)
                except Exception as restore:
                    self._record(f"{device}: 복귀 재시도 필요 — {restore}")

    def status(self):
        result = self.backend.snapshot()
        result.update(ok=True, states=result["devices"], owned=self.owned.copy(), events=list(self.events))
        return result
