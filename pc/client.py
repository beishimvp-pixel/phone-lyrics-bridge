"""
歌词桥 — Windows 桌面客户端
视觉：dir-2 巨字「词」红 / 黑 / 白
支持桌面歌词：位置 / 透明度 / 字号 / 横竖 / 颜色 / 屏幕取色
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import colorchooser, font as tkfont
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from lyrics import LyricLine, current_line, fetch_lyrics
from netutil import all_ipv4, local_ip
from hotkey import GlobalHotkey, format_hotkey, parse_hotkey
from style_prefs import LyricStyle, load_style, save_style, screen_pixel_hex, verticalize

PORT = 8765
STALE_MS = 8000
HIDE_AFTER_PAUSE_MS = 1200

RED = "#E60012"
BLACK = "#111111"
WHITE = "#FFFFFF"
MUTE = "#6B6B6B"
LINE = "#EEEEEE"
GRAY_BG = "#F3F3F3"
# 桌面歌词透明底：该色会被 Windows 抠掉（勿与歌词颜色相同）
CHROMA = "#FF00FE"


@dataclass
class PlaybackState:
    playing: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    position_ms: int = 0
    duration_ms: int = 0
    sent_at: int = 0
    received_at: float = 0.0
    track_key: str = ""
    package: str = ""


@dataclass
class AppModel:
    state: PlaybackState = field(default_factory=PlaybackState)
    lines: list[LyricLine] = field(default_factory=list)
    status: str = "等待手机连接…"
    clients: int = 0
    server_ok: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class BridgeServer:
    def __init__(self, model: AppModel) -> None:
        self.model = model
        self._fetch_token = 0

    async def handler(self, ws: ServerConnection) -> None:
        with self.model.lock:
            self.model.clients += 1
            self.model.status = "手机已连接"
        try:
            async for raw in ws:
                await self._on_message(str(raw))
        finally:
            with self.model.lock:
                self.model.clients = max(0, self.model.clients - 1)
                if self.model.clients == 0:
                    self.model.status = "等待手机连接…"
                    self.model.state = PlaybackState(received_at=time.time())

    async def _on_message(self, raw: str) -> None:
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return

        if data.get("type") == "stop":
            with self.model.lock:
                self.model.state = PlaybackState(received_at=time.time())
                self.model.lines = []
                self.model.status = "已停止"
            return

        title = str(data.get("title") or "").strip()
        artist = str(data.get("artist") or "").strip()
        album = str(data.get("album") or "").strip()
        package = str(data.get("package") or "").strip()
        playing = bool(data.get("playing"))
        position_ms = int(data.get("position_ms") or 0)
        duration_ms = int(data.get("duration_ms") or 0)
        sent_at = int(data.get("sent_at") or int(time.time() * 1000))
        track_key = f"{package}|{artist}|{title}".lower()

        with self.model.lock:
            old_key = self.model.state.track_key
            self.model.state = PlaybackState(
                playing=playing,
                title=title,
                artist=artist,
                album=album,
                position_ms=position_ms,
                duration_ms=duration_ms,
                sent_at=sent_at,
                received_at=time.time(),
                track_key=track_key,
                package=package,
            )
            self.model.status = "播放中" if playing else "已暂停"

        if not title:
            with self.model.lock:
                self.model.lines = []
            return

        if track_key != old_key:
            self._fetch_token += 1
            token = self._fetch_token
            asyncio.create_task(
                self._load_lyrics(token, artist, title, album, duration_ms, package)
            )

    async def _load_lyrics(
        self,
        token: int,
        artist: str,
        title: str,
        album: str,
        duration_ms: int,
        package: str,
    ) -> None:
        from lyrics import detect_source

        source = detect_source(package)
        tip = {
            "bilibili": "拉取 B 站字幕…",
            "fanqie": "番茄听书标题…",
            "qqmusic": "拉取歌词中…",
        }.get(source, "匹配文案中…")
        with self.model.lock:
            self.model.status = tip
            self.model.lines = []
        lines = await fetch_lyrics(artist, title, album, duration_ms, package)
        if token != self._fetch_token:
            return
        with self.model.lock:
            self.model.lines = lines
            if source == "bilibili":
                self.model.status = (
                    f"B站字幕 {len(lines)} 行" if len(lines) > 1 else "B站无字幕·显示标题"
                )
            elif source == "fanqie":
                self.model.status = "番茄·章节标题"
            else:
                self.model.status = f"歌词 {len(lines)} 行" if lines else "无歌词"


async def run_server(model: AppModel) -> None:
    server = BridgeServer(model)
    async with websockets.serve(
        server.handler,
        "0.0.0.0",
        PORT,
        ping_interval=20,
        ping_timeout=20,
    ):
        with model.lock:
            model.server_ok = True
            model.status = "服务已启动，等待手机连接…"
        await asyncio.Future()


class LyricsOverlay(tk.Toplevel):
    def __init__(self, master: tk.Tk, model: AppModel, style: LyricStyle) -> None:
        super().__init__(master)
        self.model = model
        self.style = style
        self.title("桌面歌词")
        self.attributes("-topmost", True)
        self.overrideredirect(True)
        # 无底板：窗口底色设为色键，交给 Windows 抠透明
        self.configure(bg=CHROMA)
        try:
            self.attributes("-transparentcolor", CHROMA)
            self.wm_attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass

        self.enabled = True
        self._drag_x = 0
        self._drag_y = 0
        self._visible = False
        self._last_cur = ""
        self._last_nxt = ""
        self._last_meta = ""

        self.bind("<ButtonPress-1>", self._start_drag)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._save_pos)

        self.shell = tk.Frame(self, bg=CHROMA)
        self.shell.pack(fill="both", expand=True)

        # 轻阴影（仍是字，不是色块底板）+ 正文
        self.shadow = tk.Label(
            self.shell, text="", font=("Microsoft YaHei UI", 28, "bold"),
            fg="#000000", bg=CHROMA, anchor="center", justify="center",
        )
        self.current = tk.Label(
            self.shell, text="", font=("Microsoft YaHei UI", 28, "bold"),
            fg=WHITE, bg=CHROMA, anchor="center", justify="center",
        )
        self.shadow.place(relx=0.5, rely=0.5, anchor="center", x=2, y=2)
        self.current.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self.shadow, self.current):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._save_pos)

        self.apply_style(rebuild=True)
        self.withdraw()
        self.after(80, self._tick)

    def apply_style(self, rebuild: bool = False) -> None:
        s = self.style
        try:
            self.attributes("-transparentcolor", CHROMA)
            self.wm_attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        try:
            # 透明度只作用在文字观感；底板已被抠掉
            self.attributes("-alpha", max(0.35, min(1.0, float(s.alpha))))
        except tk.TclError:
            pass

        color = (s.text_color or WHITE).upper()
        if color in (CHROMA.upper(), "#FF00FF", "#FF00FE"):
            color = WHITE
        size = max(10, min(72, int(s.font_size)))
        font = ("Microsoft YaHei UI", size, "bold")
        wrap = 40 if s.vertical else max(240, s.width - 48)
        for lab in (self.shadow, self.current):
            lab.config(
                font=font,
                bg=CHROMA,
                wraplength=wrap,
                justify="center",
                anchor="center",
            )
        self.current.config(fg=color)
        self.shadow.config(fg="#000000")

        if s.vertical:
            self.geometry(f"{max(90, s.width // 5)}x{max(280, s.height * 3)}")
        else:
            self.geometry(f"{max(320, s.width)}x{max(72, s.height)}")

        if s.x is not None and s.y is not None:
            self.geometry(f"+{s.x}+{s.y}")
        elif self.style.x is None:
            sw = self.winfo_screenwidth()
            w = max(90, s.width // 5) if s.vertical else max(320, s.width)
            self.geometry(f"+{(sw - w) // 2}+64")

        if self._last_cur:
            self._set_lyric(self._last_cur)

    def _set_lyric(self, cur: str) -> None:
        self._last_cur = cur
        text = verticalize(cur) if self.style.vertical else cur
        self.shadow.config(text=text)
        self.current.config(text=text)

    def _set_texts(self, meta: str, cur: str, nxt: str) -> None:
        # 兼容旧调用：忽略 meta / nxt
        self._last_meta, self._last_nxt = meta, nxt
        self._set_lyric(cur)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event: tk.Event) -> None:
        x = self.winfo_x() + event.x - self._drag_x
        y = self.winfo_y() + event.y - self._drag_y
        self.geometry(f"+{x}+{y}")

    def _save_pos(self, _event: tk.Event | None = None) -> None:
        self.style.x = self.winfo_x()
        self.style.y = self.winfo_y()
        save_style(self.style)

    def _estimate_position(self, st: PlaybackState) -> int:
        """按手机进度自动跟词：用 sent_at 补偿网络延迟，并加用户微调。"""
        base = int(st.position_ms)
        offset = int(getattr(self.style, "offset_ms", 0) or 0)
        if not st.playing:
            return max(0, base + offset)
        now_ms = int(time.time() * 1000)
        if st.sent_at > 0:
            lag = now_ms - int(st.sent_at)
            # 时钟偏差过大时退回本地接收时间
            if lag < 0 or lag > 15000:
                lag = int(max(0.0, (time.time() - st.received_at) * 1000)) if st.received_at else 0
        elif st.received_at:
            lag = int(max(0.0, (time.time() - st.received_at) * 1000))
        else:
            lag = 0
        return max(0, base + lag + offset)

    def _should_show(self, st: PlaybackState, has_lyrics: bool) -> bool:
        if not self.enabled:
            return False
        # 桌面层只展示已匹配到的歌词，不放占位文案
        if not has_lyrics:
            return False
        if not st.title:
            return False
        age_ms = (time.time() - st.received_at) * 1000 if st.received_at else 99999
        if age_ms > STALE_MS:
            return False
        if not st.playing and age_ms > HIDE_AFTER_PAUSE_MS:
            return False
        return True

    def _tick(self) -> None:
        with self.model.lock:
            st = PlaybackState(**self.model.state.__dict__)
            lines = list(self.model.lines)

        show = self._should_show(st, bool(lines))
        if show and not self._visible:
            self.deiconify()
            self._visible = True
        elif not show and self._visible:
            self.withdraw()
            self._visible = False

        if show:
            pos = self._estimate_position(st)
            _, cur, _nxt = current_line(lines, pos)
            self._set_lyric(cur or "…")

        self.after(80, self._tick)


class DesktopClient(tk.Tk):
    def __init__(self, model: AppModel) -> None:
        super().__init__()
        self.model = model
        self.style = load_style()
        self.title("歌词桥")
        self.configure(bg=GRAY_BG)
        self.geometry("720x640")
        self.minsize(480, 360)

        title_f = tkfont.Font(family="Microsoft YaHei UI", size=28, weight="bold")
        body_f = tkfont.Font(family="Microsoft YaHei UI", size=12)
        mono_f = tkfont.Font(family="Consolas", size=12)
        big_f = tkfont.Font(family="Microsoft YaHei UI", size=28, weight="bold")
        rail_f = tkfont.Font(family="Microsoft YaHei UI", size=48, weight="bold")
        tiny_f = tkfont.Font(family="Microsoft YaHei UI", size=10)

        shell = tk.Frame(self, bg=WHITE)
        shell.pack(fill="both", expand=True, padx=20, pady=20)

        rail = tk.Frame(shell, bg=RED, width=96)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Label(rail, text="词", font=rail_f, fg=WHITE, bg=RED).place(relx=0.5, rely=0.5, anchor="center")

        main = tk.Frame(shell, bg=WHITE)
        main.pack(side="left", fill="both", expand=True)

        # 可滚动内容区：窗口缩小时用滚轮/滚动条查看
        canvas = tk.Canvas(main, bg=WHITE, highlightthickness=0)
        scrollbar = tk.Scrollbar(main, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        pad = tk.Frame(canvas, bg=WHITE)
        pad_win = canvas.create_window((0, 0), window=pad, anchor="nw")

        def _sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(pad_win, width=event.width)

        pad.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        def _wheel(event: tk.Event) -> None:
            if getattr(event, "delta", 0):
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        def _bind_wheel(_event=None) -> None:
            canvas.bind_all("<MouseWheel>", _wheel)
            canvas.bind_all("<Button-4>", _wheel)
            canvas.bind_all("<Button-5>", _wheel)

        def _unbind_wheel(_event=None) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        inner_pad = tk.Frame(pad, bg=WHITE)
        inner_pad.pack(fill="both", expand=True, padx=22, pady=18)

        tk.Label(inner_pad, text="歌词桥", font=title_f, fg=BLACK, bg=WHITE).pack(anchor="w")
        tk.Label(inner_pad, text="桌面端 · 只显示歌词，无底板", font=body_f, fg=MUTE, bg=WHITE).pack(
            anchor="w", pady=(2, 12)
        )

        self.ip_var = tk.StringVar(value=local_ip())
        self.status_var = tk.StringVar(value="启动中…")
        self.link_var = tk.StringVar(value="等待连接")
        self.lyric_on_var = tk.StringVar(value="OFF")
        self.track_var = tk.StringVar(value="—")
        self.line_var = tk.StringVar(value="等待手机播放")
        self.clients_var = tk.StringVar(value="0")
        self._badges: list[tuple[tk.Label, tk.StringVar]] = []

        def info_row(label: str, value_var: tk.StringVar, badge_var: tk.StringVar | None = None) -> None:
            row = tk.Frame(inner_pad, bg=WHITE)
            row.pack(fill="x")
            tk.Frame(row, bg=LINE, height=1).pack(fill="x", side="top")
            inner = tk.Frame(row, bg=WHITE)
            inner.pack(fill="x", pady=8)
            tk.Label(inner, text=label, font=body_f, fg=BLACK, bg=WHITE, width=8, anchor="w").pack(side="left")
            tk.Label(inner, textvariable=value_var, font=mono_f, fg=BLACK, bg=WHITE, anchor="w").pack(
                side="left", fill="x", expand=True
            )
            if badge_var is not None:
                badge = tk.Label(inner, textvariable=badge_var, font=tiny_f, fg=WHITE, bg=BLACK, padx=10, pady=2)
                badge.pack(side="right")
                self._badges.append((badge, badge_var))

        info_row("地址", self.ip_var, tk.StringVar(value="局域网"))
        info_row("连接", self.link_var, self.clients_var)
        self._link_badge = self._badges[-1][0]
        info_row("歌词", self.status_var, self.lyric_on_var)
        self._lyric_badge = self._badges[-1][0]

        stage = tk.Frame(inner_pad, bg=WHITE)
        stage.pack(fill="x", pady=(6, 0))
        tk.Frame(stage, bg=BLACK, height=2).pack(fill="x")
        tk.Label(stage, text="DESKTOP", font=tiny_f, fg=MUTE, bg=WHITE).pack(anchor="w", pady=(10, 0))
        tk.Label(stage, textvariable=self.track_var, font=big_f, fg=BLACK, bg=WHITE, anchor="w").pack(anchor="w")
        tk.Label(stage, textvariable=self.line_var, font=body_f, fg=MUTE, bg=WHITE, anchor="w").pack(
            anchor="w", pady=(4, 8)
        )

        # ---- style panel ----
        style_box = tk.LabelFrame(
            inner_pad, text=" 桌面歌词样式 ", font=body_f, fg=BLACK, bg=WHITE, labelanchor="nw", padx=10, pady=8
        )
        style_box.pack(fill="x", pady=(4, 8))

        self.style.show_meta = False
        self.style.show_next = False
        self.style.show_rail = False
        if (self.style.text_color or "").upper() in ("#111111", "#000000", BLACK.upper()):
            self.style.text_color = WHITE
        self.alpha_var = tk.DoubleVar(value=self.style.alpha)
        self.size_var = tk.IntVar(value=self.style.font_size)
        self.vertical_var = tk.BooleanVar(value=self.style.vertical)
        self.offset_var = tk.IntVar(value=int(getattr(self.style, "offset_ms", 0) or 0))
        self.color_var = tk.StringVar(value=self.style.text_color)
        self.pos_var = tk.StringVar(value=self._pos_text())

        r1 = tk.Frame(style_box, bg=WHITE)
        r1.pack(fill="x", pady=4)
        tk.Label(r1, text="文字透明度", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Scale(
            r1, from_=0.35, to=1.0, resolution=0.05, orient="horizontal",
            variable=self.alpha_var, bg=WHITE, highlightthickness=0, command=lambda _=None: self._on_style(),
        ).pack(side="left", fill="x", expand=True)

        r2 = tk.Frame(style_box, bg=WHITE)
        r2.pack(fill="x", pady=4)
        tk.Label(r2, text="字号", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Scale(
            r2, from_=12, to=56, resolution=1, orient="horizontal",
            variable=self.size_var, bg=WHITE, highlightthickness=0, command=lambda _=None: self._on_style(),
        ).pack(side="left", fill="x", expand=True)

        r3 = tk.Frame(style_box, bg=WHITE)
        r3.pack(fill="x", pady=4)
        tk.Label(r3, text="方向", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Radiobutton(
            r3, text="横排", variable=self.vertical_var, value=False, bg=WHITE,
            command=self._on_style_rebuild,
        ).pack(side="left")
        tk.Radiobutton(
            r3, text="竖排", variable=self.vertical_var, value=True, bg=WHITE,
            command=self._on_style_rebuild,
        ).pack(side="left", padx=(12, 0))

        # 颜色与操作收在同一行，不再单独占一排大红按钮
        r4 = tk.Frame(style_box, bg=WHITE)
        r4.pack(fill="x", pady=4)
        tk.Label(r4, text="文字颜色", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        self.color_preview = tk.Label(r4, text="  ", bg=self.style.text_color, width=3, relief="solid", bd=1)
        self.color_preview.pack(side="left", padx=(0, 6))
        tk.Label(r4, textvariable=self.color_var, font=mono_f, bg=WHITE, fg=MUTE).pack(side="left")
        tk.Button(
            r4, text="选色", command=self.pick_color, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left", padx=(10, 4))
        tk.Button(
            r4, text="吸取", command=self.eyedropper, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left")

        r6 = tk.Frame(style_box, bg=WHITE)
        r6.pack(fill="x", pady=(4, 0))
        tk.Label(r6, text="位置", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Label(r6, textvariable=self.pos_var, font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left")
        tk.Button(
            r6, text="重置", command=self.reset_pos, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left", padx=8)
        tk.Label(r6, text="可拖动桌面歌词", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left")

        r_off = tk.Frame(style_box, bg=WHITE)
        r_off.pack(fill="x", pady=(8, 2))
        tk.Label(r_off, text="进度微调", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Scale(
            r_off, from_=-3000, to=3000, orient="horizontal", resolution=50,
            variable=self.offset_var, length=280, bg=WHITE, highlightthickness=0,
            command=lambda _v: self._on_style(),
        ).pack(side="left", fill="x", expand=True)
        tk.Label(r_off, text="ms", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left", padx=4)

        self.hotkey_var = tk.StringVar(value=self.style.hotkey or "Ctrl+Alt+H")
        self.hotkey_status = tk.StringVar(value="")
        r7 = tk.Frame(style_box, bg=WHITE)
        r7.pack(fill="x", pady=(8, 2))
        tk.Label(r7, text="快捷键", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Label(r7, textvariable=self.hotkey_var, font=mono_f, bg=WHITE, fg=BLACK).pack(side="left")
        tk.Button(
            r7, text="录制", command=self.record_hotkey, font=tiny_f,
            fg=WHITE, bg=BLACK, relief="flat", bd=0, padx=10, pady=2, cursor="hand2",
        ).pack(side="left", padx=8)
        tk.Button(
            r7, text="默认", command=self.reset_hotkey, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left")
        tk.Label(r7, textvariable=self.hotkey_status, font=tiny_f, bg=WHITE, fg=MUTE).pack(
            side="left", padx=8
        )
        tk.Label(
            style_box,
            text="桌面歌词无底板；快捷键显示/隐藏（需含 Ctrl / Alt / Shift）",
            font=tiny_f, bg=WHITE, fg=MUTE, anchor="w",
        ).pack(fill="x", pady=(4, 0))

        btns = tk.Frame(inner_pad, bg=WHITE)
        btns.pack(fill="x", pady=(10, 4))

        def action(text: str, cmd, primary: bool = False) -> None:
            tk.Button(
                btns, text=text, command=cmd, font=body_f,
                fg=WHITE, bg=RED if primary else BLACK,
                activebackground="#ff3344" if primary else "#333",
                relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            ).pack(side="left", padx=(0, 8))

        self.overlay_btn_var = tk.StringVar(value="关闭桌面歌词")
        action("复制 IP", self.copy_ip, primary=True)
        action("刷新 IP", self.refresh_ip)
        tk.Button(
            btns, textvariable=self.overlay_btn_var, command=self.toggle_overlay, font=body_f,
            fg=WHITE, bg=RED,
            activebackground="#ff3344",
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
        ).pack(side="left", padx=(0, 8))
        action("预览样式", self.preview_style)

        self.overlay_enabled = bool(getattr(self.style, "overlay_enabled", True))
        self.overlay = LyricsOverlay(self, model, self.style)
        self.overlay.enabled = self.overlay_enabled
        self._sync_overlay_btn()
        self._hotkey: GlobalHotkey | None = None
        self._recording = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.refresh_ui)
        self.after(500, self._sync_pos_label)
        self.after(300, self._register_hotkey)

    def _pos_text(self) -> str:
        if self.style.x is None or self.style.y is None:
            return "未固定（默认居中上方）"
        return f"X={self.style.x}  Y={self.style.y}"

    def _sync_pos_label(self) -> None:
        self.pos_var.set(self._pos_text())
        self.after(800, self._sync_pos_label)

    def _commit_style(self, rebuild: bool = False) -> None:
        self.style.alpha = float(self.alpha_var.get())
        self.style.font_size = int(self.size_var.get())
        self.style.vertical = bool(self.vertical_var.get())
        self.style.text_color = self.color_var.get()
        self.style.show_meta = False
        self.style.show_next = False
        self.style.show_rail = False
        self.style.offset_ms = int(self.offset_var.get())
        self.style.overlay_enabled = bool(self.overlay_enabled)
        save_style(self.style)
        self.overlay.style = self.style
        self.overlay.apply_style(rebuild=rebuild)
        self.color_preview.config(bg=self.style.text_color)

    def _on_style(self) -> None:
        self._commit_style(rebuild=False)

    def _on_style_rebuild(self) -> None:
        self._commit_style(rebuild=True)

    def pick_color(self) -> None:
        rgb, hx = colorchooser.askcolor(color=self.color_var.get(), title="选择歌词颜色")
        if hx:
            self.color_var.set(hx.upper())
            self._commit_style()

    def eyedropper(self) -> None:
        tip = tk.Toplevel(self)
        tip.title("吸取颜色")
        tip.attributes("-topmost", True)
        tip.geometry("320x120+200+200")
        tip.configure(bg=WHITE)
        tk.Label(
            tip,
            text="点击屏幕任意位置吸取颜色\n（先点下面开始，再点桌面）",
            font=("Microsoft YaHei UI", 11),
            bg=WHITE,
            fg=BLACK,
        ).pack(pady=16)

        def start() -> None:
            tip.destroy()
            picker = tk.Toplevel(self)
            picker.attributes("-topmost", True)
            picker.attributes("-alpha", 0.15)
            picker.overrideredirect(True)
            picker.configure(bg="#000000")
            picker.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
            picker.configure(cursor="crosshair")

            def grab(event: tk.Event) -> None:
                hx = screen_pixel_hex(event.x_root, event.y_root)
                self.color_var.set(hx)
                self._commit_style()
                picker.destroy()

            picker.bind("<Button-1>", grab)
            picker.focus_force()

        tk.Button(
            tip, text="开始吸取", command=start, bg=RED, fg=WHITE, relief="flat", padx=16, pady=8
        ).pack()

    def reset_pos(self) -> None:
        self.style.x = None
        self.style.y = None
        save_style(self.style)
        self.overlay.style = self.style
        self.overlay.apply_style(rebuild=True)
        self.pos_var.set(self._pos_text())

    def preview_style(self) -> None:
        self._commit_style(rebuild=True)
        self.overlay._set_lyric("这是桌面歌词预览")
        self.overlay.deiconify()
        self.overlay._visible = True

    def copy_ip(self) -> None:
        ip = local_ip()
        self.clipboard_clear()
        self.clipboard_append(ip)
        with self.model.lock:
            self.model.status = f"已复制 {ip}"

    def refresh_ip(self) -> None:
        self.ip_var.set(f"{local_ip()}:{PORT}")

    def _sync_overlay_btn(self) -> None:
        self.overlay_btn_var.set("关闭桌面歌词" if self.overlay_enabled else "开启桌面歌词")

    def toggle_overlay(self) -> None:
        self.overlay_enabled = not self.overlay_enabled
        self.overlay.enabled = self.overlay_enabled
        self.style.overlay_enabled = self.overlay_enabled
        save_style(self.style)
        self._sync_overlay_btn()
        if not self.overlay_enabled:
            self.overlay.withdraw()
            self.overlay._visible = False

    def _toggle_overlay_hotkey(self) -> None:
        # marshal to UI thread
        self.after(0, self.toggle_overlay)

    def _register_hotkey(self) -> None:
        if self._hotkey:
            self._hotkey.stop()
            self._hotkey = None
        hk = (self.hotkey_var.get() or "Ctrl+Alt+H").strip()
        self.style.hotkey = hk
        save_style(self.style)
        self._hotkey = GlobalHotkey(hk, self._toggle_overlay_hotkey)
        if self._hotkey.start():
            self.hotkey_status.set("已启用")
        else:
            self.hotkey_status.set(self._hotkey.error or "未启用")

    def reset_hotkey(self) -> None:
        self.hotkey_var.set("Ctrl+Alt+H")
        self._register_hotkey()

    def record_hotkey(self) -> None:
        if self._recording:
            return
        self._recording = True
        self.hotkey_status.set("请按下组合键…")
        grab = tk.Toplevel(self)
        grab.title("录制快捷键")
        grab.attributes("-topmost", True)
        grab.geometry("360x140+420+260")
        grab.configure(bg=WHITE)
        tk.Label(
            grab,
            text="按下组合键（例如 Ctrl+Alt+H）\n松开后自动保存",
            font=("Microsoft YaHei UI", 12),
            bg=WHITE,
            fg=BLACK,
        ).pack(pady=24)

        def down(vk: int) -> bool:
            return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

        def on_key(event: tk.Event) -> str:
            ks = (event.keysym or "").upper()
            if ks in (
                "CONTROL_L", "CONTROL_R", "CONTROL",
                "ALT_L", "ALT_R", "ALT",
                "SHIFT_L", "SHIFT_R", "SHIFT",
                "WIN_L", "WIN_R", "SUPER_L", "SUPER_R",
            ):
                return "break"

            mods = 0
            if down(0x11):  # VK_CONTROL
                mods |= 0x0002
            if down(0x12):  # VK_MENU / Alt
                mods |= 0x0001
            if down(0x10):  # VK_SHIFT
                mods |= 0x0004
            if down(0x5B) or down(0x5C):  # VK_LWIN / RWIN
                mods |= 0x0008

            key = ks[3:] if ks.startswith("KP_") else ks
            if len(key) == 1:
                key = key.upper()
            parts: list[str] = []
            if mods & 0x0002:
                parts.append("Ctrl")
            if mods & 0x0001:
                parts.append("Alt")
            if mods & 0x0004:
                parts.append("Shift")
            if mods & 0x0008:
                parts.append("Win")
            parts.append(key if len(key) <= 3 else key.title())
            candidate = "+".join(parts)
            parsed_try = parse_hotkey(candidate)
            if not parsed_try:
                self.hotkey_status.set("需包含 Ctrl/Alt/Shift")
                return "break"
            pretty = format_hotkey(parsed_try[0] & ~0x4000, parsed_try[1])
            self.hotkey_var.set(pretty)
            self._recording = False
            try:
                grab.grab_release()
            except Exception:
                pass
            grab.destroy()
            self._register_hotkey()
            return "break"

        grab.bind("<KeyPress>", on_key)
        grab.focus_force()
        grab.grab_set()

        def cancel() -> None:
            self._recording = False
            self.hotkey_status.set("已取消")
            try:
                grab.grab_release()
            except Exception:
                pass
            grab.destroy()

        grab.protocol("WM_DELETE_WINDOW", cancel)
        tk.Button(grab, text="取消", command=cancel, relief="flat", bg="#F0F0F0", padx=12, pady=4).pack()

    def refresh_ui(self) -> None:
        with self.model.lock:
            st = PlaybackState(**self.model.state.__dict__)
            lines = list(self.model.lines)
            status = self.model.status
            clients = self.model.clients
            ok = self.model.server_ok

        show = bool(st.title) and (
            st.playing or (st.received_at and (time.time() - st.received_at) * 1000 <= HIDE_AFTER_PAUSE_MS)
        )
        if st.title and st.received_at and (time.time() - st.received_at) * 1000 > STALE_MS:
            show = False
        if not self.overlay_enabled:
            show = False

        self.ip_var.set(f"{local_ip()}:{PORT}")
        self.link_var.set("手机已连接" if clients else "等待手机连接")
        self.clients_var.set(str(clients) if clients else "0")
        self.status_var.set(status if ok else f"服务异常 · {status}")
        self.lyric_on_var.set("ON" if show else "OFF")
        self._link_badge.config(bg=BLACK if clients else "#DDDDDD", fg=WHITE if clients else MUTE)
        self._lyric_badge.config(bg=BLACK if show else "#DDDDDD", fg=WHITE if show else MUTE)

        if st.title:
            self.track_var.set(st.title)
            if lines:
                pos = st.position_ms
                if st.playing and st.received_at:
                    pos += int(max(0.0, (time.time() - st.received_at) * 1000))
                _, cur, nxt = current_line(lines, pos)
                self.line_var.set(cur or nxt or "…")
            else:
                self.line_var.set(f"{st.artist} · 未匹配到歌词" if st.artist else "未匹配到歌词")
        else:
            self.track_var.set("—")
            self.line_var.set("停播即隐藏" if clients else "等待手机播放")

        self.after(200, self.refresh_ui)

    def on_close(self) -> None:
        self._commit_style()
        if self._hotkey:
            self._hotkey.stop()
            self._hotkey = None
        self.destroy()


def main() -> None:
    socket.setdefaulttimeout(5)
    model = AppModel()
    print("歌词桥桌面客户端 · dir-2 + 样式调节")
    print("本机 IP:", ", ".join(all_ipv4()))
    print("端口:", PORT)

    def _thread() -> None:
        try:
            asyncio.run(run_server(model))
        except Exception as e:
            with model.lock:
                model.server_ok = False
                model.status = f"服务失败：{e}"

    threading.Thread(target=_thread, daemon=True).start()
    DesktopClient(model).mainloop()


if __name__ == "__main__":
    main()
