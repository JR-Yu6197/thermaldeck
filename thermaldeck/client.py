"""Unprivileged GUI client. Polkit owns authentication; no passwords are stored."""
import json
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time

INSTALLED_LAUNCHER = Path("/opt/thermaldeck/launcher.py")


class WorkerClient:
    def __init__(self):
        self.process = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.errors = None
        self.last_error = None
        self.last_status = {}

    def _read_reply(self, timeout):
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            if not select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            char = self.process.stdout.read(1)
            if not char:
                self.errors.seek(0)
                details = self.errors.read().decode("utf-8", errors="replace")[-3000:]
                raise RuntimeError("관리자 제어 연결이 종료되었습니다. " + details)
            data += char
            if char == b"\n":
                return json.loads(data)
            if len(data) > 262144:
                raise RuntimeError("제어 응답 크기를 초과했습니다.")
        raise RuntimeError("관리자 인증 또는 제어 응답 시간이 초과되었습니다.")

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if self.process is not None:
                raise RuntimeError("이전 제어 세션이 종료되었습니다. 앱을 다시 실행해 주세요.")
            if not INSTALLED_LAUNCHER.is_file():
                raise RuntimeError("제어 구성요소 설치가 필요합니다: sudo ./scripts/install.sh")
            for path in [INSTALLED_LAUNCHER, INSTALLED_LAUNCHER.parent]:
                if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                    raise RuntimeError("설치된 제어 구성요소의 소유권/권한을 확인하세요.")
            self.errors = tempfile.TemporaryFile()
            self.process = subprocess.Popen(
                ["/usr/bin/pkexec", "/usr/bin/python3", "-I", str(INSTALLED_LAUNCHER), "worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, bufsize=0)
            try:
                if not self._read_reply(180).get("ready"):
                    raise RuntimeError("관리자 제어 연결을 시작하지 못했습니다.")
            except Exception:
                if not self.process.stdin.closed:
                    self.process.stdin.close()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    # No ready handshake means no control request was sent.
                    # A cancelled authentication can safely be retried.
                    self.errors.close()
                    self.errors = None
                    self.process = None
                raise
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()

    def _exchange(self, request):
        if self.last_error:
            raise RuntimeError(self.last_error)
        try:
            self.process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            self.process.stdin.flush()
            reply = self._read_reply(10)
            if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                raise RuntimeError("잘못된 제어 응답입니다.")
        except Exception as exc:
            # A late reply must never become the next request's response.
            # EOF asks the worker to restore its owned devices immediately.
            self.last_error = str(exc)
            if not self.process.stdin.closed:
                self.process.stdin.close()
            raise
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "제어 요청 실패"))
        self.last_status = reply
        return reply

    def request(self, request):
        with self.lock:
            if self.stop.is_set():
                raise RuntimeError("제어 세션을 닫는 중입니다.")
            self.start()
            if self.last_error:
                raise RuntimeError(self.last_error)
            return self._exchange(request)

    def _heartbeat(self):
        while not self.stop.wait(2):
            try:
                with self.lock:
                    if self.stop.is_set():
                        return
                    self._exchange({"action": "heartbeat"})
            except Exception as exc:
                self.last_error = str(exc)
                # Closing the pipe triggers recovery even if UI error handling fails.
                with self.lock:
                    if self.process and not self.process.stdin.closed:
                        self.process.stdin.close()
                return

    def close(self):
        self.stop.set()
        with self.lock:
            if self.process is None:
                return
            if not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("제어 프로세스 종료를 확인하지 못했습니다. 팬 상태를 확인하세요.") from exc
            self.errors.seek(0)
            details = self.errors.read().decode("utf-8", errors="replace")[-3000:]
            code = self.process.returncode
            self.errors.close()
            self.process = None
            if code:
                raise RuntimeError("제어 세션 종료/복귀 확인 실패: " + details)
