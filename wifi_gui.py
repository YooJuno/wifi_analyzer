#!/usr/bin/env python3
"""Cross-platform Wi-Fi troubleshooting dashboard.

The application separates current link quality, observed BSS/channel pressure,
signal stability over time, and gateway/Internet/DNS/HTTPS reachability.
Platform-specific collection lives in the platforms package; this module owns
presentation and orchestration.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, font as tkfont, ttk

from diagnostics import (analyze_connection, assess_rssi, assess_snr,
                         recommend_channels, rf_relations,
                         run_path_diagnostics)
from wifi_core import AP, ConnectionInfo, ScanSnapshot, assign_keys, channel_to_frequency
from measurement_log import MeasurementRecorder
from wifi_platform import (platform_label, request_location_permission,
                           scan_snapshot)


BG = "#f3f5f7"
SURFACE = "#ffffff"
CARD = SURFACE
ELEVATED = "#e9edf1"
BORDER = "#d5dce3"
GRID = "#e8edf2"
FG = "#17212b"
MUTED = "#3e4b58"
BLUE = "#286ba6"
GREEN = "#317451"
YELLOW = "#936113"
RED = "#b14036"
SELECTION = "#dfebf6"
FONT = "AnalyzerBody"
SMALL_FONT = "AnalyzerSmall"
HEADING_FONT = "AnalyzerHeading"
VALUE_FONT = "AnalyzerValue"
MONO_FONT = "AnalyzerMono"
RECENT_SECONDS = 300
HISTORY_SECONDS = 3600
MAX_HISTORY_NETWORKS = 512

STATUS_COLORS = {
    "critical": RED, "failed": RED, "warning": YELLOW, "info": BLUE,
    "ok": GREEN, "unknown": MUTED,
}
STATUS_TEXT = {
    "critical": "문제", "failed": "실패", "warning": "주의", "info": "정보",
    "ok": "정상", "unknown": "미측정",
}
PALETTE = (
    "#286ba6", "#438477", "#9c7040", "#78659a", "#a65e63", "#527c96",
)
RSSI_TOP, RSSI_BOTTOM = -30, -100

BAND_AXES = {
    2: (2400, 2500, list(range(1, 14))),
    5: (5160, 5845, [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112,
                     116, 120, 124, 128, 132, 136, 140, 144, 149, 153, 157,
                     161, 165]),
    6: (5925, 7125, list(range(5, 230, 16))),
}


def stable_color(key: str) -> str:
    digest = hashlib.blake2b(str(key).encode("utf-8", "replace"), digest_size=2).digest()
    return PALETTE[int.from_bytes(digest, "big") % len(PALETTE)]


def display_band(band: int | None) -> str:
    return {2: "2.4 GHz", 5: "5 GHz", 6: "6 GHz"}.get(band, "–")


def display_width(width: int | None, known: bool = True) -> str:
    if not width or not known:
        return "미제공"
    return f"{width} MHz"


def compact_text(value: str, limit: int = 28) -> str:
    """Keep summary cards stable while preserving full values elsewhere."""
    value = str(value)
    return value if len(value) <= limit else value[:limit - 1] + "…"


def source_is_reliable(snapshot: ScanSnapshot) -> bool:
    return not snapshot.warnings and all(ap.bssid and ap.width_known and not ap.rssi_estimated for ap in snapshot.aps)


def recent_samples(samples, now, seconds=RECENT_SECONDS):
    """Use a time window rather than letting old outliers dominate diagnosis."""
    return [sample for sample in samples if now - seconds <= sample[0] <= now]


def connection_identity(connection):
    if not connection.connected:
        return None
    return (connection.interface, (connection.bssid or "").lower().replace("-", ":"),
            connection.ssid, connection.band, connection.channel)


class MetricReadout(tk.Frame):
    """One unboxed instrument readout in a shared measurement strip."""

    def __init__(self, master, title: str):
        super().__init__(master, bg=SURFACE, bd=0)
        body = tk.Frame(self, bg=SURFACE)
        body.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(body, text=title, bg=CARD, fg=MUTED, anchor="w",
                 font=SMALL_FONT).pack(fill="x")
        self.value_label = tk.Label(body, text="–", bg=CARD, fg=FG, anchor="w",
                                    font=VALUE_FONT)
        self.value_label.pack(fill="x", pady=(5, 3))
        self.detail_label = tk.Label(body, text="측정 대기 중", bg=CARD, fg=MUTED,
                                     anchor="w", justify="left",
                                     font=SMALL_FONT)
        self.detail_label.pack(fill="x")
        self.bind("<Configure>", lambda event: self.detail_label.configure(
            wraplength=max(80, event.width - 32)))

    def set(self, value: str, detail: str = "", status: str = "unknown"):
        color = STATUS_COLORS.get(status, MUTED)
        self.value_label.configure(text=value, fg=FG)
        self.detail_label.configure(text=detail or " ", fg=color if status in {
            "critical", "failed", "warning"} else MUTED)


class PlotCanvas(tk.Canvas):
    """Coalesce resize events and postpone drawing until the plot is visible."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._draw_job = None
        self.bind("<Configure>", self.request_redraw)
        self.bind("<Map>", self.request_redraw)

    def request_redraw(self, _event=None):
        if self._draw_job is None and self.winfo_viewable():
            self._draw_job = self.after_idle(self._draw)

    def _draw(self):
        self._draw_job = None
        if self.winfo_viewable():
            self.redraw()


class SpectrumCanvas(PlotCanvas):
    """Estimated occupied-channel map from BSS scan observations."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=360)
        self.band = 5
        self.aps: list[AP] = []
        self.selected_key: str | None = None
        self.current_key: str | None = None

    def set_data(self, aps, band, selected_key=None, current_key=None):
        self.band = band
        self.aps = [ap for ap in aps if ap.band == band]
        self.selected_key = selected_key
        self.current_key = current_key
        self.request_redraw()

    def _x(self, frequency):
        low, high, _channels = BAND_AXES[self.band]
        usable = max(1, self.width - self.left - self.right)
        return self.left + (frequency - low) / (high - low) * usable

    def _y(self, rssi):
        value = max(RSSI_BOTTOM, min(RSSI_TOP, rssi))
        usable = max(1, self.height - self.top - self.bottom)
        return self.top + (RSSI_TOP - value) / (RSSI_TOP - RSSI_BOTTOM) * usable

    @staticmethod
    def _curve(x0, x1, peak, base, points=44):
        import math
        coords = []
        shoulder = 0.2
        for index in range(points + 1):
            ratio = index / points
            if ratio < shoulder:
                gain = 0.5 - 0.5 * math.cos(math.pi * ratio / shoulder)
            elif ratio > 1 - shoulder:
                gain = 0.5 - 0.5 * math.cos(math.pi * (1 - ratio) / shoulder)
            else:
                gain = 1.0
            coords.extend((x0 + (x1 - x0) * ratio, base + (peak - base) * gain))
        return coords

    def redraw(self):
        self.delete("all")
        self.width, self.height = self.winfo_width(), self.winfo_height()
        if self.width < 180 or self.height < 120 or self.band not in BAND_AXES:
            return
        self.left, self.right, self.top, self.bottom = 48, 18, 26, 38
        low, high, channels = BAND_AXES[self.band]

        self.create_text(self.left, 7, text="수신 신호 (dBm)", anchor="nw",
                         fill=MUTED, font=SMALL_FONT)
        self.create_text(self.width - self.right, 7,
                         text="곡선은 스캔 기반 점유 추정 · 실제 airtime 아님",
                         anchor="ne", fill=MUTED, font=SMALL_FONT)
        for level in range(-90, -29, 10):
            y = self._y(level)
            self.create_line(self.left, y, self.width - self.right, y, fill=GRID)
            self.create_text(self.left - 7, y, text=str(level), anchor="e",
                             fill=MUTED, font=SMALL_FONT)

        for channel in channels:
            frequency = channel_to_frequency(channel, self.band)
            if not low <= frequency <= high:
                continue
            x = self._x(frequency)
            self.create_line(x, self.top, x, self.height - self.bottom, fill=GRID,
                             dash=(1, 4))
            self.create_text(x, self.height - self.bottom + 7, text=str(channel),
                             anchor="n", fill=MUTED, font=SMALL_FONT)

        selected = next((ap for ap in self.aps if ap.key == self.selected_key), None)
        current = next((ap for ap in self.aps if ap.key == self.current_key), None)
        focus = selected or current
        relation_keys = set()
        if focus:
            relation_keys = {relation.other.key for relation in rf_relations(focus, self.aps)}
            start, end = focus.rng
            if focus.width_known:
                self.create_rectangle(self._x(start), self.top, self._x(end),
                                      self.height - self.bottom, fill="#edf4fa",
                                      outline="")

        base = self._y(RSSI_BOTTOM)
        labels = []
        for ap in sorted(self.aps, key=lambda item: item.rssi):
            start, end = ap.rng
            x0 = max(self.left, self._x(start))
            x1 = min(self.width - self.right, self._x(end))
            peak = self._y(ap.rssi)
            color = stable_color(ap.key)
            is_current = ap.key == self.current_key
            is_selected = ap.key == self.selected_key
            relevant = not focus or is_current or is_selected or ap.key in relation_keys
            if ap.width_known:
                self.create_line(self._curve(x0, x1, peak, base),
                                 fill=color if relevant else BORDER,
                                 width=3 if (is_current or is_selected) else (2 if relevant else 1))
            else:
                x = self._x(ap.center_frequency)
                self.create_line(x, base, x, peak, fill=color, width=1, dash=(3, 3))
                self.create_oval(x - 3, peak - 3, x + 3, peak + 3, fill=color, outline="")
            if is_current or is_selected or (relevant and ap.rssi >= -65):
                name = ap.ssid or "(숨김)"
                if len(name) > 18:
                    name = name[:17] + "…"
                if not ap.width_known:
                    name += " (폭 미제공)"
                prefix = "● " if is_current else ""
                labels.append(((x0 + x1) / 2, peak - 7, prefix + name, color))

        occupied = []
        for x, y, text, color in sorted(labels, key=lambda item: item[1]):
            while any(abs(x - ox) < 95 and abs(y - oy) < 16 for ox, oy in occupied):
                y -= 15
            if y >= self.top:
                self.create_text(x, y, text=text, anchor="s", fill=color,
                                 font=SMALL_FONT)
                occupied.append((x, y))


class HistoryCanvas(PlotCanvas):
    """RSSI/noise timeline with real timestamps."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=340)
        self.samples = []
        self.title = ""

    def set_data(self, title, samples):
        self.title = title
        self.samples = list(samples)
        self.request_redraw()

    def redraw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 180 or height < 100:
            return
        left, right, top, bottom = 50, 18, 34, 38
        self.create_text(left, 10, text=self.title or "네트워크를 선택하세요",
                         anchor="nw", fill=FG, font=HEADING_FONT)
        if not self.samples:
            self.create_text(width / 2, height / 2, text="수집된 신호 이력이 없습니다",
                             fill=MUTED, font=FONT)
            return

        for level in range(-90, -29, 10):
            y = top + (RSSI_TOP - level) / (RSSI_TOP - RSSI_BOTTOM) * (height - top - bottom)
            self.create_line(left, y, width - right, y, fill=GRID)
            self.create_text(left - 8, y, text=str(level), anchor="e", fill=MUTED,
                             font=SMALL_FONT)

        first_time = self.samples[0][0]
        last_time = max(first_time + 1, self.samples[-1][0])

        def point(sample_time, dbm):
            x = left + (sample_time - first_time) / (last_time - first_time) * (width - left - right)
            value = max(RSSI_BOTTOM, min(RSSI_TOP, dbm))
            y = top + (RSSI_TOP - value) / (RSSI_TOP - RSSI_BOTTOM) * (height - top - bottom)
            return x, y

        rssi_points, noise_points = [], []
        for sample_time, rssi, noise in self.samples:
            if rssi is not None:
                rssi_points.extend(point(sample_time, rssi))
            if noise is not None and noise < 0:
                noise_points.extend(point(sample_time, noise))
        if len(noise_points) >= 4:
            self.create_line(noise_points, fill=MUTED, width=1, dash=(5, 3))
        if len(rssi_points) >= 4:
            self.create_line(rssi_points, fill=BLUE, width=2)
        elif rssi_points:
            x, y = rssi_points[-2:]
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=BLUE, outline="")

        self.create_text(left, height - 8, text=time.strftime("%H:%M:%S", time.localtime(first_time)),
                         anchor="sw", fill=MUTED, font=SMALL_FONT)
        self.create_text(width - right, height - 8,
                         text=time.strftime("%H:%M:%S", time.localtime(self.samples[-1][0])),
                         anchor="se", fill=MUTED, font=SMALL_FONT)
        self.create_text(width - right, top, text="━ RSSI    ┄ Noise", anchor="ne",
                         fill=MUTED, font=SMALL_FONT)


class PressureCanvas(PlotCanvas):
    """Relative candidate-channel comparison chart."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=220)
        self.scores = []

    def set_data(self, scores):
        self.scores = list(scores)
        self.request_redraw()

    def redraw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 160 or height < 100:
            return
        if not self.scores:
            self.create_text(width / 2, height / 2,
                             text="이 대역/폭의 채널 후보 데이터가 없습니다",
                             fill=MUTED, font=FONT)
            return
        scores = sorted(self.scores, key=lambda item: item.channel)
        left, right, top, bottom = 44, 12, 24, 34
        values = [item.pressure_dbm if item.pressure_dbm is not None else -100 for item in scores]
        low, high = -100, max(-40, max(values) + 5)
        slot = (width - left - right) / len(scores)
        for index, (score, value) in enumerate(zip(scores, values)):
            normalized = max(0.03, min(1.0, (value - low) / (high - low)))
            bar_height = normalized * (height - top - bottom)
            x0 = left + index * slot + slot * 0.16
            x1 = left + (index + 1) * slot - slot * 0.16
            y0 = height - bottom - bar_height
            color = BLUE
            self.create_rectangle(x0, y0, x1, height - bottom, fill=color, outline="")
            self.create_text((x0 + x1) / 2, height - bottom + 7, text=str(score.channel),
                             anchor="n", fill=MUTED, font=SMALL_FONT)
            label = "없음" if score.pressure_dbm is None else f"{score.pressure_dbm:.0f}"
            self.create_text((x0 + x1) / 2, y0 - 3, text=label, anchor="s",
                             fill=FG, font=SMALL_FONT)
        comparison_width = self.scores[0].width if self.scores else 0
        self.create_text(8, 7,
                         text=f"{comparison_width} MHz · 추정 RF 압력 (dBm) · 낮을수록 여유",
                         anchor="nw", fill=MUTED, font=SMALL_FONT)


class App(tk.Tk):
    SCAN_HISTORY = 900

    def __init__(self):
        super().__init__(className="WiFiAnalyzer")
        self.title("WiFi Analyzer")
        self.geometry("1440x900")
        self.minsize(1100, 740)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.events = queue.Queue()
        self.snapshot = ScanSnapshot([])
        self.aps: list[AP] = []
        self.connection = ConnectionInfo()
        self.current_ap: AP | None = None
        self.selected_key: str | None = None
        self.history = collections.defaultdict(
            lambda: collections.deque(maxlen=self.SCAN_HISTORY))
        self.findings = []
        self.channel_scores = []
        self.path_result = None
        self.scan_busy = False
        self.path_busy = False
        self.paused = False
        self.scan_after_id = None
        self.closed = False
        self.initial_channel_band_selected = False
        self.path_buttons = []
        self._dirty_tabs = set()
        self._score_cache = {}
        self._query_after_id = None
        self._connection_id = None
        self._connection_generation = 0
        self._last_elapsed = 0.0
        self.recorder = None
        self._log_error = ""
        self._last_log_state = None

        self.interval = tk.DoubleVar(value=10.0)
        self.query = tk.StringVar()
        self.network_band = tk.StringVar(value="모든 대역")
        self.channel_band = tk.StringVar(value="5 GHz")
        self.channel_width = tk.StringVar(value="자동")
        self.problem_only = tk.BooleanVar(value=False)
        self.show_hidden = tk.BooleanVar(value=True)
        self.history_window = tk.StringVar(value="5분")
        self.sort_column = "rssi"
        self.sort_reverse = True

        self._configure_fonts()
        self._configure_styles()
        self._load_icon()
        self._build_header()
        self._build_notebook()
        self._build_menu()
        self._location_manager = None
        self.after(500, self._request_location)
        self.after(150, self._pump)
        self.after(50, self.scan_now)

    def _configure_fonts(self):
        # Keep native system families, including the platform's Korean fallback.
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=13)
        self._fonts = []
        for name, size, weight, family in (
            (FONT, 13, "normal", base.actual("family")),
            (SMALL_FONT, 12, "normal", base.actual("family")),
            (HEADING_FONT, 13, "bold", base.actual("family")),
            (VALUE_FONT, 18, "normal", base.actual("family")),
            (MONO_FONT, 13, "normal", tkfont.nametofont("TkFixedFont").actual("family")),
        ):
            self._fonts.append(tkfont.Font(self, name=name, family=family,
                                          size=size, weight=weight))
        self.option_add("*Font", FONT)

    def _load_icon(self):
        resources = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon = resources / "assets" / "wifi-analyzer.png"
        if icon.exists():
            self._icon_image = tk.PhotoImage(file=str(icon))
            self.iconphoto(True, self._icon_image)

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT, background=BG, foreground=FG)
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0),
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        padding=(19, 10), borderwidth=0, font=FONT,
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        style.map("TNotebook.Tab", background=[("selected", SELECTION)],
                  foreground=[("selected", BLUE)])
        style.configure("Analyzer.Treeview", background=SURFACE,
                        fieldbackground=SURFACE, foreground=FG, borderwidth=0,
                        rowheight=34, font=FONT)
        style.layout("Analyzer.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Analyzer.Treeview.Heading", [
            ("Treeheading.cell", {"sticky": "nswe"}),
            ("Treeheading.padding", {"sticky": "nswe", "children": [
                ("Treeheading.text", {"sticky": "we"}),
            ]}),
        ])
        style.configure("Analyzer.Treeview.Heading", background=ELEVATED,
                        foreground=MUTED, borderwidth=0, padding=(8, 7), font=SMALL_FONT)
        style.map("Analyzer.Treeview", background=[("selected", SELECTION)],
                  foreground=[("selected", FG)])
        style.configure("Dark.TCombobox", fieldbackground=ELEVATED,
                        background=ELEVATED, foreground=FG, arrowcolor=FG, padding=5)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", ELEVATED)],
                  foreground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", ELEVATED)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        style.configure("TButton", font=FONT, padding=(12, 7), background=SURFACE,
                        foreground=FG, bordercolor=BORDER, focuscolor=BLUE,
                        lightcolor=SURFACE, darkcolor=SURFACE, relief="flat")
        style.map("TButton", background=[("active", ELEVATED)],
                  foreground=[("disabled", MUTED)])
        style.configure("Primary.TButton", background=BLUE, foreground="#ffffff")
        style.map("Primary.TButton", background=[("disabled", ELEVATED),
                                                 ("active", "#205888")],
                  foreground=[("disabled", MUTED), ("active", "#ffffff")])
        style.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TSpinbox", fieldbackground=SURFACE, foreground=FG, padding=5)
        self.option_add("*TCombobox*Listbox.selectBackground", SELECTION)
        self.option_add("*TCombobox*Listbox.selectForeground", FG)

    def _button(self, master, text, command, primary=False):
        return ttk.Button(master, text=text, command=command,
                          style="Primary.TButton" if primary else "TButton")

    def _build_header(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 14))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="WiFi Analyzer", bg=BG, fg=FG,
                 font=("TkDefaultFont", 17, "bold")).pack(side="left")

        controls = tk.Frame(header, bg=BG)
        controls.pack(side="right")
        self.scan_button = self._button(controls, "스캔", self.scan_now, primary=True)
        self.scan_button.pack(side="left", padx=(0, 6))
        self.pause_button = self._button(controls, "일시정지", self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 6))
        self.path_button = self._button(controls, "경로 검사", self.run_path_test)
        self.path_button.pack(side="left", padx=(0, 6))
        self.path_buttons.append((self.path_button, "경로 검사"))
        self._button(controls, "내보내기…", self.export_report).pack(side="left", padx=(0, 15))
        self.log_button = self._button(controls, "기록 시작…", self.toggle_logging)
        self.log_button.pack(side="left", padx=(0, 15))
        tk.Label(controls, text="간격", bg=BG, fg=MUTED, font=SMALL_FONT).pack(side="left")
        ttk.Spinbox(controls, from_=5, to=120, increment=5, width=5,
                    textvariable=self.interval, command=self.schedule_scan).pack(
                        side="left", padx=(5, 4))
        tk.Label(controls, text="초", bg=BG, fg=MUTED, font=SMALL_FONT).pack(side="left")

        self.banner = tk.Label(self, text="", bg="#fff3db", fg=YELLOW,
                               anchor="w", justify="left", padx=12, pady=6,
                               font=SMALL_FONT)
        self.banner.bind("<Configure>", lambda event: self.banner.configure(
            wraplength=max(200, event.width - 24)))
        footer = tk.Frame(self, bg=BG)
        footer.pack(side="bottom", fill="x", padx=18, pady=(0, 9))
        self.log_status = tk.Label(footer, text="기록 꺼짐", bg=BG, fg=MUTED, font=SMALL_FONT)
        self.log_status.pack(side="right", padx=(16, 0))
        self.scan_status = tk.Label(footer, text="초기화 중", bg=BG, fg=MUTED,
                                    anchor="w", font=SMALL_FONT)
        self.scan_status.pack(side="left", fill="x", expand=True)

    def _build_menu(self):
        shortcut = "Command" if self.tk.call("tk", "windowingsystem") == "aqua" else "Control"
        if shortcut == "Command":
            self.createcommand("tk::mac::Quit", self.close)
        label = "⌘" if shortcut == "Command" else "Ctrl+"
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="리포트 내보내기…", accelerator=f"{label}S", command=self.export_report)
        file_menu.add_separator()
        file_menu.add_command(label="측정 기록 시작 / 중지", accelerator=f"{label}L", command=self.toggle_logging)
        file_menu.add_command(label="로그 폴더 열기", command=self.open_log_folder)
        menu.add_cascade(label="파일", menu=file_menu)
        measurement = tk.Menu(menu, tearoff=False)
        for text, key, command in (("지금 스캔", "r", self.scan_now),
                                   ("일시정지 / 재개", "p", self.toggle_pause),
                                   ("경로 검사", "d", self.run_path_test)):
            measurement.add_command(label=text, accelerator=f"{label}{key.upper()}", command=command)
            self.bind(f"<{shortcut}-{key}>", lambda _event, action=command: action())
        self.bind(f"<{shortcut}-s>", lambda _event: self.export_report())
        self.bind(f"<{shortcut}-l>", lambda _event: self.toggle_logging())
        menu.add_cascade(label="측정", menu=measurement)
        self.configure(menu=menu)

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self, style="TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.overview_tab = tk.Frame(self.notebook, bg=BG)
        self.network_tab = tk.Frame(self.notebook, bg=BG)
        self.channel_tab = tk.Frame(self.notebook, bg=BG)
        self.history_tab = tk.Frame(self.notebook, bg=BG)
        self.path_tab = tk.Frame(self.notebook, bg=BG)
        for frame, title in (
            (self.overview_tab, "모니터"), (self.network_tab, "네트워크"),
            (self.channel_tab, "채널 분석"), (self.history_tab, "시간 추이"),
            (self.path_tab, "경로 진단"),
        ):
            self.notebook.add(frame, text=title)
        self._build_overview()
        self._build_networks()
        self._build_channels()
        self._build_history()
        self._build_path()
        self._dirty_tabs.update(str(frame) for frame in (
            self.overview_tab, self.network_tab, self.channel_tab, self.history_tab, self.path_tab))
        self.notebook.bind("<<NotebookTabChanged>>", self._refresh_active_tab)

    def _panel(self, master, title):
        panel = tk.Frame(master, bg=SURFACE, bd=0)
        tk.Label(panel, text=title, bg=CARD, fg=FG, anchor="w",
                 font=HEADING_FONT).pack(fill="x", padx=14, pady=(13, 9))
        return panel

    def _build_overview(self):
        cards = tk.Frame(self.overview_tab, bg=SURFACE)
        cards.pack(fill="x", pady=(12, 12))
        for column, weight in enumerate((3, 2, 2, 3, 2)):
            cards.grid_columnconfigure(column, weight=weight, uniform="measurement")
        self.connection_card = MetricReadout(cards, "연결 / 인터페이스")
        self.signal_card = MetricReadout(cards, "RSSI")
        self.snr_card = MetricReadout(cards, "SNR")
        self.link_card = MetricReadout(cards, "채널 / 링크")
        self.path_card = MetricReadout(cards, "경로 검사")
        for column, card in enumerate((self.connection_card, self.signal_card,
                                      self.snr_card, self.link_card, self.path_card)):
            card.grid(row=0, column=column, sticky="nsew")

        findings_panel = self._panel(self.overview_tab, "점검 결과")
        findings_panel.pack(side="bottom", fill="x", pady=(12, 0))

        split = tk.PanedWindow(self.overview_tab, orient="horizontal", bg=BG,
                               sashwidth=7, bd=0, relief="flat")
        split.pack(fill="both", expand=True)
        history_panel = self._panel(split, "수신 신호 · 최근 5분")
        recommend_panel = self._panel(split, "채널 후보")
        split.add(history_panel, stretch="always", minsize=550)
        split.add(recommend_panel, stretch="never", minsize=350, width=400)
        self.overview_history = HistoryCanvas(history_panel)
        self.overview_history.configure(height=240)
        self.overview_history.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        columns = ("severity", "title", "evidence")
        findings_table = tk.Frame(findings_panel, bg=SURFACE)
        findings_table.pack(fill="x", padx=8)
        self.findings_tree = ttk.Treeview(findings_table, columns=columns, show="headings",
                                          style="Analyzer.Treeview", selectmode="browse", height=4)
        for key, text, width, anchor in (
            ("severity", "상태", 80, "center"), ("title", "항목", 320, "w"),
            ("evidence", "측정 근거", 490, "w"),
        ):
            self.findings_tree.heading(key, text=text)
            self.findings_tree.column(key, width=width, anchor=anchor,
                                      stretch=key == "evidence")
        for severity, color in STATUS_COLORS.items():
            self.findings_tree.tag_configure(severity, foreground=color if severity in {
                "critical", "warning"} else FG)
        scrollbar = ttk.Scrollbar(findings_table, orient="vertical", command=self.findings_tree.yview)
        self.findings_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.findings_tree.pack(side="left", fill="both", expand=True)
        self.findings_tree.bind("<<TreeviewSelect>>", self._show_finding)
        self.finding_detail = tk.Label(findings_panel, text="측정 대기 중", bg=SURFACE,
                                        fg=MUTED, anchor="w", justify="left", font=FONT)
        self.finding_detail.pack(fill="x", padx=16, pady=(9, 12))
        self.finding_detail.bind("<Configure>", lambda event: self.finding_detail.configure(
            wraplength=max(200, event.width)))

        recommendations_table = tk.Frame(recommend_panel, bg=SURFACE)
        self.recommend_tree = ttk.Treeview(
            recommendations_table, columns=("channel", "overlap", "pressure"),
            show="headings", style="Analyzer.Treeview", selectmode="browse", height=6)
        for key, text, width in (
            ("channel", "채널 / MHz", 125), ("overlap", "BSS", 55),
            ("pressure", "RF · dBm", 100)
        ):
            self.recommend_tree.heading(key, text=text)
            self.recommend_tree.column(key, width=width, anchor="w" if key == "channel" else "e",
                                       stretch=key == "channel")
        scrollbar = ttk.Scrollbar(recommendations_table, orient="vertical", command=self.recommend_tree.yview)
        self.recommend_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.recommend_tree.pack(side="left", fill="both", expand=True)
        self.recommend_tree.bind("<<TreeviewSelect>>", self._show_recommendation)
        self.recommend_detail = tk.Label(recommend_panel, bg=SURFACE, fg=MUTED,
                                          anchor="w", justify="left", font=SMALL_FONT,
                                          text="측정 대기 중")
        self.recommend_detail.bind("<Configure>", lambda event: self.recommend_detail.configure(
            wraplength=max(100, event.width)))
        tk.Label(recommend_panel,
                 text="주변 BSS 기준 상대값 · 실제 채널 사용률 아님",
                 bg=CARD, fg=MUTED, wraplength=340, justify="left",
                 font=SMALL_FONT).pack(side="bottom", fill="x", padx=14, pady=(0, 13))
        self.recommend_detail.pack(side="bottom", fill="x", padx=14, pady=(5, 6))
        recommendations_table.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    def _build_networks(self):
        toolbar = tk.Frame(self.network_tab, bg=BG)
        toolbar.pack(fill="x", pady=(12, 12))
        ttk.Combobox(toolbar, textvariable=self.network_band, state="readonly",
                     style="Dark.TCombobox", width=10,
                     values=("모든 대역", "2.4 GHz", "5 GHz", "6 GHz")).pack(side="left")
        tk.Label(toolbar, text="검색", bg=BG, fg=MUTED, font=SMALL_FONT).pack(
            side="left", padx=(16, 6))
        ttk.Entry(toolbar, textvariable=self.query, width=22, font=FONT).pack(side="left")
        for text, variable in (("문제 관련만", self.problem_only),
                               ("숨김 SSID", self.show_hidden)):
            ttk.Checkbutton(toolbar, text=text, variable=variable,
                            command=self._filter_networks).pack(side="left", padx=(14, 0))
        self.network_count = tk.Label(toolbar, text="BSS 0개", bg=BG, fg=MUTED,
                                      font=SMALL_FONT)
        self.network_count.pack(side="right")

        columns = ("state", "ssid", "bssid", "band", "channel", "width", "rssi",
                   "snr", "load", "phy", "security")
        wrapper = tk.Frame(self.network_tab, bg=SURFACE)
        wrapper.pack(fill="both", expand=True)
        self.network_tree = ttk.Treeview(wrapper, columns=columns, show="headings",
                                         style="Analyzer.Treeview", selectmode="browse")
        specs = (
            ("state", "", 32, "center"), ("ssid", "SSID", 230, "w"),
            ("bssid", "BSSID", 160, "w"), ("band", "대역", 80, "center"),
            ("channel", "채널", 60, "e"), ("width", "폭", 85, "e"),
            ("rssi", "RSSI", 90, "e"), ("snr", "SNR", 75, "e"),
            ("load", "채널 사용", 90, "e"), ("phy", "PHY", 100, "w"),
            ("security", "보안", 150, "w"),
        )
        for key, text, width, anchor in specs:
            self.network_tree.heading(key, text=text,
                                      command=lambda col=key: self.sort_networks(col))
            self.network_tree.column(key, width=width, minwidth=width,
                                     anchor=anchor, stretch=key == "ssid")
        self.network_tree.tag_configure("current", foreground=BLUE)
        self.network_tree.tag_configure("weak", foreground=MUTED)
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self.network_tree.yview)
        horizontal = ttk.Scrollbar(wrapper, orient="horizontal", command=self.network_tree.xview)
        self.network_tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        wrapper.grid_columnconfigure(0, weight=1)
        wrapper.grid_rowconfigure(0, weight=1)
        self.network_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.network_tree.bind("<<TreeviewSelect>>", self.select_network)
        self.query.trace_add("write", self._debounce_search)
        self.network_band.trace_add("write", lambda *_args: self._filter_networks())

    def _build_channels(self):
        toolbar = tk.Frame(self.channel_tab, bg=BG)
        toolbar.pack(fill="x", pady=(12, 12))
        tk.Label(toolbar, text="분석 대역", bg=BG, fg=MUTED,
                 font=SMALL_FONT).pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.channel_band, state="readonly",
                     style="Dark.TCombobox", width=9,
                     values=("2.4 GHz", "5 GHz", "6 GHz")).pack(side="left", padx=(6, 12))
        tk.Label(toolbar, text="비교 폭", bg=BG, fg=MUTED,
                 font=SMALL_FONT).pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.channel_width, state="readonly",
                     style="Dark.TCombobox", width=9,
                     values=("자동", "20 MHz", "40 MHz", "80 MHz", "160 MHz")).pack(
                         side="left", padx=(6, 12))
        tk.Label(toolbar, text="BSS 관측 기반 · 현재 연결은 경쟁 신호에서 제외",
                 bg=BG, fg=MUTED, font=SMALL_FONT).pack(side="right")
        self.channel_band.trace_add("write", lambda *_args: self.update_channel_tab())
        self.channel_width.trace_add("write", lambda *_args: self.update_channel_tab())

        upper = self._panel(self.channel_tab, "채널 점유 지도")
        upper.pack(fill="both", expand=True, pady=(0, 7))
        self.spectrum = SpectrumCanvas(upper)
        self.spectrum.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        lower = self._panel(self.channel_tab, "채널 후보 비교")
        lower.pack(fill="both", expand=False)
        self.pressure_chart = PressureCanvas(lower)
        self.pressure_chart.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_history(self):
        toolbar = tk.Frame(self.history_tab, bg=BG)
        toolbar.pack(fill="x", pady=(12, 12))
        self.history_label = tk.Label(toolbar, text="현재 연결의 신호를 기록합니다",
                                      bg=BG, fg=MUTED, font=SMALL_FONT)
        self.history_label.pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.history_window, state="readonly",
                     style="Dark.TCombobox", values=("5분", "15분", "1시간"), width=6).pack(
                         side="right", padx=(12, 0))
        self.history_window.trace_add("write", lambda *_args: self.update_history())
        self.history_stats = tk.Label(toolbar, text="", bg=BG, fg=FG,
                                      font=SMALL_FONT)
        self.history_stats.pack(side="right")
        panel = self._panel(self.history_tab, "RSSI / Noise 시간 추이")
        panel.pack(fill="both", expand=True)
        self.history_canvas = HistoryCanvas(panel)
        self.history_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_path(self):
        toolbar = tk.Frame(self.path_tab, bg=BG)
        toolbar.pack(fill="x", pady=(12, 12))
        tk.Label(toolbar, text="게이트웨이 → 외부 IP → DNS → HTTPS",
                 bg=BG, fg=MUTED, font=SMALL_FONT).pack(side="left")
        path_tab_button = self._button(
            toolbar, "진단 실행", self.run_path_test, primary=True)
        path_tab_button.pack(side="right")
        self.path_buttons.append((path_tab_button, "진단 실행"))
        cards = tk.Frame(self.path_tab, bg=SURFACE)
        cards.pack(fill="x", pady=(0, 8))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1, uniform="path")
        self.path_cards = {}
        for index, (stage, title) in enumerate((
            ("gateway", "게이트웨이 / LAN"), ("internet", "외부 IP / ISP"),
            ("dns", "DNS 조회"), ("https", "HTTPS 연결"),
        )):
            card = MetricReadout(cards, title)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 7) if index < 3 else 0)
            self.path_cards[stage] = card
        result_panel = self._panel(self.path_tab, "구간 판정")
        result_panel.pack(fill="both", expand=True)
        self.path_conclusion = tk.Label(
            result_panel, text="아직 경로 진단을 실행하지 않았습니다.", bg=CARD,
            fg=MUTED, anchor="nw", justify="left", wraplength=1000,
            font=HEADING_FONT)
        self.path_conclusion.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.path_conclusion.bind("<Configure>", lambda event: self.path_conclusion.configure(
            wraplength=max(200, event.width)))

    def _request_location(self):
        try:
            self._location_manager, _status = request_location_permission()
        except Exception:
            self._location_manager = None

    def interval_ms(self):
        try:
            seconds = float(self.interval.get())
            return int(min(120.0, max(5.0, seconds)) * 1000) if math.isfinite(seconds) else 10000
        except (tk.TclError, ValueError, OverflowError):
            return 10000

    def schedule_scan(self):
        if self.closed or self.paused:
            return
        if self.scan_after_id is not None:
            self.after_cancel(self.scan_after_id)
        self.scan_after_id = self.after(self.interval_ms(), self._scheduled_scan)

    def _scheduled_scan(self):
        self.scan_after_id = None
        if not self.paused:
            self.scan_now()

    def scan_now(self):
        if self.scan_after_id is not None:
            try:
                self.after_cancel(self.scan_after_id)
            except tk.TclError:
                pass
            self.scan_after_id = None
        if self.closed or self.scan_busy:
            return
        self.scan_busy = True
        self.scan_button.configure(text="스캔 중…", state="disabled")
        self.scan_status.configure(text="스캔 중…", fg=BLUE)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        started = time.monotonic()
        try:
            self.events.put(("scan_ok", scan_snapshot(), time.monotonic() - started))
        except Exception as exc:
            self.events.put(("scan_error", str(exc), time.monotonic() - started))

    def run_path_test(self):
        if self.path_busy:
            return
        self._set_path_busy(True)
        self.path_card.set("검사 중", "게이트웨이 · 외부 IP · DNS · HTTPS", "info")
        threading.Thread(target=self._path_worker, args=(self._connection_generation,), daemon=True).start()

    def _set_path_busy(self, busy):
        self.path_busy = busy
        for button, normal_text in self.path_buttons:
            button.configure(
                text="진단 중…" if busy else normal_text,
                state="disabled" if busy else "normal")

    def _path_worker(self, generation):
        try:
            self.events.put(("path_ok", (generation, run_path_diagnostics()), 0))
        except Exception as exc:
            self.events.put(("path_error", (generation, str(exc)), 0))

    def _accept_path_result(self, generation, result):
        if generation != self._connection_generation:
            return False
        self.path_result = result
        self.update_path_result()
        if self.recorder and self.recorder.recording:
            self._log_event("path_diagnostics", {"connection": asdict(self.connection), "result": asdict(result)})
        return True

    def _reset_path_result(self):
        self.path_result = None
        self.path_card.set("미측정", "연결 변경 · 재검사 필요")
        for card in self.path_cards.values():
            card.set("–", "현재 연결에서 측정하지 않음")
        self.path_conclusion.configure(text="현재 연결에서 경로 검사를 실행하세요.", fg=MUTED)

    def _pump(self):
        try:
            while True:
                try:
                    kind, payload, elapsed = self.events.get_nowait()
                except queue.Empty:
                    break
                is_scan = kind.startswith("scan_")
                is_path = kind.startswith("path_")
                try:
                    if kind == "scan_ok":
                        self.accept_snapshot(payload, elapsed)
                    elif kind == "scan_error":
                        self.scan_status.configure(text=f"스캔 실패 · {payload}", fg=RED)
                        self.show_banner(str(payload))
                        self._log_event("scan_error", {"message": str(payload)})
                    elif kind == "path_ok":
                        self._accept_path_result(*payload)
                    elif kind == "path_error":
                        generation, message = payload
                        if generation == self._connection_generation:
                            self.path_card.set("실패", message, "critical")
                            self.path_conclusion.configure(text=message, fg=RED)
                            self._log_event("path_error", {"message": message})
                    elif kind == "log_finished" and payload is self.recorder:
                        self.log_button.configure(text="기록 시작…", state="normal")
                        if payload.error:
                            self._log_error = payload.error
                            self.show_banner(" · ".join(self.snapshot.warnings))
                except Exception as exc:
                    if is_scan:
                        self.scan_status.configure(text=f"표시 처리 실패 · {exc}", fg=RED)
                        self.show_banner(f"스캔 결과 처리 실패: {exc}")
                    elif is_path:
                        self.path_card.set("실패", str(exc), "critical")
                        self.path_conclusion.configure(
                            text=f"경로 진단 결과 처리 실패: {exc}", fg=RED)
                finally:
                    if is_scan:
                        self.scan_busy = False
                        self.scan_button.configure(text="스캔", state="normal")
                        self.schedule_scan()
                    if is_path:
                        self._set_path_busy(False)
        finally:
            if not self.closed:
                self._update_logging_status()
                self.after(150, self._pump)

    def accept_snapshot(self, snapshot: ScanSnapshot, elapsed: float):
        snapshot.aps = assign_keys(snapshot.aps)
        self.snapshot = snapshot
        self.aps = snapshot.aps
        self.connection = snapshot.connection
        self.current_ap = self._merge_current_connection()
        self._score_cache.clear()
        identity = connection_identity(self.connection)
        if identity != self._connection_id:
            self._connection_id = identity
            self._connection_generation += 1
            self._reset_path_result()
        if self.current_ap and not self.initial_channel_band_selected:
            self.channel_band.set(display_band(self.current_ap.band))
            self.initial_channel_band_selected = True
        observed_at = snapshot.timestamp or time.time()
        for ap in self.aps:
            self.history[ap.key].append((observed_at, ap.rssi, ap.noise))
        self._prune_history(observed_at)

        history_values = []
        if self.current_ap:
            history_values = [sample[1] for sample in recent_samples(
                self.history[self.current_ap.key], observed_at)
                              if sample[1] is not None]
        self.findings = analyze_connection(
            self.current_ap, self.aps, history_values,
            source_reliable=source_is_reliable(snapshot), connected=self.connection.connected)
        self._calculate_channel_scores()
        if self.recorder and self.recorder.recording:
            self.recorder.record_scan(snapshot, self.current_ap, self.findings, self.channel_scores, elapsed)
        self.refresh_all()
        self._last_elapsed = elapsed
        stamp = time.strftime("%H:%M:%S", time.localtime(observed_at))
        mode = "일시정지 · " if self.paused else ""
        self.scan_status.configure(
            text=f"{mode}{self.connection.interface or platform_label()}  |  {snapshot.source}  |  "
                 f"BSS {len(self.aps)}  |  마지막 스캔 {stamp}  |  {elapsed:.1f} s", fg=MUTED)
        self.show_banner(" · ".join(snapshot.warnings) if snapshot.warnings else "")

    def _prune_history(self, now):
        for key, samples in list(self.history.items()):
            while samples and samples[0][0] < now - HISTORY_SECONDS:
                samples.popleft()
            if not samples:
                del self.history[key]
        pinned = {self.selected_key, self.current_ap.key if self.current_ap else None}
        oldest = sorted(self.history, key=lambda key: self.history[key][-1][0])
        for key in oldest:
            if len(self.history) <= MAX_HISTORY_NETWORKS:
                break
            if key not in pinned:
                del self.history[key]

    def _merge_current_connection(self):
        connection = self.connection
        current = None
        if connection.bssid:
            normalized = connection.bssid.lower().replace("-", ":")
            current = next((ap for ap in self.aps
                            if (ap.bssid or "").lower().replace("-", ":") == normalized), None)
        current = current or next((ap for ap in self.aps if ap.cur), None)
        if current is None and connection.connected and connection.channel and connection.band:
            candidates = [
                ap for ap in self.aps
                if ap.ch == connection.channel and ap.band == connection.band
                and (not connection.ssid or ap.ssid == connection.ssid)
                and (not connection.width or not ap.width_known or ap.width == connection.width)
            ]
            if len(candidates) == 1:
                current = candidates[0]
        if current is None and connection.connected and connection.channel and connection.band:
            current = AP(connection.ssid, connection.bssid, connection.channel,
                         connection.width or 20, connection.band,
                         connection.rssi if connection.rssi is not None else -99,
                         connection.noise, connection.security, connection.phy, True,
                         width_known=connection.width is not None, rssi_estimated=connection.rssi_estimated)
            assign_keys([current])
            self.aps.append(current)
        if current:
            for ap in self.aps:
                ap.cur = ap is current
            if connection.rssi is not None:
                current.rssi = connection.rssi
                current.rssi_estimated = connection.rssi_estimated
            if connection.noise is not None:
                current.noise = connection.noise
            if connection.width:
                current.width = connection.width
                current.width_known = True
            if connection.channel:
                current.ch = connection.channel
            if connection.band:
                current.band = connection.band
            current.phy = connection.phy or current.phy
            current.sec = connection.security or current.sec
            assign_keys(self.aps)
        return current

    def _calculate_channel_scores(self):
        band = self.current_ap.band if self.current_ap else 5
        width = (self.current_ap.width if self.current_ap and self.current_ap.width_known
                 and band != 2 else 20)
        self.channel_scores = self._channel_scores_for(band, width)

    def _channel_scores_for(self, band, width):
        key = (band, width)
        if key not in self._score_cache:
            self._score_cache[key] = (recommend_channels(self.aps, band, width, exclude=self.current_ap)
                                      if any(ap.band == band for ap in self.aps) else [])
        return self._score_cache[key]

    def refresh_all(self):
        self.update_cards()
        self._dirty_tabs.update(str(frame) for frame in (
            self.overview_tab, self.network_tab, self.channel_tab, self.history_tab))
        self._refresh_active_tab()

    def _tab_visible(self, tab):
        return self.notebook.select() == str(tab)

    def _refresh_active_tab(self, _event=None):
        selected = self.notebook.select()
        if selected not in self._dirty_tabs:
            return
        self._dirty_tabs.discard(selected)
        if selected == str(self.overview_tab):
            self.populate_findings()
            self.populate_recommendations()
            ap = self.current_ap
            samples = recent_samples(self.history.get(ap.key, ()), self.snapshot.timestamp) if ap else []
            self.overview_history.set_data(ap.ssid or ap.bssid or "현재 연결" if ap else "", samples)
        elif selected == str(self.network_tab):
            self.populate_networks()
        elif selected == str(self.channel_tab):
            self.update_channel_tab()
        elif selected == str(self.history_tab):
            self.update_history()
        elif selected == str(self.path_tab):
            self.update_path_result()

    def _debounce_search(self, *_args):
        if self._query_after_id is not None:
            self.after_cancel(self._query_after_id)
        self._query_after_id = self.after(160, self._filter_networks)

    def _filter_networks(self):
        self._query_after_id = None
        self._dirty_tabs.add(str(self.network_tab))
        self._refresh_active_tab()

    def update_cards(self):
        connection = self.connection
        current = self.current_ap
        if not current:
            self.connection_card.set(compact_text(connection.ssid or "연결됨") if connection.connected else "연결 없음",
                                     "연결 정보 일부 미제공" if connection.connected else "Wi-Fi 연결 상태를 확인하세요",
                                     "info" if connection.connected else "critical")
            prefix = "≈" if connection.rssi_estimated else ""
            self.signal_card.set(f"{prefix}{connection.rssi} dBm" if connection.rssi is not None else "–",
                                 "추정 RSSI" if connection.rssi_estimated else ("수신 신호" if connection.rssi is not None else "RSSI 미측정"), "unknown")
            self.snr_card.set("–", "Noise 미측정", "unknown")
            self.link_card.set("–", "채널 정보 없음", "unknown")
            return
        self.connection_card.set(
            compact_text(current.ssid or "(숨김 SSID)", 23),
            f"{current.bssid or 'BSSID 미제공'}\n{connection.interface or platform_label()}", "ok")
        rssi_state = assess_rssi(current.rssi)
        prefix = "≈" if current.rssi_estimated else ""
        self.signal_card.set(f"{prefix}{current.rssi} dBm",
                             f"{'추정 신호' if current.rssi_estimated else '수신 신호'} · {rssi_state.label}", rssi_state.status)
        snr = current.snr if current.noise is not None and current.noise < 0 else None
        snr_state = assess_snr(snr)
        noise_detail = f"잡음 {current.noise} dBm" if current.noise is not None else "잡음 미제공"
        self.snr_card.set(f"{prefix}{snr} dB" if snr is not None else "미측정",
                          noise_detail, snr_state.status)
        rate = f"{connection.tx_rate:.0f} Mbps" if connection.tx_rate else "속도 미제공"
        self.link_card.set(
            f"ch {current.ch} · {display_width(current.width, current.width_known)}",
            f"{display_band(current.band)} · {connection.phy or current.phy or 'PHY 미제공'}\n송신 링크 {rate}",
            "ok" if current.width_known else "info")

    def populate_findings(self):
        selected = self.findings_tree.selection()
        self.findings_tree.configure(height=min(5, max(2, len(self.findings))))
        self.findings_tree.delete(*self.findings_tree.get_children())
        for finding in self.findings:
            self.findings_tree.insert(
                "", "end", iid=f"finding-{finding.code}", tags=(finding.severity,),
                values=(STATUS_TEXT.get(finding.severity, finding.severity),
                        finding.title, finding.evidence))
        if self.findings:
            key = selected[0] if selected and self.findings_tree.exists(selected[0]) else f"finding-{self.findings[0].code}"
            self.findings_tree.selection_set(key)
        self._show_finding()

    def _show_finding(self, _event=None):
        selection = self.findings_tree.selection()
        finding = next((item for item in self.findings
                        if selection and f"finding-{item.code}" == selection[0]), None)
        self.finding_detail.configure(text=f"조치  {finding.action}" if finding else "점검 결과 없음")

    def populate_recommendations(self):
        selected = self.recommend_tree.selection()
        self.recommend_tree.delete(*self.recommend_tree.get_children())
        self._displayed_scores = {}
        for score in self.channel_scores[:6]:
            pressure = "미관측" if score.pressure_dbm is None else f"{score.pressure_dbm:.0f}"
            suffix = " DFS" if score.dfs else ""
            key = f"recommend-{score.band}-{score.channel}-{score.width}"
            self._displayed_scores[key] = score
            self.recommend_tree.insert(
                "", "end", iid=key,
                values=(f"{score.channel} / {score.width}{suffix}", score.overlapping_bss, pressure))
        if self._displayed_scores:
            key = selected[0] if selected and selected[0] in self._displayed_scores else next(iter(self._displayed_scores))
            self.recommend_tree.selection_set(key)
        self._show_recommendation()

    def _show_recommendation(self, _event=None):
        selection = self.recommend_tree.selection()
        score = self._displayed_scores.get(selection[0]) if selection else None
        if score:
            self.recommend_detail.configure(text=f"{display_band(score.band)} · 관측 신뢰도 {score.confidence}\n"
                                            f"강한 BSS {score.strong_bss}개" +
                                            (" · DFS 채널" if score.dfs else ""))
        else:
            band = self.current_ap.band if self.current_ap else 5
            missing = any(ap.band == band and not ap.width_known for ap in self.aps if ap is not self.current_ap)
            self.recommend_detail.configure(text="채널 폭 미제공 · 후보 비교 제한" if missing else "이 대역 / 폭의 채널 후보 없음")

    def filtered_networks(self):
        query = self.query.get().strip().casefold()
        wanted_band = {"2.4 GHz": 2, "5 GHz": 5, "6 GHz": 6}.get(self.network_band.get())
        problem_keys = set()
        if self.problem_only.get() and self.current_ap:
            problem_keys.add(self.current_ap.key)
            problem_keys.update(relation.other.key for relation in
                                rf_relations(self.current_ap, self.aps))
        result = []
        for ap in self.aps:
            if wanted_band and ap.band != wanted_band:
                continue
            if not self.show_hidden.get() and not ap.ssid:
                continue
            if problem_keys and ap.key not in problem_keys:
                continue
            if query and query not in (ap.ssid or "").casefold() and \
                    query not in (ap.bssid or "").casefold():
                continue
            result.append(ap)
        return result

    def _network_sort_value(self, ap):
        utilization = ap.channel_utilization if ap.channel_utilization is not None else -1
        return {
            "state": int(ap.cur), "ssid": (ap.ssid or "").casefold(),
            "bssid": ap.bssid or "", "band": ap.band, "channel": ap.ch,
            "width": ap.width, "rssi": ap.rssi, "snr": ap.snr,
            "load": utilization, "phy": ap.phy or "", "security": ap.sec or "",
        }.get(self.sort_column, ap.rssi)

    def sort_networks(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = column in {"state", "rssi", "snr", "load"}
        self.populate_networks()

    def populate_networks(self):
        if not hasattr(self, "network_tree"):
            return
        items = sorted(self.filtered_networks(), key=self._network_sort_value,
                       reverse=self.sort_reverse)
        existing = set(self.network_tree.get_children())
        visible = set()
        for index, ap in enumerate(items):
            utilization = ("–" if ap.channel_utilization is None else
                           f"{ap.channel_utilization / 255 * 100:.0f}%")
            values = (
                "●" if ap.cur else "", ap.ssid or "(숨김 SSID)", ap.bssid or "미제공",
                display_band(ap.band), ap.ch, display_width(ap.width, ap.width_known),
                f"{'≈' if ap.rssi_estimated else ''}{ap.rssi} dBm",
                f"{ap.snr} dB" if ap.noise is not None and ap.noise < 0 else "–",
                utilization,
                ap.phy or "–", ap.sec or "–",
            )
            tag = "current" if ap.cur else ("weak" if ap.rssi < -80 else "")
            if ap.key in existing:
                self.network_tree.item(ap.key, values=values, tags=(tag,))
                self.network_tree.move(ap.key, "", index)
            else:
                self.network_tree.insert("", "end", iid=ap.key, values=values, tags=(tag,))
            visible.add(ap.key)
        for item_id in existing - visible:
            self.network_tree.delete(item_id)
        self.network_count.configure(text=f"표시 {len(items)}개 / 전체 BSS {len(self.aps)}개")
        if self.selected_key in visible:
            self.network_tree.selection_set(self.selected_key)

    def select_network(self, _event=None):
        selection = self.network_tree.selection()
        key = selection[0] if selection else None
        if key == self.selected_key:
            return
        self.selected_key = key
        selected = self.selected_ap()
        if selected:
            self.channel_band.set(display_band(selected.band))
        self.update_channel_tab()
        self.update_history()

    def selected_ap(self):
        return next((ap for ap in self.aps if ap.key == self.selected_key), None)

    def update_channel_tab(self):
        if not hasattr(self, "spectrum"):
            return
        if not self._tab_visible(self.channel_tab):
            self._dirty_tabs.add(str(self.channel_tab))
            return
        band = {"2.4 GHz": 2, "5 GHz": 5, "6 GHz": 6}.get(self.channel_band.get(), 5)
        current_key = self.current_ap.key if self.current_ap else None
        self.spectrum.set_data(self.aps, band, self.selected_key, current_key)
        requested_width = self.channel_width.get()
        focus = self.selected_ap() or self.current_ap
        if requested_width == "자동":
            if focus and focus.band == band and focus.width_known:
                width = focus.width
            else:
                width = 20 if band in {2, 6} else 80
        else:
            width = int(requested_width.split()[0])
        self.pressure_chart.set_data(self._channel_scores_for(band, width))

    def update_history(self):
        if not hasattr(self, "history_canvas"):
            return
        if not self._tab_visible(self.history_tab):
            self._dirty_tabs.add(str(self.history_tab))
            return
        ap = self.selected_ap() or self.current_ap
        if not ap:
            self.history_canvas.set_data("", [])
            self.history_stats.configure(text="")
            return
        seconds = {"5분": 300, "15분": 900, "1시간": 3600}[self.history_window.get()]
        samples = recent_samples(self.history.get(ap.key, ()), self.snapshot.timestamp, seconds)
        self.history_canvas.set_data(ap.ssid or ap.bssid or "숨김 네트워크", samples)
        values = [sample[1] for sample in samples if sample[1] is not None]
        if values:
            self.history_stats.configure(
                text=f"현재 {values[-1]} dBm · 최소 {min(values)} · 최대 {max(values)} · "
                     f"변동 {max(values) - min(values)} dB · {len(values)}회")
        self.history_label.configure(
            text=f"{ap.bssid or 'BSSID 미제공'} · {display_band(ap.band)} ch{ap.ch}")

    def update_path_result(self):
        result = self.path_result
        if not result:
            return
        for check in result.checks:
            card = self.path_cards.get(check.stage)
            if not card:
                continue
            value = (f"{check.latency_ms:.1f} ms" if check.latency_ms is not None
                     else STATUS_TEXT.get(check.status, check.status))
            detail = check.detail
            if check.loss_percent is not None and "손실" not in detail:
                detail = f"손실 {check.loss_percent:.0f}% · {detail}"
            card.set(value, f"{check.target} · {detail}", check.status)
        self.path_conclusion.configure(
            text=f"{result.conclusion}\n\n검사 시각  " + time.strftime("%H:%M:%S", time.localtime(result.checked_at)),
            fg=STATUS_COLORS.get(result.severity, MUTED))
        self.path_card.set(STATUS_TEXT.get(result.severity, result.severity),
                           time.strftime("%H:%M:%S 검사", time.localtime(result.checked_at)), result.severity)

    def show_banner(self, text):
        text = " · ".join(value for value in (self._log_error, text) if value)
        if text:
            self.banner.configure(text=("기록 오류  |  " if self._log_error else "알림  |  ") + text)
            if not self.banner.winfo_ismapped():
                self.banner.pack(fill="x", padx=12, pady=(0, 6), before=self.notebook)
        else:
            self.banner.pack_forget()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            if self.scan_after_id is not None:
                self.after_cancel(self.scan_after_id)
                self.scan_after_id = None
            self.pause_button.configure(text="다시 시작")
            self.scan_status.configure(text="스캔 일시정지", fg=YELLOW)
            self._log_event("scan_paused")
        else:
            self.pause_button.configure(text="일시정지")
            self._log_event("scan_resumed")
            self.scan_now()

    def _log_event(self, event_type, data=None):
        if self.recorder and self.recorder.recording:
            self.recorder.record_event(event_type, data)

    def toggle_logging(self):
        if self.recorder and not self.recorder.finished.is_set():
            self._log_event("recording_stop")
            self.recorder.stop()
            self.log_button.configure(text="저장 중…", state="disabled")
        else:
            directory = filedialog.askdirectory(
                parent=self, title="측정 로그 저장 폴더",
                initialdir=str(self.recorder.directory.parent if self.recorder else Path.home() / "Documents"))
            if not directory:
                return
            try:
                self.recorder = MeasurementRecorder(
                    directory, metadata={"scan_interval_seconds": self.interval_ms() / 1000},
                    on_finished=lambda recorder: self.events.put(("log_finished", recorder, 0)))
            except OSError as exc:
                self._log_error = f"로그 시작 실패: {exc}"
                self.show_banner(" · ".join(self.snapshot.warnings))
                return
            self._log_error = ""
            self.show_banner(" · ".join(self.snapshot.warnings))
            self.log_button.configure(text="기록 중지", state="normal")
            self.scan_now()
        self._update_logging_status()

    def _update_logging_status(self):
        recorder = self.recorder
        if recorder is None:
            state = ("기록 꺼짐", MUTED)
        elif recorder.error:
            state = (f"기록 실패 · {recorder.samples_written}회 저장", RED)
        elif recorder.finished.is_set():
            state = (f"기록 완료 · {recorder.samples_written}회", MUTED)
        elif not recorder.recording:
            state = (f"기록 저장 중 · {recorder.samples_written}회", MUTED)
        else:
            state = (f"● 기록 중 · {recorder.samples_written}회", GREEN)
        if state != self._last_log_state:
            self.log_status.configure(text=state[0], fg=state[1])
            self._last_log_state = state

    def open_log_folder(self):
        if not self.recorder:
            self.show_banner("측정 기록을 시작하면 로그 폴더를 열 수 있습니다.")
            return
        try:
            folder = str(self.recorder.directory)
            if sys.platform == "win32":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
        except OSError as exc:
            self.show_banner(f"로그 폴더 열기 실패: {exc}")

    def export_report(self):
        if not self.aps:
            self.show_banner("저장할 스캔 데이터가 없습니다.")
            return
        default_name = time.strftime("wifi-report-%Y%m%d-%H%M%S.json")
        path = filedialog.asksaveasfilename(
            parent=self, title="진단 리포트 저장", initialfile=default_name,
            defaultextension=".json", filetypes=(("JSON", "*.json"), ("모든 파일", "*.*")))
        if not path:
            return
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "platform": platform_label(), "source": self.snapshot.source,
            "warnings": list(self.snapshot.warnings), "connection": asdict(self.connection),
            "findings": [asdict(item) for item in self.findings],
            "path_diagnostics": asdict(self.path_result) if self.path_result else None,
            "recommendations": [asdict(item) for item in self.channel_scores[:10]],
            "observations": [
                {
                    "ssid": ap.ssid, "bssid": ap.bssid, "band": ap.band,
                    "channel": ap.ch, "width": ap.width, "width_known": ap.width_known,
                    "rssi": ap.rssi, "rssi_estimated": ap.rssi_estimated, "noise": ap.noise,
                    "snr": ap.snr if ap.noise is not None and ap.noise < 0 else None,
                    "security": ap.sec, "phy": ap.phy,
                    "channel_utilization": ap.channel_utilization,
                    "station_count": ap.station_count, "current": ap.cur,
                }
                for ap in self.aps
            ],
        }
        try:
            with open(path, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2)
            self.scan_status.configure(text=f"리포트 저장 완료 · {path}", fg=GREEN)
        except OSError as exc:
            self.show_banner(f"리포트 저장 실패: {exc}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.recorder:
            self._log_event("app_close")
            self.recorder.stop()
        if self.scan_after_id is not None:
            try:
                self.after_cancel(self.scan_after_id)
            except tk.TclError:
                pass
        self.destroy()


if __name__ == "__main__":
    application = App()
    try:
        application.mainloop()
    finally:
        if application.recorder:
            application.recorder.stop()
