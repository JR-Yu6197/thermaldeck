"""Read-only inventory and constrained dispatch for supported fan devices."""
from pathlib import Path

from .hwmon import HwmonBackend
from .nvidia import NvidiaBackend


class Backend:
    def __init__(self, motherboard=None, nvidia=None):
        self.mb = motherboard or HwmonBackend()
        self.errors = []
        try:
            self.gpu = nvidia or NvidiaBackend()
        except Exception as exc:
            self.gpu = None
            self.errors.append(f"NVIDIA: {exc}")

    def snapshot(self):
        errors = list(self.errors)
        devices = self.mb.discover()
        if self.gpu:
            devices = self.gpu.discover() + devices
        try:
            board = Path("/sys/class/dmi/id/board_name").read_text().strip()
        except OSError:
            board = "Unknown motherboard"
        if not any(d["kind"] == "motherboard" and d["controllable"] for d in devices):
            errors.append("메인보드 PWM 장치가 없습니다. it87 설치 및 로드 상태를 확인하세요.")
        return {"board": board, "cpu_temperature": self.mb.cpu_temperature(), "devices": devices, "errors": errors}

    def device_backend(self, device):
        if not isinstance(device, str):
            raise ValueError("장치 ID가 필요합니다.")
        if device.startswith("gpu:") and self.gpu:
            return self.gpu
        if device.startswith("mb:"):
            return self.mb
        raise ValueError("인식되지 않은 장치입니다.")

    def temperature(self, sensor):
        if sensor == "cpu":
            return self.mb.cpu_temperature()
        if self.gpu and sensor in self.gpu.devices:
            return self.gpu.temperature(sensor)
        raise ValueError("인식되지 않은 온도 센서입니다.")

    def close(self):
        if self.gpu:
            self.gpu.close()
