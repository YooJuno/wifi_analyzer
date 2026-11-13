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
import queue
import threading
import time
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, ttk

from diagnostics import (analyze_connection, assess_rssi, assess_snr,
                         recommend_channels, rf_relations,
                         run_path_diagnostics)
from wifi_core import AP, ConnectionInfo, ScanSnapshot, assign_keys, channel_to_frequency
from wifi_platform import (platform_label, request_location_permission,
                           scan_snapshot)


BG = "#0d1117"
SURFACE = "#161b22"
CARD = "#1c2128"
ELEVATED = "#21262d"
BORDER = "#30363d"
GRID = "#252c35"
FG = "#f0f3f6"
MUTED = "#8b949e"
BLUE = "#58a6ff"
GREEN = "#3fb950"
YELLOW = "#d29922"
RED = "#f85149"

STATUS_COLORS = {
    "critical": RED, "failed": RED, "warning": YELLOW, "info": BLUE,
    "ok": GREEN, "unknown": MUTED,
}
STATUS_TEXT = {
    "critical": "문제", "failed": "실패", "warning": "주의", "info": "정보",
    "ok": "정상", "unknown": "미측정",
}
PALETTE = (
    "#58a6ff", "#f85149", "#3fb950", "#d29922", "#bc8cff", "#ff7b72",
    "#39c5cf", "#a5d64f", "#f0883e", "#79c0ff", "#db6d8f", "#56d4a0",
)
RSSI_TOP, RSSI_BOTTOM = -30, -95

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
    return not snapshot.warnings and all(ap.bssid and ap.width_known for ap in snapshot.aps)


class MetricCard(tk.Frame):
    """Compact status card used across Overview and Path tabs."""

    def __init__(self, master, title: str):
        super().__init__(master, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1, bd=0)
        self.accent = tk.Frame(self, bg=MUTED, width=4)
        self.accent.pack(side="left", fill="y")
        body = tk.Frame(self, bg=CARD)
        body.pack(side="left", fill="both", expand=True, padx=12, pady=9)
        tk.Label(body, text=title, bg=CARD, fg=MUTED, anchor="w",
                 font=("TkDefaultFont", 9, "bold")).pack(fill="x")
        self.value_label = tk.Label(body, text="–", bg=CARD, fg=FG, anchor="w",
                                    font=("TkDefaultFont", 17, "bold"))
        self.value_label.pack(fill="x", pady=(3, 1))
        self.detail_label = tk.Label(body, text="측정 대기 중", bg=CARD, fg=MUTED,
                                     anchor="w", justify="left",
                                     font=("TkDefaultFont", 9))
        self.detail_label.pack(fill="x")

    def set(self, value: str, detail: str = "", status: str = "unknown"):
        color = STATUS_COLORS.get(status, MUTED)
        self.accent.configure(bg=color)
        self.value_label.configure(text=value, fg=color if status != "unknown" else FG)
        self.detail_label.configure(text=detail or " ")


class SpectrumCanvas(tk.Canvas):
    """Estimated occupied-channel map from BSS scan observations."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=360)
        self.band = 5
        self.aps: list[AP] = []
        self.selected_key: str | None = None
        self.current_key: str | None = None
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, aps, band, selected_key=None, current_key=None):
        self.band = band
        self.aps = [ap for ap in aps if ap.band == band]
        self.selected_key = selected_key
        self.current_key = current_key
        self.redraw()

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
                         fill=MUTED, font=("TkDefaultFont", 9, "bold"))
        self.create_text(self.width - self.right, 7,
                         text="곡선은 스캔 기반 점유 추정 · 실제 airtime 아님",
                         anchor="ne", fill=MUTED, font=("TkDefaultFont", 9))
        for level in range(-90, -29, 10):
            y = self._y(level)
            self.create_line(self.left, y, self.width - self.right, y, fill=GRID)
            self.create_text(self.left - 7, y, text=str(level), anchor="e",
                             fill=MUTED, font=("TkDefaultFont", 8))

        for channel in channels:
            frequency = channel_to_frequency(channel, self.band)
            if not low <= frequency <= high:
                continue
            x = self._x(frequency)
            self.create_line(x, self.top, x, self.height - self.bottom, fill=GRID,
                             dash=(1, 4))
            self.create_text(x, self.height - self.bottom + 7, text=str(channel),
                             anchor="n", fill=MUTED, font=("TkDefaultFont", 8))

        selected = next((ap for ap in self.aps if ap.key == self.selected_key), None)
        current = next((ap for ap in self.aps if ap.key == self.current_key), None)
        focus = selected or current
        relation_keys = set()
        if focus:
            relation_keys = {relation.other.key for relation in rf_relations(focus, self.aps)}
            start, end = focus.rng
            self.create_rectangle(self._x(start), self.top, self._x(end),
                                  self.height - self.bottom, fill="#10243a",
                                  outline=BLUE, stipple="gray50")

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
            self.create_line(self._curve(x0, x1, peak, base),
                             fill=color if relevant else BORDER,
                             width=3 if (is_current or is_selected) else (2 if relevant else 1))
            if is_current or is_selected or (relevant and ap.rssi >= -65):
                name = ap.ssid or "(숨김)"
                if len(name) > 18:
                    name = name[:17] + "…"
                prefix = "● " if is_current else ""
                labels.append(((x0 + x1) / 2, peak - 7, prefix + name, color))

        occupied = []
        for x, y, text, color in sorted(labels, key=lambda item: item[1]):
            while any(abs(x - ox) < 95 and abs(y - oy) < 16 for ox, oy in occupied):
                y -= 15
            if y >= self.top:
                self.create_text(x, y, text=text, anchor="s", fill=color,
                                 font=("TkDefaultFont", 9, "bold"))
                occupied.append((x, y))


class HistoryCanvas(tk.Canvas):
    """RSSI/noise timeline with real timestamps."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=340)
        self.samples = []
        self.title = ""
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, title, samples):
        self.title = title
        self.samples = list(samples)
        self.redraw()

    def redraw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 180 or height < 100:
            return
        left, right, top, bottom = 50, 18, 34, 38
        self.create_text(left, 10, text=self.title or "네트워크를 선택하세요",
                         anchor="nw", fill=FG, font=("TkDefaultFont", 11, "bold"))
        if not self.samples:
            self.create_text(width / 2, height / 2, text="수집된 신호 이력이 없습니다",
                             fill=MUTED, font=("TkDefaultFont", 11))
            return

        for level in range(-90, -29, 10):
            y = top + (RSSI_TOP - level) / (RSSI_TOP - RSSI_BOTTOM) * (height - top - bottom)
            self.create_line(left, y, width - right, y, fill=GRID)
            self.create_text(left - 8, y, text=str(level), anchor="e", fill=MUTED,
                             font=("TkDefaultFont", 8))

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
            self.create_line(noise_points, fill=MUTED, width=1, dash=(5, 3), smooth=True)
        if len(rssi_points) >= 4:
            self.create_line(rssi_points, fill=BLUE, width=2, smooth=True)
        elif rssi_points:
            x, y = rssi_points[-2:]
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill=BLUE, outline="")

        self.create_text(left, height - 8, text=time.strftime("%H:%M:%S", time.localtime(first_time)),
                         anchor="sw", fill=MUTED, font=("TkDefaultFont", 8))
        self.create_text(width - right, height - 8,
                         text=time.strftime("%H:%M:%S", time.localtime(self.samples[-1][0])),
                         anchor="se", fill=MUTED, font=("TkDefaultFont", 8))
        self.create_text(width - right, top, text="━ RSSI    ┄ Noise", anchor="ne",
                         fill=MUTED, font=("TkDefaultFont", 9))


class PressureCanvas(tk.Canvas):
    """Relative candidate-channel comparison chart."""

    def __init__(self, master):
        super().__init__(master, bg=SURFACE, highlightthickness=0, height=220)
        self.scores = []
        self.bind("<Configure>", lambda _event: self.redraw())

    def set_data(self, scores):
        self.scores = list(scores)
        self.redraw()

    def redraw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 160 or height < 100:
            return
        if not self.scores:
            self.create_text(width / 2, height / 2,
                             text="이 대역/폭의 채널 후보 데이터가 없습니다",
                             fill=MUTED, font=("TkDefaultFont", 10))
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
            color = GREEN if score.strong_bss == 0 else (YELLOW if score.strong_bss <= 1 else RED)
            self.create_rectangle(x0, y0, x1, height - bottom, fill=color, outline="")
            self.create_text((x0 + x1) / 2, height - bottom + 7, text=str(score.channel),
                             anchor="n", fill=MUTED, font=("TkDefaultFont", 8))
            label = "없음" if score.pressure_dbm is None else f"{score.pressure_dbm:.0f}"
            self.create_text((x0 + x1) / 2, y0 - 3, text=label, anchor="s",
                             fill=FG, font=("TkDefaultFont", 8))
        comparison_width = self.scores[0].width if self.scores else 0
        self.create_text(8, 7,
                         text=f"{comparison_width} MHz · 추정 RF 압력 (dBm) · 낮을수록 여유",
                         anchor="nw", fill=MUTED, font=("TkDefaultFont", 9))


class App(tk.Tk):
    SCAN_HISTORY = 900

    def __init__(self):
        super().__init__()
        self.title("WiFi Analyzer")
        self.geometry("1480x920")
        self.minsize(1080, 720)
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

        self.interval = tk.DoubleVar(value=10.0)
        self.query = tk.StringVar()
        self.network_band = tk.StringVar(value="모든 대역")
        self.channel_band = tk.StringVar(value="5 GHz")
        self.channel_width = tk.StringVar(value="자동")
        self.problem_only = tk.BooleanVar(value=False)
        self.show_hidden = tk.BooleanVar(value=True)
        self.sort_column = "rssi"
        self.sort_reverse = True

        self._configure_styles()
        self._build_header()
        self._build_notebook()
        self._location_manager = None
        self.after(500, self._request_location)
        self.after(150, self._pump)
        self.after(50, self.scan_now)

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                        padding=(16, 8), borderwidth=0,
                        font=("TkDefaultFont", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", ELEVATED)],
                  foreground=[("selected", FG)])
        style.configure("Analyzer.Treeview", background=SURFACE,
                        fieldbackground=SURFACE, foreground=FG, borderwidth=0,
                        rowheight=27, font=("TkDefaultFont", 10))
        style.configure("Analyzer.Treeview.Heading", background=ELEVATED,
                        foreground=MUTED, borderwidth=0,
                        font=("TkDefaultFont", 9, "bold"))
        style.map("Analyzer.Treeview", background=[("selected", "#1f4068")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Dark.TCombobox", fieldbackground=ELEVATED,
                        background=ELEVATED, foreground=FG, arrowcolor=FG)
        style.map("Dark.TCombobox", fieldbackground=[("readonly", ELEVATED)],
                  foreground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", ELEVATED)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", "#1f6feb")

    def _button(self, master, text, command, primary=False):
        return tk.Button(
            master, text=text, command=command,
            bg="#1f6feb" if primary else ELEVATED, fg="#ffffff" if primary else FG,
            activebackground="#388bfd" if primary else BORDER,
            activeforeground="#ffffff", relief="flat", bd=0,
            highlightthickness=0, padx=12, pady=6,
            font=("TkDefaultFont", 9, "bold"),
        )

    def _build_header(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 7))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="WiFi Analyzer", bg=BG, fg=FG,
                 font=("TkDefaultFont", 20, "bold")).pack(side="left")
        self.scan_status = tk.Label(left, text="초기화 중", bg=BG, fg=MUTED,
                                    font=("TkDefaultFont", 9))
        self.scan_status.pack(side="left", padx=(12, 0))

        controls = tk.Frame(header, bg=BG)
        controls.pack(side="right")
        self.path_button = self._button(controls, "경로 진단", self.run_path_test, primary=True)
        self.path_button.pack(side="left", padx=(0, 6))
        self.path_buttons.append((self.path_button, "경로 진단"))
        self._button(controls, "리포트 저장", self.export_report).pack(side="left", padx=(0, 6))
        self.pause_button = self._button(controls, "일시정지", self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 6))
        self._button(controls, "지금 스캔", self.scan_now).pack(side="left", padx=(0, 8))
        tk.Label(controls, text="주기", bg=BG, fg=MUTED,
                 font=("TkDefaultFont", 9)).pack(side="left")
        ttk.Spinbox(controls, from_=5, to=120, increment=5, width=5,
                    textvariable=self.interval).pack(side="left", padx=(4, 0))

        self.banner = tk.Label(self, text="", bg="#2d220d", fg="#e3b341",
                               anchor="w", justify="left", padx=12, pady=6,
                               font=("TkDefaultFont", 9))

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self, style="TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.overview_tab = tk.Frame(self.notebook, bg=BG)
        self.network_tab = tk.Frame(self.notebook, bg=BG)
        self.channel_tab = tk.Frame(self.notebook, bg=BG)
        self.history_tab = tk.Frame(self.notebook, bg=BG)
        self.path_tab = tk.Frame(self.notebook, bg=BG)
        for frame, title in (
            (self.overview_tab, "개요"), (self.network_tab, "네트워크"),
            (self.channel_tab, "채널 분석"), (self.history_tab, "시간 추이"),
            (self.path_tab, "경로 진단"),
        ):
            self.notebook.add(frame, text=title)
        self._build_overview()
        self._build_networks()
        self._build_channels()
        self._build_history()
        self._build_path()

    def _panel(self, master, title):
        panel = tk.Frame(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tk.Label(panel, text=title, bg=CARD, fg=FG, anchor="w",
                 font=("TkDefaultFont", 11, "bold")).pack(fill="x", padx=10, pady=(9, 7))
        return panel

    def _build_overview(self):
        cards = tk.Frame(self.overview_tab, bg=BG)
        cards.pack(fill="x", pady=(8, 9))
        for column in range(5):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
        self.connection_card = MetricCard(cards, "현재 연결")
        self.signal_card = MetricCard(cards, "신호")
        self.snr_card = MetricCard(cards, "신호 대 잡음")
        self.link_card = MetricCard(cards, "링크")
        self.path_card = MetricCard(cards, "네트워크 경로")
        for column, card in enumerate((self.connection_card, self.signal_card,
                                      self.snr_card, self.link_card, self.path_card)):
            card.grid(row=0, column=column, sticky="nsew",
                      padx=(0, 7) if column < 4 else 0)

        split = tk.PanedWindow(self.overview_tab, orient="horizontal", bg=BG,
                               sashwidth=7, bd=0, relief="flat")
        split.pack(fill="both", expand=True)
        findings_panel = self._panel(split, "자동 진단 · 근거와 다음 조치")
        recommend_panel = self._panel(split, "상대적으로 여유로운 채널")
        split.add(findings_panel, stretch="always", minsize=650)
        split.add(recommend_panel, stretch="never", minsize=330, width=390)

        columns = ("severity", "title", "evidence", "action")
        self.findings_tree = ttk.Treeview(findings_panel, columns=columns, show="headings",
                                          style="Analyzer.Treeview", selectmode="browse")
        for key, text, width, anchor in (
            ("severity", "상태", 62, "center"), ("title", "진단", 190, "w"),
            ("evidence", "근거", 210, "w"), ("action", "권장 조치", 360, "w"),
        ):
            self.findings_tree.heading(key, text=text)
            self.findings_tree.column(key, width=width, anchor=anchor,
                                      stretch=key in {"evidence", "action"})
        for severity, color in STATUS_COLORS.items():
            self.findings_tree.tag_configure(severity, foreground=color)
        self.findings_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.recommend_tree = ttk.Treeview(
            recommend_panel, columns=("channel", "pressure", "reason"),
            show="headings", style="Analyzer.Treeview", selectmode="none", height=7)
        for key, text, width in (
            ("channel", "후보", 90), ("pressure", "압력", 70), ("reason", "관측 근거", 230)
        ):
            self.recommend_tree.heading(key, text=text)
            self.recommend_tree.column(key, width=width, anchor="w", stretch=key == "reason")
        self.recommend_tree.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        tk.Label(recommend_panel,
                 text="주변 BSS 신호를 비교한 값입니다. 트래픽·비 Wi-Fi 잡음은 측정하지 않습니다.",
                 bg=CARD, fg=MUTED, wraplength=350, justify="left",
                 font=("TkDefaultFont", 8)).pack(fill="x", padx=10, pady=(0, 8))

    def _build_networks(self):
        toolbar = tk.Frame(self.network_tab, bg=BG)
        toolbar.pack(fill="x", pady=(8, 6))
        ttk.Combobox(toolbar, textvariable=self.network_band, state="readonly",
                     style="Dark.TCombobox", width=10,
                     values=("모든 대역", "2.4 GHz", "5 GHz", "6 GHz")).pack(side="left")
        tk.Entry(toolbar, textvariable=self.query, bg=ELEVATED, fg=FG,
                 insertbackground=FG, relief="flat", width=28,
                 font=("TkDefaultFont", 10)).pack(side="left", padx=(7, 0), ipady=5)
        for text, variable in (("문제 관련만", self.problem_only),
                               ("숨김 SSID", self.show_hidden)):
            tk.Checkbutton(toolbar, text=text, variable=variable, command=self.populate_networks,
                           bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                           selectcolor=ELEVATED, font=("TkDefaultFont", 9)).pack(
                               side="left", padx=(10, 0))
        self.network_count = tk.Label(toolbar, text="BSS 0개", bg=BG, fg=MUTED,
                                      font=("TkDefaultFont", 9))
        self.network_count.pack(side="right")

        columns = ("state", "ssid", "bssid", "band", "channel", "width", "rssi",
                   "snr", "load", "phy", "security")
        wrapper = tk.Frame(self.network_tab, bg=SURFACE)
        wrapper.pack(fill="both", expand=True)
        self.network_tree = ttk.Treeview(wrapper, columns=columns, show="headings",
                                         style="Analyzer.Treeview", selectmode="browse")
        specs = (
            ("state", "", 32, "center"), ("ssid", "SSID", 230, "w"),
            ("bssid", "BSSID", 145, "w"), ("band", "대역", 68, "center"),
            ("channel", "채널", 55, "e"), ("width", "폭", 75, "e"),
            ("rssi", "RSSI", 78, "e"), ("snr", "SNR", 65, "e"),
            ("load", "채널 사용", 78, "e"), ("phy", "PHY", 88, "w"),
            ("security", "보안", 150, "w"),
        )
        for key, text, width, anchor in specs:
            self.network_tree.heading(key, text=text,
                                      command=lambda col=key: self.sort_networks(col))
            self.network_tree.column(key, width=width, anchor=anchor, stretch=key == "ssid")
        self.network_tree.tag_configure("current", foreground=GREEN)
        self.network_tree.tag_configure("weak", foreground=MUTED)
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self.network_tree.yview)
        self.network_tree.configure(yscrollcommand=scrollbar.set)
        self.network_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.network_tree.bind("<<TreeviewSelect>>", self.select_network)
        self.query.trace_add("write", lambda *_args: self.populate_networks())
        self.network_band.trace_add("write", lambda *_args: self.populate_networks())

    def _build_channels(self):
        toolbar = tk.Frame(self.channel_tab, bg=BG)
        toolbar.pack(fill="x", pady=(8, 6))
        tk.Label(toolbar, text="분석 대역", bg=BG, fg=MUTED,
                 font=("TkDefaultFont", 9)).pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.channel_band, state="readonly",
                     style="Dark.TCombobox", width=9,
                     values=("2.4 GHz", "5 GHz", "6 GHz")).pack(side="left", padx=(6, 12))
        tk.Label(toolbar, text="비교 폭", bg=BG, fg=MUTED,
                 font=("TkDefaultFont", 9)).pack(side="left")
        ttk.Combobox(toolbar, textvariable=self.channel_width, state="readonly",
                     style="Dark.TCombobox", width=9,
                     values=("자동", "20 MHz", "40 MHz", "80 MHz", "160 MHz")).pack(
                         side="left", padx=(6, 12))
        tk.Label(toolbar, text="주변 BSS 신호 기반 점유 추정이며 스펙트럼 측정값이 아닙니다.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9)).pack(side="left")
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
        toolbar.pack(fill="x", pady=(8, 6))
        self.history_label = tk.Label(toolbar, text="현재 연결의 신호를 기록합니다",
                                      bg=BG, fg=MUTED, font=("TkDefaultFont", 9))
        self.history_label.pack(side="left")
        self.history_stats = tk.Label(toolbar, text="", bg=BG, fg=FG,
                                      font=("TkDefaultFont", 9, "bold"))
        self.history_stats.pack(side="right")
        panel = self._panel(self.history_tab, "RSSI / Noise 시간 추이")
        panel.pack(fill="both", expand=True)
        self.history_canvas = HistoryCanvas(panel)
        self.history_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _build_path(self):
        toolbar = tk.Frame(self.path_tab, bg=BG)
        toolbar.pack(fill="x", pady=(8, 7))
        tk.Label(toolbar, text="Wi-Fi 문제인지 공유기·ISP·DNS·HTTPS 문제인지 구간별로 확인합니다.",
                 bg=BG, fg=MUTED, font=("TkDefaultFont", 9)).pack(side="left")
        path_tab_button = self._button(
            toolbar, "진단 실행", self.run_path_test, primary=True)
        path_tab_button.pack(side="right")
        self.path_buttons.append((path_tab_button, "진단 실행"))
        cards = tk.Frame(self.path_tab, bg=BG)
        cards.pack(fill="x", pady=(0, 8))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1, uniform="path")
        self.path_cards = {}
        for index, (stage, title) in enumerate((
            ("gateway", "게이트웨이 / LAN"), ("internet", "외부 IP / ISP"),
            ("dns", "DNS 조회"), ("https", "HTTPS 연결"),
        )):
            card = MetricCard(cards, title)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 7) if index < 3 else 0)
            self.path_cards[stage] = card
        result_panel = self._panel(self.path_tab, "구간 판정")
        result_panel.pack(fill="both", expand=True)
        self.path_conclusion = tk.Label(
            result_panel, text="아직 경로 진단을 실행하지 않았습니다.", bg=CARD,
            fg=MUTED, anchor="nw", justify="left", wraplength=1000,
            font=("TkDefaultFont", 12, "bold"))
        self.path_conclusion.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    def _request_location(self):
        try:
            self._location_manager, _status = request_location_permission()
        except Exception:
            self._location_manager = None

    def interval_ms(self):
        try:
            return int(max(5.0, float(self.interval.get())) * 1000)
        except (tk.TclError, ValueError):
            return 10000

    def schedule_scan(self):
        if self.closed or self.paused:
            return
        if self.scan_after_id is not None:
            self.after_cancel(self.scan_after_id)
        self.scan_after_id = self.after(self.interval_ms(), self._scheduled_scan)

    def _scheduled_scan(self):
        self.scan_after_id = None
        self.scan_now()

    def scan_now(self):
        if self.scan_after_id is not None:
            try:
                self.after_cancel(self.scan_after_id)
            except tk.TclError:
                pass
            self.scan_after_id = None
        if self.closed or self.paused or self.scan_busy:
            return
        self.scan_busy = True
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
        threading.Thread(target=self._path_worker, daemon=True).start()

    def _set_path_busy(self, busy):
        self.path_busy = busy
        for button, normal_text in self.path_buttons:
            button.configure(
                text="진단 중…" if busy else normal_text,
                state="disabled" if busy else "normal")

    def _path_worker(self):
        try:
            self.events.put(("path_ok", run_path_diagnostics(), 0))
        except Exception as exc:
            self.events.put(("path_error", str(exc), 0))

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
                    elif kind == "path_ok":
                        self.path_result = payload
                        self.update_path_result()
                    elif kind == "path_error":
                        self.path_card.set("실패", str(payload), "critical")
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
                        self.schedule_scan()
                    if is_path:
                        self._set_path_busy(False)
        finally:
            if not self.closed:
                self.after(150, self._pump)

    def accept_snapshot(self, snapshot: ScanSnapshot, elapsed: float):
        snapshot.aps = assign_keys(snapshot.aps)
        self.snapshot = snapshot
        self.aps = snapshot.aps
        self.connection = snapshot.connection
        self.current_ap = self._merge_current_connection()
        if self.current_ap and not self.initial_channel_band_selected:
            self.channel_band.set(display_band(self.current_ap.band))
            self.initial_channel_band_selected = True
        observed_at = snapshot.timestamp or time.time()
        for ap in self.aps:
            self.history[ap.key].append((observed_at, ap.rssi, ap.noise))

        history_values = []
        if self.current_ap:
            history_values = [sample[1] for sample in self.history[self.current_ap.key]
                              if sample[1] is not None]
        self.findings = analyze_connection(
            self.current_ap, self.aps, history_values,
            source_reliable=source_is_reliable(snapshot))
        self._calculate_channel_scores()
        self.refresh_all()
        stamp = time.strftime("%H:%M:%S", time.localtime(snapshot.timestamp))
        self.scan_status.configure(
            text=f"{platform_label()} · {snapshot.source} · BSS {len(self.aps)}개 · "
                 f"{stamp} · {elapsed:.1f}초", fg=MUTED)
        self.show_banner(" · ".join(snapshot.warnings) if snapshot.warnings else "")

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
                         width_known=connection.width is not None)
            assign_keys([current])
            self.aps.append(current)
        if current:
            for ap in self.aps:
                ap.cur = ap is current
            if connection.rssi is not None:
                current.rssi = connection.rssi
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
        width = 20 if band == 2 else (80 if self.current_ap and self.current_ap.width >= 80 else 20)
        self.channel_scores = recommend_channels(self.aps, band, width)

    def refresh_all(self):
        self.update_cards()
        self.populate_findings()
        self.populate_recommendations()
        self.populate_networks()
        self.update_channel_tab()
        self.update_history()

    def update_cards(self):
        connection = self.connection
        current = self.current_ap
        if not current:
            self.connection_card.set("연결 없음", "Wi-Fi 연결 상태를 확인하세요", "critical")
            self.signal_card.set("–", "RSSI 미측정", "unknown")
            self.snr_card.set("–", "Noise 미측정", "unknown")
            self.link_card.set("–", "채널 정보 없음", "unknown")
            return
        self.connection_card.set(
            compact_text(current.ssid or "(숨김 SSID)"),
            f"{current.bssid or 'BSSID 미제공'} · {connection.interface or platform_label()}", "ok")
        rssi_state = assess_rssi(current.rssi)
        self.signal_card.set(f"{current.rssi} dBm", rssi_state.label, rssi_state.status)
        snr = current.snr if current.noise is not None and current.noise < 0 else None
        snr_state = assess_snr(snr)
        noise_detail = f"Noise {current.noise} dBm" if current.noise is not None else "Noise 미제공"
        self.snr_card.set(f"{snr} dB" if snr is not None else "미측정",
                          noise_detail, snr_state.status)
        rate = f"{connection.tx_rate:.0f} Mbps" if connection.tx_rate else "속도 미제공"
        self.link_card.set(
            f"ch {current.ch} · {display_width(current.width, current.width_known)}",
            f"{display_band(current.band)} · {connection.phy or current.phy or 'PHY 미제공'} · {rate}",
            "ok" if current.width_known else "info")

    def populate_findings(self):
        self.findings_tree.delete(*self.findings_tree.get_children())
        for index, finding in enumerate(self.findings):
            self.findings_tree.insert(
                "", "end", iid=f"finding-{index}", tags=(finding.severity,),
                values=(STATUS_TEXT.get(finding.severity, finding.severity),
                        finding.title, finding.evidence, finding.action))

    def populate_recommendations(self):
        self.recommend_tree.delete(*self.recommend_tree.get_children())
        for index, score in enumerate(self.channel_scores[:5]):
            pressure = "겹침 없음" if score.pressure_dbm is None else f"{score.pressure_dbm:.0f} dBm"
            suffix = " · DFS" if score.dfs else ""
            self.recommend_tree.insert(
                "", "end", iid=f"recommend-{index}",
                values=(f"{display_band(score.band)} ch{score.channel}/{score.width}",
                        pressure, score.reason + suffix))

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
                f"{ap.rssi} dBm",
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
        self.selected_key = selection[0] if selection else None
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
        self.pressure_chart.set_data(recommend_channels(self.aps, band, width))

    def update_history(self):
        if not hasattr(self, "history_canvas"):
            return
        ap = self.selected_ap() or self.current_ap
        if not ap:
            self.history_canvas.set_data("", [])
            self.history_stats.configure(text="")
            return
        samples = self.history.get(ap.key, ())
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
            text=result.conclusion, fg=STATUS_COLORS.get(result.severity, MUTED))
        self.path_card.set(STATUS_TEXT.get(result.severity, result.severity),
                           result.conclusion, result.severity)

    def show_banner(self, text):
        if text:
            self.banner.configure(text="⚠ " + text)
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
        else:
            self.pause_button.configure(text="일시정지")
            self.scan_now()

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
                    "rssi": ap.rssi, "noise": ap.noise, "snr": ap.snr,
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
        self.closed = True
        if self.scan_after_id is not None:
            try:
                self.after_cancel(self.scan_after_id)
            except tk.TclError:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
