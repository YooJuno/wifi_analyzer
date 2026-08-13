#!/usr/bin/env python3
"""Wi-Fi 간섭 스펙트럼 뷰어 + AP별 상태 (macOS)

  · 각 AP를 실제 점유 주파수 폭만큼 벌어진 곡선으로 그린다. 곡선이 겹치는
    구간이 곧 서로 간섭하는 구간이다.
  · 오른쪽 표에서 AP별 SSID / BSSID / 채널 / 대역폭 / 신호 / SNR / 겹침 수를 본다.
  · 행을 클릭하면 그래프에서 강조되고, 아래에 신호 추이가 그려진다.

필요:
  pip3 install pyobjc-framework-CoreWLAN

SSID·BSSID가 "권한필요"로 나오면 macOS 14+ 의 위치 서비스 제약이다.
  시스템 설정 → 개인정보 보호 및 보안 → 위치 서비스 → 실행한 터미널 앱을 켠 뒤 재실행
  (VS Code 에서 실행했다면 Code, iTerm 이면 iTerm 을 켜야 한다)

실행:
  python3 wifi_gui.py
"""

import collections
import json
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

# ── 채널 본딩 ───────────────────────────────────────────────────────
BLOCKS = {
    40: [(36, 40), (44, 48), (52, 56), (60, 64), (100, 104), (108, 112),
         (116, 120), (124, 128), (132, 136), (140, 144), (149, 153), (157, 161)],
    80: [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)],
    160: [(36, 64), (100, 128)],
}


def ch_to_freq(ch):
    return 2407 + 5 * ch if ch <= 14 else 5000 + 5 * ch


def occupied(ch, width):
    if ch <= 14:
        c = ch_to_freq(ch)
        return c - 10, c + 10
    for lo, hi in BLOCKS.get(width, []):
        if lo <= ch <= hi:
            return ch_to_freq(lo) - 10, ch_to_freq(hi) + 10
    c = ch_to_freq(ch)
    return c - 10, c + 10


def overlap(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


# ── AP ──────────────────────────────────────────────────────────────
class AP:
    __slots__ = ("ssid", "bssid", "ch", "width", "band", "rssi", "noise",
                 "sec", "phy", "cur", "key")

    def __init__(self, ssid, bssid, ch, width, band, rssi, noise,
                 sec="", phy="", cur=False):
        self.ssid = ssid
        self.bssid = bssid
        self.ch, self.width, self.band = ch, width, band
        self.rssi, self.noise = rssi, noise
        self.sec, self.phy, self.cur = sec, phy, cur
        self.key = bssid or f"{ssid}|{band}|{ch}|{width}"

    @property
    def rng(self):
        return occupied(self.ch, self.width)

    @property
    def snr(self):
        return self.rssi - self.noise if self.noise < 0 else 0


def assign_keys(aps):
    """행 식별자를 유일하게 만든다.

    BSSID가 있으면 그것이 물리 라디오 식별자지만, 스캔 API가 같은 BSSID를
    중복 반환할 수도 있다. 위치 권한이 없어 BSSID가 비는 경우에도 같은
    (ssid, band, ch, width)의 AP가 여럿 있을 수 있다. 모든 충돌 그룹에 번호를
    붙여 Tkinter Treeview의 행 ID가 항상 유일하도록 한다.
    """
    groups = {}
    for a in aps:
        identity = ("bssid", a.bssid) if a.bssid else (
            "network", a.ssid, a.band, a.ch, a.width)
        groups.setdefault(identity, []).append(a)
    for identity, g in groups.items():
        g.sort(key=lambda x: -x.rssi)
        base = g[0].bssid if identity[0] == "bssid" else "|".join(map(str, identity[1:]))
        if len(g) == 1:
            g[0].key = base
            continue
        for i, a in enumerate(g):
            a.key = f"{base}#{i}"
    return aps


# ── 스캔: CoreWLAN ──────────────────────────────────────────────────
WIDTH_MAP = {0: 20, 1: 20, 2: 40, 3: 40, 4: 80, 5: 160}


def scan_corewlan():
    import CoreWLAN
    itf = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
    nets, _ = itf.scanForNetworksWithName_error_(None, None)
    cur_b = itf.bssid()
    out = []
    for n in (nets or []):
        wc = n.wlanChannel()
        if wc is None:
            continue
        ch = int(wc.channelNumber())
        w = WIDTH_MAP.get(int(wc.channelWidth()), 20)
        b = n.bssid()
        s = n.ssid()
        out.append(AP(s if s else None, b, ch, w, 2 if ch <= 14 else 5,
                      int(n.rssiValue()), int(n.noiseMeasurement()),
                      "", "", bool(b) and b == cur_b))
    return out


# ── 스캔: system_profiler 대체 경로 ─────────────────────────────────
CH_RE = re.compile(r"(\d+)\s*\((\d)GHz(?:,\s*(\d+)MHz)?\)")
SN_RE = re.compile(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm")


def _entry(name, e, cur=False):
    m = CH_RE.search(str(e.get("spairport_network_channel", "")))
    if not m:
        return None
    sn = SN_RE.search(str(e.get("spairport_signal_noise", "")))
    rssi, noise = (int(sn.group(1)), int(sn.group(2))) if sn else (-99, 0)
    nm = None if str(name).startswith("<redacted") else name
    return AP(nm, None, int(m.group(1)), int(m.group(3)) if m.group(3) else 20,
              int(m.group(2)), rssi, noise,
              str(e.get("spairport_security_mode", "")).replace("spairport_security_mode_", ""),
              str(e.get("spairport_network_phymode", "")), cur)


def scan_sp():
    r = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                       capture_output=True, text=True, timeout=45)
    aps = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "spairport_current_network_information" and isinstance(v, dict):
                    a = _entry(v.get("_name", "?"), v, True)
                    if a:
                        aps.append(a)
                elif k == "spairport_airport_other_local_wireless_networks" and isinstance(v, list):
                    for it in v:
                        a = _entry(it.get("_name", "?"), it)
                        if a:
                            aps.append(a)
                else:
                    walk(v)
        elif isinstance(o, list):
            for i in o:
                walk(i)

    walk(json.loads(r.stdout))
    return aps


try:
    import CoreWLAN  # noqa: F401
    SCAN, SOURCE = scan_corewlan, "CoreWLAN"
except ImportError:
    SCAN, SOURCE = scan_sp, "system_profiler"


# ── 위치 권한 ───────────────────────────────────────────────────────
AUTH_NAMES = {0: "미결정", 1: "제한됨", 2: "거부됨", 3: "허용", 4: "허용"}


def request_location():
    """SSID/BSSID 를 보려면 위치 권한이 필요하다.

    macOS 는 번들 앱에만 권한을 준다. WiFiScan.app 으로 실행하면 여기서 시스템
    프롬프트가 뜨고, 맨 python3 로 실행하면 요청이 무시되어 계속 '미결정'이다.
    반환한 manager 는 GC 되지 않도록 붙잡아 둬야 한다.
    """
    try:
        from CoreLocation import CLLocationManager
    except ImportError:
        return None, None
    mgr = CLLocationManager.alloc().init()
    st = mgr.authorizationStatus()
    if st == 0:
        mgr.requestWhenInUseAuthorization()
    return mgr, st


# ── 색 ──────────────────────────────────────────────────────────────
BG, PANEL, FG, GRID, DIM = "#12151a", "#171b21", "#e6eaee", "#232a33", "#79838f"
OKC, WARN, BAD = "#5fd39a", "#ffd166", "#ff7a5c"
PALETTE = ["#4ea3ff", "#ff7a5c", "#5fd39a", "#ffd166", "#c792ea", "#ff8ec7",
           "#7ee8e8", "#a8d24d", "#ff9f43", "#7f9cf5", "#e5766a", "#6bc5a8"]
RSSI_TOP, RSSI_BOT = -25, -95

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
    def _curve(x0, x1, ypeak, ybase, n=48):
        """가장자리는 바닥, 가운데는 평평한 종 모양 폴리라인을 만든다.

        어깨는 raised-cosine 으로 부드럽게 올린다. 채워진 면 없이 선만 그리므로
        여러 AP가 겹쳐도 각 곡선이 그대로 보인다.
        """
        import math
        w = x1 - x0
        sh = 0.22                      # 어깨가 차지하는 비율
        pts = []
        for i in range(n + 1):
            t = i / n
            if t < sh:
                f = 0.5 - 0.5 * math.cos(math.pi * (t / sh))
            elif t > 1 - sh:
                f = 0.5 - 0.5 * math.cos(math.pi * ((1 - t) / sh))
            else:
                f = 1.0
            pts += [x0 + w * t, ybase + (ypeak - ybase) * f]
        return pts

    def redraw(self):
        self.delete("all")
        self.w, self.h = self.winfo_width(), self.winfo_height()
        if self.w < 80 or self.h < 70:
            return
        self.pl, self.pt, self.pb = 46, 20, 26
        lo, hi, chans = BANDS[self.band]

        self.create_text(10, 6, text=f"{self.band} GHz", anchor="nw", fill=DIM,
                         font=("SF Mono", 10, "bold"))
        for d in range(-90, -20, 10):
            y = self._y(d)
            self.create_line(self.pl, y, self.w - 14, y, fill=GRID)
            self.create_text(self.pl - 6, y, text=str(d), anchor="e", fill=DIM,
                             font=("SF Mono", 9))
        for ch in chans:
            f = ch_to_freq(ch)
            if not (lo <= f <= hi):
                continue
            x = self._x(f)
            self.create_line(x, self.pt, x, self.h - self.pb, fill=GRID, dash=(1, 3))
            self.create_text(x, self.h - self.pb + 4, text=str(ch), anchor="n",
                             fill=DIM, font=("SF Mono", 9))

        base = self._y(RSSI_BOT)
        for ap in sorted(self.aps, key=lambda a: a.rssi):
            c = color_of(ap.key)
            f0, f1 = ap.rng
            x0, x1 = self._x(f0), self._x(f1)
            yp = self._y(ap.rssi)
            focus = self.sel is None or self.sel == ap.key
            self.create_line(self._curve(x0, x1, yp, base), fill=c,
                             width=3 if ap.cur else (2 if focus else 1))
            if focus and (ap.cur or ap.rssi > -78 or self.sel == ap.key):
                nm = ap.ssid or "(권한필요)"
                nm = nm if len(nm) <= 20 else nm[:19] + "…"
                self.create_text((x0 + x1) / 2, yp - 9,
                                 text=("▶ " + nm) if ap.cur else nm, anchor="s",
                                 fill=c, font=("SF Mono", 10,
                                               "bold" if ap.cur else "normal"))


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
        self.busy = False
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=self.HIST))
        self.seen = {}
        self.interval = tk.DoubleVar(value=5.0)
        self.band_filter = tk.StringVar(value="모든 대역")
        self.text_filter = tk.StringVar()
        self.sort_col, self.sort_reverse = "rssi", True

        self._locmgr, _ = request_location()
        self._style()
        self._header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self._filters(body)
        self._network_table(body)
        self._charts(body)
        self._detail(body)

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
        s.configure("TCombobox", fieldbackground="#1e242c", background="#1e242c",
                    foreground=FG, arrowcolor=FG, borderwidth=0)

    def _header(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(bar, text="WiFi Analyzer", bg=BG, fg=FG, anchor="w",
                 font=("SF Pro Display", 20, "bold")).pack(side="left")
        self.lbl = tk.Label(bar, text="스캔 중…", bg=BG, fg=DIM, anchor="w",
                            font=("SF Mono", 11))
        self.lbl.pack(side="left", padx=(16, 0))
        r = tk.Frame(bar, bg=BG)
        r.pack(side="right")
        tk.Label(r, text="스캔 주기", bg=BG, fg=DIM,
                 font=("SF Mono", 10)).pack(side="left", padx=(0, 4))
        ttk.Spinbox(r, from_=2, to=60, increment=1, width=4,
                    textvariable=self.interval).pack(side="left")
        tk.Button(r, text="새로 고침", command=self.kick, bg="#263243", fg=FG,
                  activebackground="#33445b", activeforeground=FG,
                  highlightthickness=0, borderwidth=0, padx=12).pack(side="left", padx=(8, 0))
        self.warn = tk.Label(self, text="", bg=BG, fg=WARN, anchor="w",
                             justify="left", font=("SF Mono", 10))
        self.warn.pack(fill="x", padx=16, pady=(0, 4))

    def _filters(self, body):
        bar = tk.Frame(body, bg=PANEL, height=42)
        bar.pack(fill="x", pady=(0, 8))
        bar.pack_propagate(False)
        tk.Label(bar, text="네트워크", bg=PANEL, fg=FG,
                 font=("SF Mono", 11, "bold")).pack(side="left", padx=(12, 10))
        ttk.Combobox(bar, textvariable=self.band_filter, state="readonly", width=10,
                     values=("모든 대역", "2.4 GHz", "5 GHz")).pack(side="left")
        tk.Label(bar, text="  검색", bg=PANEL, fg=DIM,
                 font=("SF Mono", 10)).pack(side="left")
        search = tk.Entry(bar, textvariable=self.text_filter, bg="#1e242c", fg=FG,
                          insertbackground=FG, relief="flat",
                          font=("SF Mono", 10), width=26)
        search.pack(side="left", padx=(6, 0), ipady=4)
        tk.Label(bar, text="행을 선택하면 아래 그래프와 상세 정보가 갱신됩니다",
                 bg=PANEL, fg=DIM, font=("SF Mono", 10)).pack(side="right", padx=12)
        self.band_filter.trace_add("write", lambda *_: self.apply_filter())
        self.text_filter.trace_add("write", lambda *_: self.apply_filter())

    def _network_table(self, body):
        frame = tk.Frame(body, bg=BG)
        frame.pack(fill="both", expand=True)
        self.count_label = tk.Label(frame, text="발견한 네트워크", bg=BG, fg=DIM,
                                    anchor="w", font=("SF Mono", 10))
        self.count_label.pack(fill="x", pady=(0, 3))
        cols = ("ssid", "bssid", "band", "ch", "bw", "rssi", "snr", "sec", "ov")
        table = tk.Frame(frame, bg=PANEL)
        table.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(table, columns=cols, show="headings",
                               style="T.Treeview", selectmode="browse", height=10)
        specs = (
            ("ssid", "네트워크 이름", 260, "w"), ("bssid", "BSSID / MAC", 160, "w"),
            ("band", "대역", 62, "center"), ("ch", "채널", 54, "e"),
            ("bw", "폭", 62, "e"), ("rssi", "신호", 76, "e"),
            ("snr", "SNR", 62, "e"), ("sec", "보안", 120, "w"),
            ("ov", "겹침", 56, "e"),
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
        self.tv.bind("<<TreeviewSelect>>", self.on_pick)

    def _charts(self, body):
        charts = tk.Frame(body, bg=BG)
        charts.pack(fill="both", expand=True, pady=(10, 0))
        for band, title, attr in ((2, "2.4 GHz · 채널 혼잡도", "sp2"),
                                 (5, "5 GHz · 채널 혼잡도", "sp5")):
            pane = tk.Frame(charts, bg=PANEL)
            pane.pack(side="left", fill="both", expand=True,
                      padx=(0, 5) if band == 2 else (5, 0))
            tk.Label(pane, text=title, bg=PANEL, fg=DIM, anchor="w",
                     font=("SF Mono", 10, "bold")).pack(fill="x", padx=10, pady=(7, 0))
            canvas = Spectrum(pane, band, 250)
            canvas.pack(fill="both", expand=True, padx=4, pady=(0, 4))
            setattr(self, attr, canvas)

    def _detail(self, body):
        panel = tk.Frame(body, bg=PANEL, height=126)
        panel.pack(fill="x", pady=(10, 0))
        panel.pack_propagate(False)
        left = tk.Frame(panel, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(10, 8), pady=8)
        tk.Label(left, text="선택 네트워크 · 신호 추이", bg=PANEL, fg=DIM, anchor="w",
                 font=("SF Mono", 10, "bold")).pack(fill="x")
        self.trend = Trend(left)
        self.trend.pack(fill="both", expand=True, pady=(3, 0))
        right = tk.Frame(panel, bg=PANEL, width=490)
        right.pack(side="right", fill="both", padx=(8, 10), pady=8)
        right.pack_propagate(False)
        self.detail = tk.Label(right, text="표에서 네트워크를 선택하세요.", bg=PANEL,
                               fg=DIM, anchor="w", justify="left",
                               font=("SF Mono", 10))
        self.detail.pack(side="left", fill="both", expand=True)
        tk.Button(right, text="선택 해제", command=self.clear_sel, bg="#263243", fg=FG,
                  activebackground="#33445b", activeforeground=FG,
                  highlightthickness=0, borderwidth=0, padx=8).pack(side="right")

    # 스캔 루프
    def kick(self):
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
                self.after(int(max(2.0, self.interval.get()) * 1000), self.kick)
        except queue.Empty:
            pass
        self.after(200, self.pump)

    # 표시
    def rivals(self, ap):
        return [o for o in self.aps
                if o is not ap and o.band == ap.band and overlap(ap.rng, o.rng) > 0]

    def filtered_aps(self):
        query = self.text_filter.get().strip().casefold()
        band = {"2.4 GHz": 2, "5 GHz": 5}.get(self.band_filter.get())
        return [
            ap for ap in self.aps
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
        self.sp2.set_data(self.view_aps, self.sel)
        self.sp5.set_data(self.view_aps, self.sel)
        self.populate_table()
        self.update_detail()

    def populate_table(self):
        if not hasattr(self, "tv"):
            return
        keep = self.sel
        self.tv.delete(*self.tv.get_children())
        self.rows = sorted(self.view_aps, key=self._sort_value, reverse=self.sort_reverse)
        for a in self.rows:
            tag = "cur" if a.cur else ("strong" if a.rssi > -70 else "weak")
            self.tv.insert("", "end", iid=a.key, tags=(tag,), values=(
                ("● " if a.cur else "") + (a.ssid or "(숨김 SSID)"),
                a.bssid or "미제공",
                f"{a.band} GHz", a.ch, f"{a.width} MHz", f"{a.rssi} dBm",
                f"{a.snr} dB" if a.snr else "–", a.sec or "–", len(self.rivals(a))))
        total = len(self.aps)
        self.count_label.config(
            text=f"발견한 네트워크  {len(self.view_aps)}개"
                 + (f" / 전체 {total}개" if len(self.view_aps) != total else ""))
        if keep and self.tv.exists(keep):
            self.tv.selection_set(keep)

    def render(self, dt):
        now = time.time()
        for a in self.aps:
            self.hist[a.key].append(a.rssi)
            self.seen[a.key] = now

        cur = next((a for a in self.aps if a.cur), None)
        head = f"{time.strftime('%H:%M:%S')}  ·  AP {len(self.aps)}개  ·  {SOURCE}  ·  {dt:.1f}초"
        if cur:
            ov = self.rivals(cur)
            co = sum(1 for o in ov if o.ch == cur.ch)
            head += (f"  ·  연결: {cur.ssid or '(숨김 SSID)'}  ch{cur.ch} "
                     f"{cur.width}MHz  {cur.rssi}dBm  SNR {cur.snr}dB  "
                     f"겹침 {len(ov)} (동일 채널 {co})")
        self.lbl.config(text=head)

        hidden = sum(1 for a in self.aps if not a.bssid or not a.ssid)
        if hidden:
            st = self._locmgr.authorizationStatus() if self._locmgr else None
            stx = AUTH_NAMES.get(st, "확인불가")
            if st in (0, 1, 2):
                self.warn.config(text=(
                    f"⚠ SSID/BSSID {hidden}개가 macOS에서 가려짐 — 위치 권한 [{stx}]. "
                    "WiFiAnalyzer.app으로 실행한 뒤 위치 접근을 허용하세요."))
            else:
                self.warn.config(text=(
                    f"ℹ SSID/BSSID {hidden}개는 숨김 네트워크이거나 macOS 스캔 API가 "
                    f"제공하지 않은 항목입니다 — 위치 권한 [{stx}]."))
        else:
            self.warn.config(text="")

        self.apply_filter()

    def on_pick(self, _):
        s = self.tv.selection()
        self.sel = s[0] if s else None
        self.sp5.set_data(self.view_aps, self.sel)
        self.sp2.set_data(self.view_aps, self.sel)
        self.update_detail()

    def update_detail(self):
        ap = next((a for a in self.aps if a.key == self.sel), None)
        if not ap:
            self.trend.set_data([], OKC)
            self.detail.config(text="")
            return
        self.trend.set_data(self.hist[ap.key], color_of(ap.key))
        ov = self.rivals(ap)
        co = [o for o in ov if o.ch == ap.ch]
        adj = [o for o in ov if o.ch != ap.ch]
        f0, f1 = ap.rng
        worst = max((o.rssi for o in ov), default=None)
        self.detail.config(text=(
            f"{ap.ssid or '(숨김 SSID)'}   {ap.bssid or 'BSSID 미제공'}\n"
            f"ch{ap.ch} {ap.width}MHz  ({f0}–{f1} MHz)   "
            f"{ap.rssi}dBm / noise {ap.noise}dBm\n"
            f"동일채널 {len(co)}대 · 부분겹침 {len(adj)}대"
            + (f" · 최강 간섭원 {worst}dBm" if worst is not None else "")))

    def clear_sel(self):
        self.sel = None
        self.tv.selection_remove(*self.tv.selection())
        self.apply_filter()


if __name__ == "__main__":
    App().mainloop()
