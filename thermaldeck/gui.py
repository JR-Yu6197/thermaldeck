"""ThermalDeck's GTK dashboard: read first, change cooling only on request.

Sensor reads and privileged commands run off the GTK thread. The privileged
client is deliberately created only after an explicit control action.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import threading
import time
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from .backend import Backend
from .client import WorkerClient


CSS = b"""
window { background: #10151e; color: #eef3fa; }
* { font-family: "Noto Sans CJK KR", "Noto Sans", sans-serif; }
label { color: #e3eaf3; }
.app-title { font-size: 25px; font-weight: 800; color: #f2f7ff; }
.subtitle, .muted { color: #93a4ba; font-size: 11px; }
.section-title { font-size: 15px; font-weight: 700; color: #edf4ff; }
.card { background: #1b2431; border: 1px solid #2b394c; border-radius: 13px; }
.card-title { font-size: 13px; font-weight: 700; }
.card-subtitle { font-size: 10px; color: #96a8bf; }
.metric-value { font-size: 27px; font-weight: 700; color: #ecf4ff; }
.metric-caption { font-size: 10px; color: #9cadc2; }
.small-value { font-size: 21px; font-weight: 700; color: #eef5ff; }
.unsupported-value { font-size: 15px; color: #96a8bf; padding-top: 9px; padding-bottom: 9px; }
.summary { background: #182431; border: 1px solid #284158; border-radius: 12px; }
.cpu-value { font-size: 32px; font-weight: 800; color: #66dcc6; }
.mode-tag { color: #79d9c6; font-size: 10px; }
.pump-tag { color: #fac886; font-size: 10px; }
.status { color: #86d8c6; font-size: 11px; }
.notice { background: #1c2c3d; border: 1px solid #2d4761; border-radius: 8px; }
.notice label { color: #b6cde4; font-size: 11px; }
.error { background: #3a2528; border-color: #7f424c; }
.error label { color: #ffd4d8; }
button { background: #27364a; color: #e6effc; border: 1px solid #3a4c65;
         border-radius: 7px; padding: 6px 11px; box-shadow: none; text-shadow: none; font-size: 11px; }
button:hover { background: #354a65; }
button:active { background: #425e7c; }
button:disabled { color: #718097; background: #202b3a; border-color: #2d394b; }
button.primary { background: #226f77; border-color: #389698; color: #f4ffff; }
button.primary:hover { background: #2e8790; }
button.primary:disabled { background: #254047; color: #748c90; border-color: #2c5057; }
button.flat-action { background: transparent; color: #bccce0; }
entry { background: #111a27; color: #dce7f6; border: 1px solid #344760;
        border-radius: 6px; padding: 6px 8px; font-size: 10px; }
entry:focus { border-color: #54b9b0; }
combobox button { padding: 5px 8px; }
scale trough { min-height: 5px; background: #0f1825; border-radius: 8px; }
scale highlight { background: #50bbaa; border-radius: 8px; }
scale slider { background: #a8eee2; border: 0; min-width: 14px; min-height: 14px; }
separator { background: #2c3c50; min-height: 1px; }
scrolledwindow { border: none; }
scrollbar { background: #10151e; }
scrollbar slider { background: #3a4b61; border-radius: 9px; min-width: 6px; }
tooltip { background: #24364b; color: white; }
"""

CURVES = {
    "quiet": ("저소음", [(40, 25), (60, 40), (75, 65), (85, 100)]),
    "balanced": ("균형", [(40, 30), (60, 50), (75, 75), (85, 100)]),
    "cool": ("냉각 우선", [(40, 45), (60, 65), (75, 85), (85, 100)]),
}


def styled(widget: Gtk.Widget, *names: str) -> Gtk.Widget:
    for name in names:
        widget.get_style_context().add_class(name)
    return widget


def label(text: str = "", style: str | None = None, xalign: float = 0) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    if style:
        styled(widget, style)
    return widget


def row(spacing: int = 8) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)


def column(spacing: int = 8) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def padded(widget: Gtk.Widget, amount: int) -> Gtk.Widget:
    widget.set_margin_start(amount)
    widget.set_margin_end(amount)
    widget.set_margin_top(amount)
    widget.set_margin_bottom(amount)
    return widget


def numeric(value: Any, suffix: str, digits: int = 0) -> str:
    try:
        val = float(value)
        return f"{val:.{digits}f}{suffix}" if math.isfinite(val) else "—"
    except (TypeError, ValueError):
        return "—"


def parse_curve(text: str, minimum: int, maximum: int) -> list[list[int]]:
    """Validate the visible temperature:PWM editor before sending a command."""
    points = []
    for item in re.split(r"[,;，]+", text.strip()):
        match = re.fullmatch(r"\s*(\d{1,3})\s*[:：]\s*(\d{1,3})\s*%?\s*", item)
        if not match:
            raise ValueError("곡선을 40:30, 60:50, 75:75, 85:100 형식으로 입력하세요.")
        temp, speed = map(int, match.groups())
        if not 0 <= temp <= 100 or not 0 <= speed <= 100:
            raise ValueError("곡선 온도는 0~100°C, 속도는 0~100% 범위여야 합니다.")
        if points and temp <= points[-1][0]:
            raise ValueError("곡선 온도는 낮은 값부터 중복 없이 입력하세요.")
        speed = min(maximum, max(minimum, speed))
        if points and speed < points[-1][1]:
            raise ValueError("온도가 올라갈수록 팬 출력이 유지되거나 증가해야 합니다.")
        points.append([temp, speed])
    if not 2 <= len(points) <= 12:
        raise ValueError("곡선 지점은 2~12개를 입력하세요.")
    return points


class DeviceCard(Gtk.Box):
    """A stable card preserves a user's pending slider edits during polling."""

    def __init__(self, app: "Dashboard", device: dict[str, Any]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.app = app
        self.device = device
        self.device_id = device["id"]
        self._updating = False
        self._slider_dirty = False
        self._initialized = False
        self._sensor_options: tuple[tuple[str, str], ...] = ()
        self._controls: list[Gtk.Widget] = []
        styled(self, "card")
        body = padded(column(10), 15)
        self.pack_start(body, True, True, 0)

        heading = row()
        names = column(3)
        self.title = label(device.get("name", self.device_id), "card-title")
        self.title.set_ellipsize(3)  # Pango.EllipsizeMode.END, without an extra import.
        self.title.set_max_width_chars(43)
        self.title.set_tooltip_text(device.get("name", self.device_id))
        self.subtitle = label("", "card-subtitle")
        names.pack_start(self.title, False, False, 0)
        names.pack_start(self.subtitle, False, False, 0)
        heading.pack_start(names, True, True, 0)
        self.mode = label("시스템 제어", "mode-tag", 1)
        heading.pack_end(self.mode, False, False, 0)
        body.pack_start(heading, False, False, 0)

        metrics = Gtk.Grid(column_spacing=18, column_homogeneous=True)
        self.metrics: dict[str, Gtk.Label] = {}
        self.metric_captions: dict[str, Gtk.Label] = {}
        for index, (key, caption) in enumerate((('temperature', '온도'), ('rpm', '팬 회전수'), ('percent', '출력'))):
            box = column(0)
            value = label("—", "metric-value" if device.get('kind') == 'gpu' else "small-value")
            box.pack_start(value, False, False, 0)
            caption_label = label(caption, "metric-caption")
            box.pack_start(caption_label, False, False, 0)
            self.metrics[key] = value
            self.metric_captions[key] = caption_label
            metrics.attach(box, index, 0, 1, 1)
        body.pack_start(metrics, False, False, 0)

        slider_row = row(8)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.connect("value-changed", self._slider_changed)
        self.slider_label = label("—", "muted", 1)
        self.slider_label.set_width_chars(5)
        slider_row.pack_start(self.scale, True, True, 0)
        slider_row.pack_start(self.slider_label, False, False, 0)
        apply_button = self._button("적용", self._apply_speed, primary=True)
        slider_row.pack_start(apply_button, False, False, 0)
        body.pack_start(slider_row, False, False, 0)
        self._controls.append(self.scale)

        actions = row(6)
        self.minimum_label = label("", "muted")
        actions.pack_start(self.minimum_label, True, True, 0)
        actions.pack_start(self._button("100%", lambda *_: self.app.command({'action': 'set', 'device': self.device_id, 'percent': 100}, "최대 냉각 적용")), False, False, 0)
        restore_name = "자동 제어" if device.get('kind') == 'gpu' else "시작 상태 복귀"
        self.restore_button = self._button(restore_name, lambda *_: self.app.command({'action': 'restore', 'device': self.device_id}, "제어 복귀"))
        self.restore_button.set_tooltip_text("이 앱이 변경한 장치만 복귀합니다.")
        actions.pack_start(self.restore_button, False, False, 0)
        body.pack_start(actions, False, False, 0)
        body.pack_start(Gtk.Separator(), False, False, 0)

        curve_head = row(6)
        curve_head.pack_start(label("온도 곡선", "muted"), False, False, 0)
        self.preset = Gtk.ComboBoxText()
        for key, (title, _) in CURVES.items():
            self.preset.append(key, title)
        self.preset.set_active_id("balanced")
        curve_head.pack_start(self.preset, False, False, 0)
        self.sensor = Gtk.ComboBoxText()
        if device.get('kind') == 'gpu':
            self.sensor.append(self.device_id, "GPU 온도")
        self.sensor.append("cpu", "CPU 온도")
        self.sensor.set_active(0)
        curve_head.pack_end(self.sensor, False, False, 0)
        body.pack_start(curve_head, False, False, 0)
        curve_row = row(6)
        self.curve_entry = Gtk.Entry()
        self.curve_entry.set_text("40:30, 60:50, 75:75, 85:100")
        self.curve_entry.set_tooltip_text("온도(°C):출력(%) · 쉼표로 구분 · 최소 출력 아래의 값은 자동으로 올립니다.")
        self.curve_entry.set_hexpand(True)
        curve_row.pack_start(self.curve_entry, True, True, 0)
        curve_row.pack_start(self._button("곡선 적용", self._apply_curve), False, False, 0)
        body.pack_start(curve_row, False, False, 0)
        self._controls.extend([self.preset, self.sensor, self.curve_entry])
        self.preset.connect("changed", self._preset_changed)
        self.update(device)

    def _button(self, text: str, callback: Any, primary: bool = False) -> Gtk.Button:
        button = Gtk.Button(label=text)
        if primary:
            styled(button, "primary")
        button.connect("clicked", callback)
        self._controls.append(button)
        return button

    def _slider_changed(self, *_: Any) -> None:
        self.slider_label.set_text(f"{self.scale.get_value():.0f}%")
        if not self._updating:
            self._slider_dirty = True

    def _preset_changed(self, *_: Any) -> None:
        points = CURVES[self.preset.get_active_id()][1]
        minimum = int(self.device.get('min_percent', 0))
        self.curve_entry.set_text(', '.join(f'{temp}:{max(minimum, speed)}' for temp, speed in points))

    def _apply_speed(self, *_: Any) -> None:
        self.app.command({'action': 'set', 'device': self.device_id, 'percent': int(self.scale.get_value())}, "수동 출력 적용")

    def _apply_curve(self, *_: Any) -> None:
        try:
            points = parse_curve(self.curve_entry.get_text(), int(self.device.get('min_percent', 0)), int(self.device.get('max_percent', 100)))
        except ValueError as exc:
            self.app.notice(str(exc), error=True)
            return
        self.curve_entry.set_text(', '.join(f'{temp}:{speed}' for temp, speed in points))
        self.app.command({'action': 'curve', 'device': self.device_id, 'points': points, 'sensor': self.sensor.get_active_id()}, "온도 곡선 적용")

    def update_sensor_options(self, devices: list[dict[str, Any]]) -> None:
        """Offer discovered GPU temperatures without replacing an in-progress choice."""
        gpus = [device for device in devices if device.get('kind') == 'gpu']
        options = [('cpu', 'CPU 온도')]
        options += [(device['id'], f'GPU {index + 1} 온도') for index, device in enumerate(gpus)]
        if self.device.get('kind') == 'gpu':
            options.sort(key=lambda option: option[0] != self.device_id)
        option_key = tuple(options)
        if option_key == self._sensor_options:
            return
        selected = self.sensor.get_active_id()
        self.sensor.remove_all()
        for identifier, caption in options:
            self.sensor.append(identifier, caption)
        self.sensor.set_active_id(selected if selected in dict(options) else options[0][0])
        self._sensor_options = option_key

    def _temperature_metric(self) -> None:
        value = self.device.get('temperature')
        caption = '온도'
        if self.device.get('kind') != 'gpu' and value is None:
            # Fan headers have no local temperature sensor. Show the actual
            # software control's reference, defaulting to the CPU when unowned.
            sensor = self.app.owned.get(self.device_id, {}).get('sensor', 'cpu')
            snapshot = self.app._last_snapshot
            if sensor == 'cpu':
                value = snapshot.get('cpu_temperature')
                caption = '제어 기준 CPU'
            else:
                gpus = [device for device in snapshot.get('devices', []) if device.get('kind') == 'gpu']
                for index, gpu in enumerate(gpus):
                    if gpu['id'] == sensor:
                        value = gpu.get('temperature')
                        caption = f'제어 기준 GPU {index + 1}'
                        break
                else:
                    caption = '제어 기준 센서 없음'
        self.metrics['temperature'].set_text(numeric(value, '°C', 1))
        self.metric_captions['temperature'].set_text(caption)

    def update(self, device: dict[str, Any]) -> None:
        self.device = device
        is_gpu = device.get('kind') == 'gpu'
        subtitle = device.get('chip', 'NVIDIA GPU') if is_gpu else device.get('chip', '메인보드 팬 단자')
        if device.get('pump'):
            subtitle += " · 펌프 보호 최소 출력 적용"
        if not device.get('controllable'):
            subtitle += " · 읽기 전용"
        self.subtitle.set_text(subtitle)
        self._temperature_metric()
        missing_gpu_rpm = is_gpu and device.get('rpm') is None
        self.metrics['rpm'].set_text('미지원' if missing_gpu_rpm else numeric(device.get('rpm'), ' RPM'))
        self.metric_captions['rpm'].set_text('드라이버 RPM 미제공' if missing_gpu_rpm else ('GPU 보고 RPM (최대)' if is_gpu else '팬 회전수'))
        rpm_style = self.metrics['rpm'].get_style_context()
        if missing_gpu_rpm:
            rpm_style.add_class('unsupported-value')
        else:
            rpm_style.remove_class('unsupported-value')
        self.metrics['percent'].set_text(numeric(device.get('percent'), '%'))
        minimum, maximum = int(device.get('min_percent', 0)), int(device.get('max_percent', 100))
        self.minimum_label.set_text(f"최소 {minimum}%" + (" · 펌프" if device.get('pump') else ""))
        self._updating = True
        self.scale.set_range(minimum, max(minimum + 1, maximum))
        if not self._slider_dirty:
            current = device.get('percent')
            self.scale.set_value(max(minimum, int(current) if current is not None else 50))
        self._updating = False
        self.set_busy(self.app.busy)
        ownership = self.app.owned.get(self.device_id, {})
        mode = ownership.get('mode')
        hardware_mode = {
            'manual': '수동 출력', 'full': '최대 출력', 'full_speed': '최대 출력',
            'hardware': '하드웨어 제어', 'auto': '자동 제어', 'unavailable': '상태 확인 불가',
        }.get(device.get('mode'), str(device.get('mode') or '상태 확인 불가'))
        text = {'manual': '● 앱 수동 제어', 'curve': '● 앱 곡선 제어'}.get(mode, hardware_mode)
        if ownership.get('thermal_override'):
            text = '● 온도 보호 100%'
        if self.app._worker_error and ownership:
            text = '제어 연결 확인 필요'
        self.mode.set_text(text)
        self.mode.set_tooltip_text(f'장치 보고 상태: {hardware_mode}')
        # Restoring another program's setting is outside this application's scope.
        self.restore_button.set_sensitive(bool(ownership) and not self.app.busy and not self.app.screenshot_path and not self.app._worker_error)

    def set_busy(self, busy: bool) -> None:
        enabled = bool(self.device.get('controllable')) and not busy and not self.app.screenshot_path and not self.app._worker_error
        for widget in self._controls:
            widget.set_sensitive(enabled)
        self.restore_button.set_sensitive(enabled and self.device_id in self.app.owned)


class Dashboard(Gtk.Window):
    def __init__(self, screenshot_path: str | None = None):
        super().__init__(title="ThermalDeck · 냉각 대시보드")
        self.set_default_size(1100, 800)
        self.set_size_request(970, 650)
        self.screenshot_path = screenshot_path
        self.busy = False
        self.closing = False
        self._destroyed = False
        self._stop = threading.Event()
        self._client: WorkerClient | None = None
        self._last_snapshot: dict[str, Any] = {}
        self._has_snapshot = False
        self._last_poll_error: str | None = None
        self._worker_error: str | None = None
        self._last_worker_events: tuple[str, ...] = ()
        self.owned: dict[str, Any] = {}
        self.cards: dict[str, DeviceCard] = {}
        self._layout_ids: tuple[str, ...] = ()
        self._screenshot_start = time.monotonic()
        icon_path = Path(__file__).resolve().parent.parent / 'assets' / 'icon.svg'
        if icon_path.is_file():
            try:
                self.set_icon_from_file(str(icon_path))
            except GLib.Error:
                pass
        self.connect('delete-event', self._on_close)
        self.connect('destroy', self._on_destroy)
        self._build()
        self.show_all()
        threading.Thread(target=self._poll, name='thermaldeck-sensors', daemon=True).start()
        if screenshot_path:
            GLib.timeout_add(2400, self._save_screenshot)

    def _build(self) -> None:
        outer = padded(column(14), 22)
        self.add(outer)
        header = row(14)
        brand = column(2)
        brand.pack_start(label('ThermalDeck', 'app-title'), False, False, 0)
        brand.pack_start(label('CPU · GPU · 메인보드 팬을 한 화면에서', 'subtitle'), False, False, 0)
        header.pack_start(brand, True, True, 0)
        self.status_label = label('센서 연결 중…', 'status')
        header.pack_start(self.status_label, False, False, 0)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner, False, False, 0)
        self.restore_all = Gtk.Button(label='내 제어 모두 복귀')
        self.restore_all.set_sensitive(False)
        self.restore_all.connect('clicked', lambda *_: self.command({'action': 'restore_all'}, '모든 앱 제어 복귀'))
        header.pack_start(self.restore_all, False, False, 0)
        outer.pack_start(header, False, False, 0)

        self.notice_box = styled(Gtk.EventBox(), 'notice')
        self.notice_label = label('센서를 읽는 중입니다. 적용을 누르기 전에는 냉각 설정을 변경하지 않습니다.')
        self.notice_label.set_line_wrap(True)
        padded(self.notice_label, 10)
        self.notice_box.add(self.notice_label)
        outer.pack_start(self.notice_box, False, False, 0)

        summary = styled(Gtk.EventBox(), 'summary')
        metrics = padded(row(30), 14)
        cpu = column(0)
        cpu.pack_start(label('CPU 온도', 'metric-caption'), False, False, 0)
        self.cpu_label = label('—', 'cpu-value')
        cpu.pack_start(self.cpu_label, False, False, 0)
        metrics.pack_start(cpu, False, False, 0)
        board = column(5)
        board.pack_start(label('시스템', 'metric-caption'), False, False, 0)
        self.board_label = label('센서 검색 중', 'card-title')
        self.board_label.set_ellipsize(3)
        board.pack_start(self.board_label, False, False, 0)
        metrics.pack_start(board, True, True, 0)
        counts = column(5)
        counts.pack_start(label('앱 제어 / 감지 장치', 'metric-caption'), False, False, 0)
        self.count_label = label('0 / 0', 'card-title')
        counts.pack_start(self.count_label, False, False, 0)
        metrics.pack_end(counts, False, False, 0)
        summary.add(metrics)
        outer.pack_start(summary, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.content = column(13)
        self.content.set_margin_end(8)
        self.content.pack_start(label('그래픽카드', 'section-title'), False, False, 0)
        self.gpu_grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        self.content.pack_start(self.gpu_grid, False, False, 0)
        self.gpu_empty = label('NVIDIA GPU 센서를 검색하고 있습니다.', 'muted')
        self.gpu_empty.set_line_wrap(True)
        self.content.pack_start(self.gpu_empty, False, False, 0)
        board_heading = row()
        board_heading.set_margin_top(5)
        board_heading.pack_start(label('메인보드 팬 · 펌프', 'section-title'), True, True, 0)
        board_heading.pack_end(label('메인보드 RPM = 회전 신호 · % = PWM 출력', 'muted'), False, False, 0)
        self.content.pack_start(board_heading, False, False, 0)
        self.board_grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        self.content.pack_start(self.board_grid, False, False, 0)
        self.board_empty = label('메인보드 팬 센서를 검색하고 있습니다.', 'muted')
        self.board_empty.set_line_wrap(True)
        self.content.pack_start(self.board_empty, False, False, 0)
        scroller.add(self.content)
        outer.pack_start(scroller, True, True, 0)
        footer = row()
        footer.pack_start(label('2초마다 갱신 · 적용 시 관리자 인증 · 앱 종료 시 변경한 제어 복귀', 'muted'), True, True, 0)
        self.timestamp = label('', 'muted', 1)
        footer.pack_end(self.timestamp, False, False, 0)
        outer.pack_end(footer, False, False, 0)

    def notice(self, text: str, error: bool = False) -> None:
        self.notice_label.set_text(text)
        context = self.notice_box.get_style_context()
        if error:
            context.add_class('error')
        else:
            context.remove_class('error')

    def _poll(self) -> None:
        backend = None
        try:
            backend = Backend()
            while not self._stop.is_set():
                try:
                    snapshot = backend.snapshot()
                    GLib.idle_add(self._update_snapshot, snapshot)
                except Exception as exc:
                    GLib.idle_add(self._poll_error, str(exc))
                # Heartbeats already fetch status. Consume their latest result
                # without issuing another command or opening a privileged client.
                client = self._client
                if client is not None:
                    GLib.idle_add(self._worker_status, getattr(client, 'last_status', {}), getattr(client, 'last_error', None))
                self._stop.wait(2)
        except Exception as exc:
            GLib.idle_add(self._poll_error, str(exc))
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass

    def _poll_error(self, message: str) -> bool:
        if not self._destroyed:
            self.status_label.set_text('센서 연결 확인 필요')
            self.notice('센서 읽기 실패: ' + message, error=True)
        return False

    def _update_snapshot(self, snapshot: dict[str, Any]) -> bool:
        if self._destroyed or self.closing:
            return False
        self._has_snapshot = True
        self._last_snapshot = snapshot
        self.cpu_label.set_text(numeric(snapshot.get('cpu_temperature'), '°C', 1))
        self.board_label.set_text(snapshot.get('board') or 'Linux 하드웨어 센서')
        devices = snapshot.get('devices', [])
        ids = tuple(device['id'] for device in devices)
        if ids != self._layout_ids:
            for grid in (self.gpu_grid, self.board_grid):
                for child in grid.get_children():
                    grid.remove(child)
            previous = self.cards
            self.cards = {}
            positions = {'gpu': 0, 'motherboard': 0}
            for device in devices:
                kind = 'gpu' if device.get('kind') == 'gpu' else 'motherboard'
                card = previous.get(device['id']) or DeviceCard(self, device)
                self.cards[device['id']] = card
                grid = self.gpu_grid if kind == 'gpu' else self.board_grid
                index = positions[kind]
                grid.attach(card, index % 2, index // 2, 1, 1)
                positions[kind] += 1
            self._layout_ids = ids
            self.content.show_all()
        for device in devices:
            self.cards[device['id']].update_sensor_options(devices)
            self.cards[device['id']].update(device)
        has_gpu = any(device.get('kind') == 'gpu' for device in devices)
        has_board = any(device.get('kind') != 'gpu' for device in devices)
        self.gpu_empty.set_text('NVIDIA GPU를 읽을 수 없습니다. NVIDIA 드라이버와 NVML 설치 상태를 확인하세요.')
        self.gpu_empty.set_visible(not has_gpu)
        self.board_empty.set_text('메인보드 팬 제어 센서가 없습니다. 지원되는 hwmon 드라이버가 필요하며, 현재는 읽을 수 있는 센서만 표시됩니다.')
        self.board_empty.set_visible(not has_board)
        self.count_label.set_text(f'{len(self.owned)} / {len(devices)}')
        self.timestamp.set_text(time.strftime('%H:%M:%S 업데이트'))
        if not self.busy:
            self.status_label.set_text('제어 연결 확인 필요' if self._worker_error else '● 모니터링 중')
        errors = snapshot.get('errors') or []
        error_text = '\n'.join(str(error) for error in errors)
        control_notice = bool(self._worker_error or self._last_worker_events)
        if error_text and error_text != self._last_poll_error and not control_notice:
            self.notice(error_text, error=True)
        elif not error_text and not self.owned and not self.busy and not control_notice:
            # Do not clear an explicit command error on every successful read.
            if self._last_poll_error or self.notice_label.get_text().startswith('센서를 읽는 중'):
                self.notice('읽기 전용으로 모니터링 중입니다. 출력 또는 곡선의 적용을 누르면 제어를 시작합니다.')
        self._last_poll_error = error_text
        return False

    def _worker_status(self, response: dict[str, Any], error: str | None) -> bool:
        """Reflect recovery/temperature guards reported by background heartbeats."""
        if self._destroyed or self.closing:
            return False
        if 'owned' in response:
            self.owned = response['owned'] or {}
        new_error = error and error != self._worker_error
        self._worker_error = error
        events = tuple(str(event) for event in (response.get('events') or []))
        if events != self._last_worker_events and events:
            self.notice('\n'.join(events[-3:]), error=True)
        self._last_worker_events = events
        if new_error:
            self.notice('관리자 제어 연결이 끊겼습니다: ' + error + '\n팬의 실제 동작을 확인한 뒤 앱을 다시 실행하세요. 자동 복귀 결과는 이 연결에서 확인할 수 없습니다.', error=True)
        self.count_label.set_text(f'{len(self.owned)} / {len(self.cards)}')
        self.restore_all.set_sensitive(bool(self.owned) and not self.busy and not self.screenshot_path and not error)
        for card in self.cards.values():
            card.update(card.device)
        if error and not self.busy:
            self.status_label.set_text('제어 연결 확인 필요')
        return False

    def _set_busy(self, value: bool, message: str = '') -> None:
        self.busy = value
        for card in self.cards.values():
            card.set_busy(value)
        self.restore_all.set_sensitive(bool(self.owned) and not value and not self.screenshot_path and not self._worker_error)
        if value:
            self.spinner.start()
            self.status_label.set_text(message + '…')
        else:
            self.spinner.stop()
            self.status_label.set_text('제어 연결 확인 필요' if self._worker_error else '● 모니터링 중')

    def command(self, payload: dict[str, Any], description: str) -> None:
        if self.busy or self.closing or self.screenshot_path or self._worker_error:
            return
        self._set_busy(True, description)
        self.notice('관리자 인증 또는 장치 응답을 기다리는 중입니다. 센서 모니터링은 계속됩니다.')

        def perform() -> None:
            response = None
            error = None
            try:
                if self._client is None:
                    self._client = WorkerClient()
                response = self._client.request(payload)
                if response.get('ok') is False:
                    raise RuntimeError(response.get('error', '제어 요청이 거부되었습니다.'))
                # Read ownership from the worker, never infer ownership from a click.
                response = self._client.request({'action': 'status'})
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(self._command_finished, response, error, description)

        threading.Thread(target=perform, name='thermaldeck-control', daemon=True).start()

    def _command_finished(self, response: dict[str, Any] | None, error: str | None, description: str) -> bool:
        if self._destroyed:
            return False
        if response and 'owned' in response:
            self.owned = response['owned'] or {}
        self._set_busy(False)
        for card in self.cards.values():
            card.update(card.device)
        self.count_label.set_text(f'{len(self.owned)} / {len(self.cards)}')
        if error:
            self.notice(f'{description} 실패: {error}', error=True)
        else:
            self.notice(f'{description} 완료. 이 앱의 제어는 복귀 버튼 또는 앱 종료 시 해제됩니다.')
        if response:
            self._worker_status(response, getattr(self._client, 'last_error', None))
        if self.closing:
            self._close_client()
        return False

    def _on_close(self, *_: Any) -> bool:
        if self.closing:
            return True
        self.closing = True
        self._stop.set()
        if self.busy:
            self.notice('현재 요청이 끝나면 앱이 변경한 제어를 복귀하고 종료합니다.')
        else:
            self._close_client()
        return True

    def _close_client(self) -> None:
        if self._client is None:
            self.destroy()
            return
        self._set_busy(True, '제어 복귀 후 종료')
        self.notice('앱에서 변경한 냉각 제어를 복귀하는 중입니다.')

        def finish() -> None:
            error = None
            try:
                self._client.close()
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(self._closed_client, error)

        threading.Thread(target=finish, name='thermaldeck-close', daemon=True).start()

    def _closed_client(self, error: str | None) -> bool:
        if error:
            self._set_busy(False)
            self.notice('종료 시 제어 복귀를 확인하지 못했습니다: ' + error, error=True)
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.NONE, text='제어 복귀를 확인하지 못했습니다')
            dialog.format_secondary_text(error + '\n\n제어 프로세스가 종료되어 이 창에서 복귀를 다시 시도할 수 없습니다. 팬 회전과 온도를 확인하세요. 메인보드 팬의 복귀가 실패했다면 재부팅 후 BIOS 팬 설정을 확인하세요.')
            dialog.add_button('상태 확인 후 닫기', Gtk.ResponseType.CLOSE)
            dialog.connect('response', self._close_error_response)
            dialog.show_all()
        else:
            self.destroy()
        return False

    def _close_error_response(self, dialog: Gtk.MessageDialog, response: int) -> None:
        dialog.destroy()
        self.destroy()

    def _on_destroy(self, *_: Any) -> None:
        self._destroyed = True
        self._stop.set()
        if Gtk.main_level():
            Gtk.main_quit()

    def _save_screenshot(self) -> bool:
        if not self._has_snapshot and time.monotonic() - self._screenshot_start < 10:
            return True
        window = self.get_window()
        if window is not None:
            pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, self.get_allocated_width(), self.get_allocated_height())
            if pixbuf:
                destination = Path(self.screenshot_path).expanduser().resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                pixbuf.savev(str(destination), 'png', [], [])
                print(destination, flush=True)
        self._on_close()
        return False


def run_gui(screenshot_path: str | None = None) -> int:
    """Open the GTK dashboard; screenshot mode never enables hardware controls."""
    initialized, _ = Gtk.init_check(None)
    if not initialized:
        raise RuntimeError('그래픽 세션을 찾을 수 없습니다. 데스크톱 터미널에서 실행하세요.')
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # D-Bus activation presents the existing dashboard when launched again.
    # Screenshot runs are independent and cannot acquire hardware control.
    flags = Gio.ApplicationFlags.NON_UNIQUE if screenshot_path else Gio.ApplicationFlags.FLAGS_NONE
    application = Gtk.Application(application_id='io.github.thermaldeck.ThermalDeck', flags=flags)

    def activate(app: Gtk.Application) -> None:
        windows = app.get_windows()
        if windows:
            windows[0].present()
            return
        window = Dashboard(screenshot_path=screenshot_path)
        app.add_window(window)
        window.present()

    application.connect('activate', activate)
    return application.run(['thermaldeck'])
