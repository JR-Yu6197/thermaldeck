"""One authenticated control session with a heartbeat and exclusive lock."""
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

from .backend import Backend
from .control import ControlSession

HEARTBEAT_TIMEOUT = 12
MAX_REQUEST = 16384


def run_worker():
    if os.geteuid() != 0:
        raise RuntimeError("관리자 인증이 필요합니다.")
    folder = Path("/run/thermaldeck")
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink() or folder.stat().st_uid != 0 or folder.stat().st_mode & 0o022:
        raise RuntimeError("제어 잠금 디렉터리 권한이 안전하지 않습니다.")
    fd = os.open(folder / "session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError("다른 ThermalDeck 제어 세션이 실행 중입니다.")
    backend = None
    session = None
    cleanup_errors = []

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGHUP, terminate)
    try:
        backend = Backend()
        session = ControlSession(backend)
        print(json.dumps({"ready": True}), flush=True)
        last_seen = time.monotonic()
        next_tick = last_seen + 2
        buffer = b""
        while True:
            now = time.monotonic()
            if now - last_seen > HEARTBEAT_TIMEOUT:
                break
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], max(0, min(1, next_tick - now)))
            if readable:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_REQUEST:
                    raise ValueError("요청 크기를 초과했습니다.")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    last_seen = time.monotonic()
                    try:
                        request = json.loads(line)
                        reply = session.request(request)
                    except Exception as exc:
                        reply = {"ok": False, "error": str(exc)}
                    print(json.dumps(reply, ensure_ascii=False), flush=True)
            if time.monotonic() >= next_tick:
                session.tick()
                next_tick = time.monotonic() + 2
    finally:
        if session:
            for attempt in range(3):
                try:
                    session.restore_all()
                    cleanup_errors.clear()
                    break
                except Exception as exc:
                    cleanup_errors = [str(exc)]
                    if attempt < 2:
                        time.sleep(0.2)
        try:
            if backend:
                backend.close()
        except Exception as exc:
            cleanup_errors.append(f"센서 연결 종료 실패: {exc}")
        finally:
            os.close(fd)
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
