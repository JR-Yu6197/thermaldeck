import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="ThermalDeck — GPU · CPU · 케이스 팬 · 펌프")
    parser.add_argument("action", nargs="?", default="gui", choices=["gui", "status", "worker"])
    parser.add_argument("--json", action="store_true", help="장치 상태를 JSON으로 표시")
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action == "worker":
        from .worker import run_worker
        run_worker()
    elif args.action == "status":
        from .backend import Backend
        backend = Backend()
        try:
            state = backend.snapshot()
            if args.json:
                print(json.dumps(state, indent=2, ensure_ascii=False))
            else:
                print(f"ThermalDeck | {state['board']} | CPU {state['cpu_temperature']}°C")
                for device in state["devices"]:
                    print(f"{device['name']}: {device.get('temperature', '—')}°C · {device['percent']}% · {device['rpm']} RPM · {device['mode']}")
                for error in state["errors"]:
                    print(error)
        finally:
            backend.close()
    else:
        if os.geteuid() == 0:
            raise RuntimeError("GUI는 일반 사용자로 실행해 주세요.")
        from .gui import run_gui
        run_gui(screenshot_path=args.screenshot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
