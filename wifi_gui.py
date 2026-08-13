#!/usr/bin/env python3
"""Wi-Fi interference spectrum viewer and AP status dashboard.

  · 각 AP를 실제 점유 주파수 폭만큼 벌어진 곡선으로 그린다. 곡선이 겹치는
    구간이 곧 서로 간섭하는 구간이다.
  · 오른쪽 표에서 AP별 SSID / BSSID / 채널 / 대역폭 / 신호 / SNR / 겹침 수를 본다.
  · 행을 클릭하면 그래프에서 강조되고, 아래에 신호 추이가 그려진다.

The operating-system specific scanner is selected by wifi_platform.
"""

import collections
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from wifi_core import (assign_keys, channel_to_frequency, interference_level,
                       interference_margin_db, relevant_interferers)
from wifi_platform import (platform_label, request_location_permission,
                           scan_source, scan_wifi)

SCAN, SOURCE = scan_wifi, scan_source()


# ── 위치 권한 ───────────────────────────────────────────────────────
AUTH_NAMES = {0: "미결정", 1: "제한됨", 2: "거부됨", 3: "허용", 4: "허용"}


# ── 색 ──────────────────────────────────────────────────────────────
BG, PANEL, FG, GRID, DIM = "#12151a", "#171b21", "#e6eaee", "#232a33", "#79838f"
OKC, WARN, BAD = "#5fd39a", "#ffd166", "#ff7a5c"
PALETTE = ["#4ea3ff", "#ff7a5c", "#5fd39a", "#ffd166", "#c792ea", "#ff8ec7",
           "#7ee8e8", "#a8d24d", "#ff9f43", "#7f9cf5", "#e5766a", "#6bc5a8"]
RSSI_TOP, RSSI_BOT = -40, -95

BANDS = {
    5: (5160, 5845, [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116,
                     120, 124, 128, 132, 136, 140, 144, 149, 153, 157, 161]),
    2: (2400, 2500, list(range(1, 14))),
}


def color_of(key):
    return PALETTE[hash(key) % len(PALETTE)]


# ── 스펙트럼 ────────────────────────────────────────────────────────
class Spectrum(tk.Canvas):
    def __init__(self, master, band, height):
        super().__init__(master, bg=BG, height=height, highlightthickness=0)
        self.band, self.aps, self.sel = band, [], None
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, aps, sel):
        self.aps = [a for a in aps if a.band == self.band]
        self.sel = sel
        self.redraw()

    def _x(self, mhz):
        lo, hi, _ = BANDS[self.band]
        return self.pl + (mhz - lo) / (hi - lo) * (self.w - self.pl - 14)

    def _y(self, dbm):
        d = max(RSSI_BOT, min(RSSI_TOP, dbm))
        return self.pt + (RSSI_TOP - d) / (RSSI_TOP - RSSI_BOT) * (self.h - self.pt - self.pb)

    @staticmethod
    def _curve(x0, x1, ypeak, ybase, n=48, pointed=False):
        """Return a channel-shape polyline.

        2.4 GHz uses a single peak at the channel centre. Wider 5 GHz ranges
        retain a plateau because the primary channel is not always the centre
        of the bonded range reported by the scan API.
        """
        import math
        w = x1 - x0
        sh = 0.22                      # 어깨가 차지하는 비율
        pts = []
        for i in range(n + 1):
            t = i / n
            if pointed:
                f = 0.5 - 0.5 * math.cos(2 * math.pi * t)
            elif t < sh:
                f = 0.5 - 0.5 * math.cos(math.pi * (t / sh))
            elif t > 1 - sh:
                f = 0.5 - 0.5 * math.cos(math.pi * ((1 - t) / sh))
            else:
                f = 1.0
            pts += [x0 + w * t, ybase + (ypeak - ybase) * f]
        return pts

    @staticmethod
    def _overlaps(first, second):
        return not (first[2] < second[0] or first[0] > second[2]
                    or first[3] < second[1] or first[1] > second[3])

    def redraw(self):
        self.delete("all")
        self.w, self.h = self.winfo_width(), self.winfo_height()
        if self.w < 80 or self.h < 70:
            return
        self.pl, self.pt, self.pb = 46, 20, 42
        lo, hi, chans = BANDS[self.band]

        self.create_text(10, 6, text="신호 (dBm)", anchor="nw", fill=DIM,
                         font=("SF Mono", 10, "bold"))
        for d in range(-90, -20, 10):
            y = self._y(d)
            self.create_line(self.pl, y, self.w - 14, y, fill=GRID)
            self.create_text(self.pl - 6, y, text=str(d), anchor="e", fill=DIM,
                             font=("SF Mono", 9))
        for ch in chans:
            f = channel_to_frequency(ch, self.band)
            if not (lo <= f <= hi):
                continue
            x = self._x(f)
            self.create_line(x, self.pt, x, self.h - self.pb, fill=GRID, dash=(1, 3))
            self.create_text(x, self.h - self.pb + 4, text=str(ch), anchor="n",
                             fill=DIM, font=("SF Mono", 9))
        self.create_text((self.pl + self.w - 14) / 2, self.h - 5, text="채널 번호",
                         anchor="s", fill=DIM, font=("SF Mono", 10, "bold"))

        base = self._y(RSSI_BOT)
        selected = next((ap for ap in self.aps if ap.key == self.sel), None)
        if selected is None:
            selected = next((ap for ap in self.aps if ap.cur), None)
        interferers = relevant_interferers(selected, self.aps) if selected else []
        rival_keys = {ap.key for _, ap in interferers}
        if selected:
            selected_start, selected_end = selected.rng
            marker_top = base - 7
            self.create_rectangle(self._x(selected_start), marker_top, self._x(selected_end), base,
                                  fill=OKC, outline="", tags=("selected-range",))
            for _, ap in interferers:
                start = max(selected_start, ap.rng[0])
                end = min(selected_end, ap.rng[1])
                self.create_rectangle(self._x(start), marker_top, self._x(end), base,
                                      fill=BAD, outline="", tags=("overlap-range",))
            self.create_text(self.w - 14, 6, text=f"겹침 {len(interferers)} AP",
                             anchor="ne", fill=BAD if interferers else OKC,
                             font=("SF Mono", 10, "bold"))

        labels = []
        for ap in sorted(self.aps, key=lambda a: a.rssi):
            c = color_of(ap.key)
            f0, f1 = ap.rng
            x0, x1 = self._x(f0), self._x(f1)
            yp = self._y(ap.rssi)
            # A selection from another band must not dim every AP in this chart.
            focus = selected is None or self.sel == ap.key or ap.key in rival_keys
            curve = self._curve(x0, x1, yp, base, pointed=self.band == 2)
            self.create_line(curve, fill=c,
                             width=3 if ap.cur else (2 if focus else 1))
            if ap.cur or ap.rssi > -78 or ap.key in rival_keys:
                nm = ap.ssid or "(권한필요)"
                nm = nm if len(nm) <= 20 else nm[:19] + "…"
                labels.append(((x0 + x1) / 2, yp - 9, ("▶ " + nm) if ap.cur else nm,
                               c, "bold" if ap.cur else "normal"))

        # Keep labels legible when several APs share the same channel and signal level.
        placed = []
        for x, y, text, color, weight in sorted(labels, key=lambda item: (item[0], -item[1])):
            item = self.create_text(x, y, text=text, anchor="s", fill=color,
                                    font=("SF Mono", 10, weight), tags=("label",))
            bounds = self.bbox(item)
            while bounds and any(self._overlaps(bounds, other) for other in placed):
                self.move(item, 0, -14)
                bounds = self.bbox(item)
            if bounds and bounds[1] < self.pt:
                self.delete(item)
            elif bounds:
                placed.append(bounds)


# ── 신호 추이 ───────────────────────────────────────────────────────
class Trend(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, bg=PANEL, height=92, highlightthickness=0)
        self.hist, self.color = [], OKC
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, hist, color):
        self.hist, self.color = list(hist), color
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 30:
            return
        if not self.hist:
            self.create_text(w / 2, h / 2, text="AP를 선택하세요", fill=DIM,
                             font=("SF Mono", 10))
            return
        pad = 6
        for d in (-40, -60, -80):
            y = pad + (RSSI_TOP - d) / (RSSI_TOP - RSSI_BOT) * (h - 2 * pad)
            self.create_line(pad, y, w - pad, y, fill=GRID)
            self.create_text(w - pad, y - 6, text=str(d), anchor="e", fill=DIM,
                             font=("SF Mono", 8))
        n = len(self.hist)
        step = (w - 2 * pad) / max(1, n - 1) if n > 1 else 0
        pts = []
        for i, v in enumerate(self.hist):
            x = pad + i * step
            d = max(RSSI_BOT, min(RSSI_TOP, v))
            y = pad + (RSSI_TOP - d) / (RSSI_TOP - RSSI_BOT) * (h - 2 * pad)
            pts += [x, y]
        if n > 1:
            self.create_line(pts, fill=self.color, width=2, smooth=True)
        self.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3,
                         fill=self.color, outline="")
        self.create_text(pad, pad, anchor="nw",
                         text=f"최근 {n}회   현재 {self.hist[-1]}dBm   "
                              f"최소 {min(self.hist)}  최대 {max(self.hist)}",
                         fill=DIM, font=("SF Mono", 9))


# ── 앱 ──────────────────────────────────────────────────────────────
class App(tk.Tk):
    HIST = 60

    def __init__(self):
        super().__init__()
        self.title("WiFi Analyzer")
        self.geometry("1440x900")
        self.minsize(980, 680)
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.aps, self.view_aps, self.rows, self.sel = [], [], [], None
        self.expanded_spectra = []
        self.last_visual_signature = None
        self.next_scan_id = None
        self.busy = False
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=self.HIST))
        self.seen = {}
        self.interval = tk.DoubleVar(value=0.5)
        self.band_filter = tk.StringVar(value="모든 대역")
        self.text_filter = tk.StringVar()
        self.diagnostic_only = tk.BooleanVar(value=False)
        self.show_hidden = tk.BooleanVar(value=False)
        self.sort_col, self.sort_reverse = "rssi", True

        self._locmgr, _ = request_location_permission()
        self._style()
        self._header()
        self._filters()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=(1, 4))
        workspace = tk.Frame(body, bg=BG)
        workspace.pack(fill="both", expand=True)
        self._network_table(workspace, compact=True)
        analysis = tk.Frame(workspace, bg=BG)
        analysis.pack(side="left", fill="both", expand=True)
        self._charts(analysis)

        self.kick()
        self.after(200, self.pump)

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("T.Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=FG, borderwidth=0, rowheight=26,
                    font=("SF Mono", 11))
        s.configure("T.Treeview.Heading", background="#1e242c", foreground=DIM,
                    borderwidth=0, font=("SF Mono", 10, "bold"))
        s.map("T.Treeview", background=[("selected", "#2a3340")])
        s.configure("TCombobox", fieldbackground="#1e242c", background="#263243",
                    foreground=FG, arrowcolor=FG, borderwidth=0,
                    selectbackground="#3d78b8", selectforeground="#ffffff")
        s.map("TCombobox",
              fieldbackground=[("readonly", "#1e242c")],
              foreground=[("readonly", FG)],
              background=[("readonly", "#263243")],
              selectbackground=[("readonly", "#3d78b8")],
              selectforeground=[("readonly", "#ffffff")])
        # The drop-down list is a Tk Listbox, not a ttk widget, on Linux.
        self.option_add("*TCombobox*Listbox.background", "#1e242c")
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", "#3d78b8")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _header(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=10, pady=(3, 1))
        tk.Label(bar, text="WiFi Analyzer", bg=BG, fg=FG, anchor="w",
                 font=("SF Pro Display", 17, "bold")).pack(side="left")
        self.lbl = tk.Label(bar, text="스캔 중…", bg=BG, fg=DIM, anchor="w",
                            font=("SF Mono", 10))
        self.lbl.pack(side="left", padx=(8, 0))
        self.header_filters = tk.Frame(bar, bg=BG)
        self.header_filters.pack(side="right", padx=(0, 6))
        r = tk.Frame(bar, bg=BG)
        r.pack(side="right")
        tk.Label(r, text="주기", bg=BG, fg=DIM,
                 font=("SF Mono", 9)).pack(side="left", padx=(0, 3))
        ttk.Spinbox(r, from_=0.5, to=60, increment=0.5, width=4,
                    textvariable=self.interval).pack(side="left")
        tk.Button(r, text="새로 고침", command=self.kick, bg="#263243", fg=FG,
                  activebackground="#33445b", activeforeground=FG,
                  highlightthickness=0, borderwidth=0, padx=8).pack(side="left", padx=(6, 0))
        self.warn = tk.Label(self, text="", bg=BG, fg=WARN, anchor="w",
                             justify="left", font=("SF Mono", 9))
        self.interval.trace_add("write", lambda *_: self.reschedule_scan())

    def _filters(self):
        bar = self.header_filters
        ttk.Combobox(bar, textvariable=self.band_filter, state="readonly", width=9,
                     values=("모든 대역", "2.4 GHz", "5 GHz")).pack(side="left")
        search = tk.Entry(bar, textvariable=self.text_filter, bg="#1e242c", fg=FG,
                          insertbackground=FG, relief="flat",
                          font=("SF Mono", 10), width=18)
        search.pack(side="left", padx=(6, 0), ipady=3)
        tk.Checkbutton(bar, text="진단 AP만", variable=self.diagnostic_only,
                       command=self.apply_filter, bg=BG, fg=FG, selectcolor="#1e242c",
                       activebackground=BG, activeforeground=FG,
                       font=("SF Mono", 9)).pack(side="left", padx=(8, 0))
        tk.Checkbutton(bar, text="숨김 SSID", variable=self.show_hidden,
                       command=self.apply_filter, bg=BG, fg=FG, selectcolor="#1e242c",
                       activebackground=BG, activeforeground=FG,
                       font=("SF Mono", 9)).pack(side="left", padx=(8, 0))
        self.band_filter.trace_add("write", lambda *_: self.apply_filter())
        self.text_filter.trace_add("write", lambda *_: self.apply_filter())

    def _network_table(self, body, compact=False):
        self.compact_table = compact
        frame = tk.Frame(body, bg=BG, width=380 if compact else 0)
        frame.pack(side="left" if compact else "top", fill="y" if compact else "both",
                   expand=not compact, padx=(0, 6) if compact else 0)
        if compact:
            frame.pack_propagate(False)
        self.count_label = tk.Label(frame, text="발견한 네트워크", bg=BG, fg=DIM,
                                    anchor="w", font=("SF Mono", 10))
        self.count_label.pack(fill="x", pady=(0, 1))
        cols = ("ssid", "ch", "rssi", "ov") if compact else (
            "ssid", "bssid", "band", "ch", "bw", "rssi", "snr", "sec", "ov")
        table = tk.Frame(frame, bg=PANEL)
        table.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(table, columns=cols, show="headings",
                               style="T.Treeview", selectmode="none", height=10)
        specs = (("ssid", "네트워크", 190, "w"), ("ch", "채널", 48, "e"),
                 ("rssi", "신호", 72, "e"), ("ov", "간섭", 46, "e")) if compact else (
            ("ssid", "네트워크 이름", 260, "w"), ("bssid", "BSSID / MAC", 160, "w"),
            ("band", "대역", 62, "center"), ("ch", "채널", 54, "e"),
            ("bw", "폭", 62, "e"), ("rssi", "신호", 76, "e"),
            ("snr", "SNR", 62, "e"), ("sec", "보안", 120, "w"),
            ("ov", "간섭", 56, "e"),
        )
        for c, title, width, anchor in specs:
            self.tv.heading(c, text=title, command=lambda col=c: self.sort_by(col))
            self.tv.column(c, width=width, anchor=anchor, stretch=(c == "ssid"))
        self.tv.tag_configure("cur", foreground=OKC)
        self.tv.tag_configure("strong", foreground=FG)
        self.tv.tag_configure("weak", foreground=DIM)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=scroll.set)
        self.tv.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", self.open_detail_popup)

    def _charts(self, body):
        charts = tk.Frame(body, bg=BG)
        charts.pack(fill="both", expand=True, pady=(3, 0))
        self.chart_panes = {}
        for band, title, attr in ((2, "2.4 GHz · 채널 겹침", "sp2"),
                                 (5, "5 GHz · 채널 겹침", "sp5")):
            pane = tk.Frame(charts, bg=PANEL)
            self.chart_panes[band] = pane
            heading = tk.Frame(pane, bg=PANEL)
            heading.pack(fill="x", padx=7, pady=(3, 0))
            tk.Label(heading, text=title, bg=PANEL, fg=DIM, anchor="w",
                     font=("SF Mono", 10, "bold")).pack(side="left")
            tk.Button(heading, text="확대", command=lambda b=band, t=title: self.open_spectrum(b, t),
                      bg="#263243", fg=FG, activebackground="#33445b", activeforeground=FG,
                      highlightthickness=0, borderwidth=0, padx=8).pack(side="right")
            canvas = Spectrum(pane, band, 250)
            canvas.pack(fill="both", expand=True, padx=2, pady=(0, 2))
            setattr(self, attr, canvas)
        self.arrange_charts()

    # 스캔 루프
    def refresh_interval_ms(self):
        try:
            return int(max(0.5, self.interval.get()) * 1000)
        except tk.TclError:
            return 500

    def schedule_scan(self, delay_ms=None):
        if self.next_scan_id is not None:
            self.after_cancel(self.next_scan_id)
        delay_ms = self.refresh_interval_ms() if delay_ms is None else delay_ms
        self.next_scan_id = self.after(delay_ms, self.kick)

    def reschedule_scan(self):
        if self.busy:
            return
        self.schedule_scan(0)

    def kick(self):
        self.next_scan_id = None
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        t0 = time.time()
        try:
            self.q.put(("ok", assign_keys(SCAN()), time.time() - t0))
        except Exception as e:
            self.q.put(("err", e, time.time() - t0))

    def pump(self):
        try:
            while True:
                kind, payload, dt = self.q.get_nowait()
                self.busy = False
                if kind == "ok":
                    self.aps = payload
                    self.render(dt)
                else:
                    self.lbl.config(text=f"스캔 실패: {payload}")
                self.schedule_scan()
        except queue.Empty:
            pass
        self.after(200, self.pump)

    # 표시
    def rivals(self, ap):
        return [other for _, other in relevant_interferers(ap, self.aps)]

    def filtered_aps(self):
        query = self.text_filter.get().strip().casefold()
        band = {"2.4 GHz": 2, "5 GHz": 5}.get(self.band_filter.get())
        current = next((ap for ap in self.aps if ap.cur), None)
        diagnostic_keys = {current.key} if current else set()
        if current:
            diagnostic_keys.update(ap.key for _, ap in relevant_interferers(current, self.aps))
        apply_diagnostic_filter = (self.diagnostic_only.get() and current is not None
                                   and (band is None or band == current.band))
        return [
            ap for ap in self.aps
            if (not apply_diagnostic_filter or ap.key in diagnostic_keys)
            and (self.show_hidden.get() or bool(ap.ssid) or ap.cur)
            if (band is None or ap.band == band)
            and (not query or query in (ap.ssid or "").casefold()
                 or query in (ap.bssid or "").casefold())
        ]

    def sort_by(self, column):
        if self.sort_col == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = column
            self.sort_reverse = column in {"rssi", "snr", "ov"}
        self.populate_table()

    def _sort_value(self, ap):
        values = {
            "ssid": (ap.ssid or "").casefold(),
            "bssid": ap.bssid or "",
            "band": ap.band,
            "ch": ap.ch,
            "bw": ap.width,
            "rssi": ap.rssi,
            "snr": ap.snr,
            "sec": ap.sec or "",
            "ov": len(self.rivals(ap)),
        }
        return values[self.sort_col]

    def apply_filter(self):
        if not hasattr(self, "tv"):
            return
        self.view_aps = self.filtered_aps()
        self.arrange_charts()
        self.refresh_spectra()
        self.populate_table()

    def refresh_spectra(self):
        self.sp2.set_data(self.view_aps, self.sel)
        self.sp5.set_data(self.view_aps, self.sel)
        active = []
        for window, spectrum in self.expanded_spectra:
            if window.winfo_exists():
                spectrum.set_data(self.view_aps, self.sel)
                active.append((window, spectrum))
        self.expanded_spectra = active

    def arrange_charts(self):
        selected_band = {"2.4 GHz": 2, "5 GHz": 5}.get(self.band_filter.get())
        visible_bands = [selected_band] if selected_band else [2, 5]
        for pane in self.chart_panes.values():
            pane.pack_forget()
        for index, band in enumerate(visible_bands):
            self.chart_panes[band].pack(fill="both", expand=True,
                                        pady=(0, 5) if index == 0 and len(visible_bands) > 1
                                        else (5, 0) if index else 0)

    def open_spectrum(self, band, title):
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(bg=BG)
        window.attributes("-fullscreen", True)
        header = tk.Frame(window, bg=BG)
        header.pack(fill="x", padx=20, pady=14)
        tk.Label(header, text=title, bg=BG, fg=FG,
                 font=("SF Pro Display", 18, "bold")).pack(side="left")
        tk.Button(header, text="닫기", command=window.destroy, bg="#263243", fg=FG,
                  activebackground="#33445b", activeforeground=FG,
                  highlightthickness=0, borderwidth=0, padx=12).pack(side="right")
        spectrum = Spectrum(window, band, 500)
        spectrum.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.expanded_spectra.append((window, spectrum))
        spectrum.set_data(self.view_aps, self.sel)
        window.bind("<Escape>", lambda _event: window.destroy())

    def populate_table(self):
        if not hasattr(self, "tv"):
            return
        self.tv.delete(*self.tv.get_children())
        self.rows = sorted(self.view_aps, key=self._sort_value, reverse=self.sort_reverse)
        for a in self.rows:
            tag = "cur" if a.cur else ("strong" if a.rssi > -70 else "weak")
            full_values = (
                ("● " if a.cur else "") + (a.ssid or "(숨김 SSID)"),
                a.bssid or "미제공",
                f"{a.band} GHz", a.ch, f"{a.width} MHz", f"{a.rssi} dBm",
                f"{a.snr} dB" if a.snr else "–", a.sec or "–", len(self.rivals(a)))
            values = (full_values[0], full_values[3], full_values[5], full_values[8]) \
                if self.compact_table else full_values
            self.tv.insert("", "end", iid=a.key, tags=(tag,), values=values)
        total = len(self.aps)
        self.count_label.config(
            text=f"발견한 네트워크  {len(self.view_aps)}개"
                 + (f" / 전체 {total}개" if len(self.view_aps) != total else ""))

    def render(self, dt):
        now = time.time()
        for a in self.aps:
            self.hist[a.key].append(a.rssi)
            self.seen[a.key] = now

        signature = tuple(sorted(
            (a.key, a.ssid, a.bssid, a.ch, a.width, a.band, a.rssi, a.noise, a.cur)
            for a in self.aps
        ))
        if signature == self.last_visual_signature:
            return
        self.last_visual_signature = signature

        cur = next((a for a in self.aps if a.cur), None)
        head = "연결 정보 없음"
        if cur:
            interferers = relevant_interferers(cur, self.aps)
            ov = [ap for _, ap in interferers]
            margin = interference_margin_db(cur, interferers)
            diagnosis = interference_level(margin)
            head = (f"ch{cur.ch} · 간섭 {diagnosis}"
                    + (f" · C/I {margin:+.0f}dB" if margin is not None else ""))
        self.lbl.config(text=head)

        hidden = sum(1 for a in self.aps if not a.bssid or not a.ssid)
        warning = ""
        if hidden and platform_label() == "macOS":
            st = self._locmgr.authorizationStatus() if self._locmgr else None
            stx = AUTH_NAMES.get(st, "확인불가")
            if st in (0, 1, 2):
                warning = (
                    f"⚠ SSID/BSSID {hidden}개가 macOS에서 가려짐 — 위치 권한 [{stx}]. "
                    "WiFiAnalyzer.app으로 실행한 뒤 위치 접근을 허용하세요.")
            else:
                warning = (
                    f"ℹ SSID/BSSID {hidden}개는 숨김 네트워크이거나 macOS 스캔 API가 "
                    f"제공하지 않은 항목입니다 — 위치 권한 [{stx}].")
        if warning:
            self.warn.config(text=warning)
            if not self.warn.winfo_ismapped():
                self.warn.pack(fill="x", padx=10, pady=(0, 1))
        else:
            self.warn.pack_forget()

        self.apply_filter()

    def open_detail_popup(self, event):
        key = self.tv.identify_row(event.y)
        ap = next((item for item in self.aps if item.key == key), None)
        if not ap:
            return
        ov = self.rivals(ap)
        co = [o for o in ov if o.ch == ap.ch]
        adj = [o for o in ov if o.ch != ap.ch]
        f0, f1 = ap.rng
        worst = max((o.rssi for o in ov), default=None)
        noise_text = f"noise {ap.noise}dBm" if ap.noise is not None else "noise 미제공"
        window = tk.Toplevel(self)
        window.title(ap.ssid or "숨김 네트워크")
        window.configure(bg=PANEL)
        window.geometry("480x260")
        window.resizable(False, False)
        window.transient(self)
        body = tk.Frame(window, bg=PANEL)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        heading = tk.Frame(body, bg=PANEL)
        heading.pack(fill="x")
        tk.Label(heading, text=ap.ssid or "(숨김 SSID)", bg=PANEL, fg=FG, anchor="w",
                 font=("SF Pro Display", 15, "bold")).pack(side="left")
        tk.Button(heading, text="닫기", command=window.destroy, bg="#263243", fg=FG,
                  activebackground="#33445b", activeforeground=FG,
                  highlightthickness=0, borderwidth=0, padx=8).pack(side="right")
        tk.Label(body, text=ap.bssid or "BSSID 미제공", bg=PANEL, fg=DIM, anchor="w",
                 font=("SF Mono", 9)).pack(fill="x", pady=(1, 6))
        detail = (f"{ap.band} GHz · ch{ap.ch} · {ap.width}MHz · {ap.rssi}dBm · {noise_text}\n"
                  f"동일 {len(co)} · 부분 {len(adj)}"
                  + (f" · 최강 {worst}dBm" if worst is not None else ""))
        tk.Label(body, text=detail, bg=PANEL, fg=FG, anchor="w", justify="left",
                 font=("SF Mono", 10)).pack(fill="x")
        trend = Trend(body)
        trend.configure(height=76)
        trend.pack(fill="both", expand=True, pady=(8, 0))
        trend.set_data(self.hist[ap.key], color_of(ap.key))
        window.bind("<Escape>", lambda _event: window.destroy())


if __name__ == "__main__":
    App().mainloop()
