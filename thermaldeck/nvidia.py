"""NVIDIA fan control through the driver-provided NVML library.

Devices are discovered at runtime and addressed by UUID, never by UI order.
"""
import ctypes as C
import math


class NvidiaError(RuntimeError):
    pass


class Nvml:
    def __init__(self):
        self.lib = C.CDLL("libnvidia-ml.so.1")
        self.lib.nvmlErrorString.argtypes = [C.c_int]
        self.lib.nvmlErrorString.restype = C.c_char_p
        signatures = {
            "nvmlInit_v2": [], "nvmlShutdown": [],
            "nvmlDeviceGetCount_v2": [C.POINTER(C.c_uint)],
            "nvmlDeviceGetHandleByIndex_v2": [C.c_uint, C.POINTER(C.c_void_p)],
            "nvmlDeviceGetHandleByUUID": [C.c_char_p, C.POINTER(C.c_void_p)],
            "nvmlDeviceGetUUID": [C.c_void_p, C.c_void_p, C.c_uint],
            "nvmlDeviceGetName": [C.c_void_p, C.c_void_p, C.c_uint],
            "nvmlDeviceGetNumFans": [C.c_void_p, C.POINTER(C.c_uint)],
            "nvmlDeviceGetMinMaxFanSpeed": [C.c_void_p, C.POINTER(C.c_uint), C.POINTER(C.c_uint)],
            "nvmlDeviceGetTemperature": [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)],
            "nvmlDeviceGetFanControlPolicy_v2": [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)],
            "nvmlDeviceGetFanSpeed_v2": [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)],
            "nvmlDeviceGetTargetFanSpeed": [C.c_void_p, C.c_uint, C.POINTER(C.c_uint)],
            "nvmlDeviceSetFanSpeed_v2": [C.c_void_p, C.c_uint, C.c_uint],
            "nvmlDeviceSetDefaultFanSpeed_v2": [C.c_void_p, C.c_uint],
        }
        self.available = set()
        for name, args in signatures.items():
            fn = getattr(self.lib, name, None)
            if fn is not None:
                fn.argtypes, fn.restype = args, C.c_int
                self.available.add(name)
        self.call("nvmlInit_v2")

    def call(self, name, *args):
        if name not in self.available:
            raise NvidiaError(f"NVIDIA 드라이버가 {name}을 지원하지 않습니다.")
        code = getattr(self.lib, name)(*args)
        if code:
            detail = self.lib.nvmlErrorString(code).decode("utf-8", errors="replace")
            raise NvidiaError(f"{name}: {detail} (NVML {code})")

    def uint(self, name, *args):
        value = C.c_uint()
        self.call(name, *args, C.byref(value))
        return value.value

    def string(self, name, handle):
        buf = C.create_string_buffer(256)
        self.call(name, handle, buf, len(buf))
        return buf.value.decode("utf-8", errors="replace")

    def handle(self, uuid):
        handle = C.c_void_p()
        self.call("nvmlDeviceGetHandleByUUID", uuid.encode("ascii"), C.byref(handle))
        return handle

    def devices(self):
        result = []
        for index in range(self.uint("nvmlDeviceGetCount_v2")):
            handle = C.c_void_p()
            self.call("nvmlDeviceGetHandleByIndex_v2", index, C.byref(handle))
            result.append((self.string("nvmlDeviceGetUUID", handle), self.string("nvmlDeviceGetName", handle)))
        return result

    def limits(self, handle):
        low, high = C.c_uint(), C.c_uint()
        self.call("nvmlDeviceGetMinMaxFanSpeed", handle, C.byref(low), C.byref(high))
        # The application deliberately does not offer manual fan-stop.
        low, high = max(30, low.value), min(100, high.value)
        if low > high:
            raise NvidiaError("드라이버가 유효한 팬 속도 범위를 제공하지 않습니다.")
        return low, high

    def close(self):
        self.call("nvmlShutdown")


class NvidiaBackend:
    def __init__(self, nvml=None):
        self.n = nvml or Nvml()
        self.devices = {"gpu:" + uuid: {"uuid": uuid, "name": name} for uuid, name in self.n.devices()}

    def _device(self, device):
        if not isinstance(device, str) or device not in self.devices:
            raise NvidiaError("인식되지 않은 GPU입니다.")
        entry = self.devices[device]
        handle = self.n.handle(entry["uuid"])
        if self.n.string("nvmlDeviceGetName", handle) != entry["name"]:
            raise NvidiaError("GPU 식별 정보가 변경되었습니다.")
        return handle, entry

    def temperature(self, device):
        handle, _ = self._device(device)
        value = self.n.uint("nvmlDeviceGetTemperature", handle, 0)
        if not math.isfinite(value) or not 0 <= value <= 125:
            raise NvidiaError("GPU 온도 값이 유효하지 않습니다.")
        return value

    def status(self, device):
        handle, entry = self._device(device)
        state = {"id": device, "kind": "gpu", "name": entry["name"], "pump": False,
                 "sensor": device, "temperature": None, "rpm": None, "percent": None,
                 "mode": "unavailable", "controllable": False, "min_percent": 30, "max_percent": 100}
        try:
            state["temperature"] = self.temperature(device)
            count = self.n.uint("nvmlDeviceGetNumFans", handle)
            if not 1 <= count <= 16:
                raise NvidiaError("GPU가 제어 가능한 팬 채널을 제공하지 않습니다.")
            low, high = self.n.limits(handle)
            fans = [{"index": index,
                     "policy": self.n.uint("nvmlDeviceGetFanControlPolicy_v2", handle, index),
                     "percent": self.n.uint("nvmlDeviceGetFanSpeed_v2", handle, index),
                     "target": self.n.uint("nvmlDeviceGetTargetFanSpeed", handle, index)} for index in range(count)]
            automatic = all(fan["policy"] == 0 for fan in fans)
            state.update(fans=fans, percent=max(fan["percent"] for fan in fans),
                         mode="auto" if automatic else "manual", controllable=True,
                         min_percent=low, max_percent=high)
            state["target_percent"] = fans[0]["target"] if all(fan["target"] == fans[0]["target"] for fan in fans) else None
        except (OSError, NvidiaError) as exc:
            state["error"] = str(exc)
        return state

    def discover(self):
        return [self.status(device) for device in self.devices]

    def validate_speed(self, device, percent):
        if type(percent) is not int:
            raise NvidiaError("속도는 정수 퍼센트여야 합니다.")
        state = self.status(device)
        if not state["controllable"]:
            raise NvidiaError(state.get("error", "제어할 수 없는 GPU입니다."))
        if not state["min_percent"] <= percent <= state["max_percent"]:
            raise NvidiaError(f"허용 범위: {state['min_percent']}–{state['max_percent']}%")
        return percent

    def auto(self, device):
        handle, _ = self._device(device)
        count = self.n.uint("nvmlDeviceGetNumFans", handle)
        if not 1 <= count <= 16:
            raise NvidiaError("유효하지 않은 GPU 팬 채널 수입니다.")
        errors = []
        for index in range(count):
            try:
                self.n.call("nvmlDeviceSetDefaultFanSpeed_v2", handle, index)
            except Exception as exc:
                errors.append(str(exc))
        state = self.status(device)
        if errors or not state["controllable"] or state["mode"] != "auto":
            raise NvidiaError("GPU 자동 복귀 확인 실패: " + "; ".join(errors or [str(state)]))
        return state

    def set_speed(self, device, percent):
        self.validate_speed(device, percent)
        handle, _ = self._device(device)
        count = self.n.uint("nvmlDeviceGetNumFans", handle)
        try:
            for index in range(count):
                self.n.call("nvmlDeviceSetFanSpeed_v2", handle, index, percent)
            state = self.status(device)
            if not state["controllable"] or not all(f["policy"] == 1 and f["target"] == percent for f in state["fans"]):
                raise NvidiaError("모든 GPU 팬 채널에서 설정값을 확인하지 못했습니다.")
            return state
        except Exception as exc:
            try:
                self.auto(device)
            except Exception as restore:
                raise NvidiaError(f"설정 실패: {exc}; 자동 복귀 실패: {restore}") from exc
            raise NvidiaError(f"설정 실패 후 GPU 자동 제어로 복귀했습니다: {exc}") from exc

    def close(self):
        self.n.close()
