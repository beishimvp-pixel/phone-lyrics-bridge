"""
歌词桥 — Windows 桌面客户端
视觉：dir-2 巨字「词」红 / 黑 / 白
支持桌面歌词：位置 / 透明度 / 字号 / 横竖 / 颜色 / 屏幕取色
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import socket
import sys
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import colorchooser, font as tkfont, messagebox
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection
from PIL import Image, ImageDraw, ImageFont, ImageTk

from lyrics import LyricLine, current_line, fetch_lyrics
from netutil import all_ipv4, local_ip
from hotkey import GlobalHotkey, format_hotkey, parse_hotkey
from style_prefs import LyricStyle, load_style, log_path, save_style, screen_pixel_hex, verticalize

# CreateMutex 句柄需在进程存活期持有，否则单实例失效
_INSTANCE_MUTEX = None

PORT = 8765
# 息屏/切后台时手机 WS 常会断几秒～一两分钟：续显并跟进度，勿立刻藏词
STALE_MS = 180_000
HOLD_AFTER_DISCONNECT_MS = 180_000
# 暂停后仍保留桌面词一段时间（QQ 桌面歌词也不会秒藏）
HIDE_AFTER_PAUSE_MS = 60_000
# QQ 式实心底条：整块可点可拖，避免色键点穿/点击后坐标异常导致「点一下就没了」
PANEL_BG = "#101010"
HIT_BAR = "#1A1A1A"
_LOG_LOCK = threading.Lock()
_LOG_MAX_BYTES = 1_500_000


def app_log(msg: str) -> None:
    """写入 %APPDATA%\\LyricsBridge\\client.log，方便排查卡住/打不开。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        path = log_path()
        with _LOG_LOCK:
            if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
                bak = path.with_suffix(".log.bak")
                try:
                    if bak.exists():
                        bak.unlink()
                    path.replace(bak)
                except OSError:
                    pass
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def ensure_single_instance() -> bool:
    """已有实例则提示并前置旧窗口，避免二次启动占不到 8765、看起来像打不开。"""
    global _INSTANCE_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\PhoneLyricsBridge_SingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return False
    return True


def focus_existing_instance() -> None:
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, "歌词桥")
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def virtual_screen() -> tuple[int, int, int, int]:
    """整块虚拟桌面（含副屏），不要用 winfo_screenwidth 只认主屏。"""
    user32 = ctypes.windll.user32
    return (
        int(user32.GetSystemMetrics(76)),  # SM_XVIRTUALSCREEN
        int(user32.GetSystemMetrics(77)),  # SM_YVIRTUALSCREEN
        int(user32.GetSystemMetrics(78)),  # SM_CXVIRTUALSCREEN
        int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
    )


def clamp_to_virtual(x: int, y: int, w: int = 80, h: int = 40) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    if vw <= 0 or vh <= 0:
        return x, y
    # 至少留一截在屏内，避免拖没
    x = min(max(x, vx - w + 40), vx + vw - 40)
    y = min(max(y, vy - h + 20), vy + vh - 20)
    return x, y


def coords_sane(x: int, y: int) -> bool:
    """透明/无边框窗在点击时 winfo 有时会给 -32000 等脏值。"""
    if x <= -30000 or y <= -30000:
        return False
    if abs(x) > 50000 or abs(y) > 50000:
        return False
    return True

RED = "#E60012"
BLACK = "#111111"
WHITE = "#FFFFFF"
MUTE = "#6B6B6B"
LINE = "#EEEEEE"
GRAY_BG = "#F3F3F3"
# 色键：抠透明底。歌词用 Pillow 硬阈值绘制，避免系统抗锯齿把边缘染成「假描边」。
CHROMA = "#01FE01"
CHROMA_RGB = (1, 254, 1)


def _parse_hex(color: str, fallback: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int, int]:
    raw = (color or "").strip().lstrip("#")
    if len(raw) != 6:
        return fallback
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return fallback


def _load_ui_font(size: int, bold: bool) -> ImageFont.ImageFont:
    size = max(8, min(72, int(size)))
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_lyric_image(
    text: str,
    *,
    font_size: int,
    bold: bool,
    fill: str,
    outline: str,
    outline_width: int,
    max_width: int,
    vertical: bool,
) -> Image.Image:
    """把歌词画成位图：填充色/描边色可独立设置；透明区填色键。"""
    content = verticalize(text) if vertical else (text or "")
    if not content.strip():
        content = " "
    font = _load_ui_font(font_size, bold)
    fill_rgb = _parse_hex(fill, (255, 255, 255))
    # 禁止与色键撞车
    if fill_rgb == CHROMA_RGB:
        fill_rgb = (255, 255, 255)
    outline_rgb = _parse_hex(outline, (0, 0, 0))
    if outline_rgb == CHROMA_RGB:
        outline_rgb = (0, 0, 0)
    stroke = max(0, min(4, int(outline_width)))

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    bbox = probe.multiline_textbbox((0, 0), content, font=font, spacing=4, align="center")
    tw = max(1, bbox[2] - bbox[0])
    th = max(1, bbox[3] - bbox[1])
    pad = 8 + stroke * 2
    # 横排过长时按字号粗略限制宽度
    width = min(max(tw + pad * 2, 40), max(120, int(max_width)))
    height = th + pad * 2
    if tw + pad * 2 > width and not vertical:
        # 简单按字符换行
        chars = list(content.replace("\n", ""))
        lines: list[str] = []
        cur = ""
        for ch in chars:
            trial = cur + ch
            tb = probe.textbbox((0, 0), trial, font=font)
            if (tb[2] - tb[0]) <= width - pad * 2 or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        content = "\n".join(lines) or " "
        bbox = probe.multiline_textbbox((0, 0), content, font=font, spacing=4, align="center")
        tw = max(1, bbox[2] - bbox[0])
        th = max(1, bbox[3] - bbox[1])
        width = min(max(tw + pad * 2, 40), max(120, int(max_width)))
        height = th + pad * 2

    # 先画到 RGBA，再硬阈值落到色键底，消灭半透明粉/黑边
    rgba = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rgba)
    xy = (width // 2, height // 2)
    if stroke > 0:
        draw.multiline_text(
            xy, content, font=font, fill=outline_rgb + (255,),
            anchor="mm", spacing=4, align="center",
            stroke_width=stroke, stroke_fill=outline_rgb + (255,),
        )
    draw.multiline_text(
        xy, content, font=font, fill=fill_rgb + (255,),
        anchor="mm", spacing=4, align="center",
    )

    out = Image.new("RGB", (width, height), CHROMA_RGB)
    px_out = out.load()
    px_in = rgba.load()
    for y in range(height):
        for x in range(width):
            r, g, b, a = px_in[x, y]
            if a >= 200:
                px_out[x, y] = (r, g, b)
    return out


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
        peer = ""
        try:
            peer = str(getattr(ws, "remote_address", "") or "")
        except Exception:
            peer = ""
        with self.model.lock:
            self.model.clients += 1
            self.model.status = "手机已连接"
        app_log(f"phone connected {peer} clients={self.model.clients}")
        try:
            async for raw in ws:
                await self._on_message(str(raw))
        except Exception as e:
            app_log(f"phone ws error {peer}: {e}")
        finally:
            with self.model.lock:
                self.model.clients = max(0, self.model.clients - 1)
                if self.model.clients == 0:
                    # 息屏断线不丢歌：保留上一曲进度，桌面继续跟词
                    st = self.model.state
                    if st.title and st.playing:
                        self.model.status = "手机暂离·歌词续显中"
                    else:
                        self.model.status = "等待手机连接…"
            app_log(f"phone disconnected {peer} clients={self.model.clients}")

    async def _on_message(self, raw: str) -> None:
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return

        if data.get("type") == "hello":
            with self.model.lock:
                self.model.status = "手机通道正常，等待播歌…"
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
            app_log(f"track change playing={playing} {artist} - {title}")
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
            "soda": "拉取歌词中…",
            "netease": "拉取歌词中…",
            "kugou": "拉取歌词中…",
        }.get(source, "匹配文案中…")
        with self.model.lock:
            self.model.status = tip
            self.model.lines = []
        try:
            lines = await fetch_lyrics(artist, title, album, duration_ms, package)
        except Exception as e:
            app_log(f"fetch_lyrics failed: {e}")
            lines = []
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
        app_log(f"lyrics ready source={source} lines={len(lines)} {artist} - {title}")


async def run_server(model: AppModel) -> None:
    server = BridgeServer(model)
    app_log(f"starting ws server 0.0.0.0:{PORT}")
    async with websockets.serve(
        server.handler,
        "0.0.0.0",
        PORT,
        # 息屏时手机端可能短暂无法回 ping；放宽避免误踢
        ping_interval=45,
        ping_timeout=120,
    ):
        with model.lock:
            model.server_ok = True
            model.status = "服务已启动，等待手机连接…"
        app_log("ws server listening")
        await asyncio.Future()


class LyricsOverlay(tk.Toplevel):
    def __init__(self, master: tk.Tk, model: AppModel, style: LyricStyle) -> None:
        super().__init__(master)
        self.model = model
        self.style = style
        self.title("桌面歌词")
        self.attributes("-topmost", True)
        self.overrideredirect(True)
        # 默认色键透明：只露字；鼠标悬停才出拖动条（QQ 桌面歌词同款）
        self.configure(bg=CHROMA)
        try:
            self.attributes("-transparentcolor", CHROMA)
            self.wm_attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass

        self.enabled = True
        self._dragging = False
        self._chrome_on = False
        self._chrome_hide_job: str | None = None
        self._drag_root_x = 0
        self._drag_root_y = 0
        self._win_x = 0
        self._win_y = 0
        self._live_x: int | None = None
        self._live_y: int | None = None
        self._visible = False
        self._last_cur = ""
        self._last_nxt = ""
        self._last_meta = ""
        self._photo: ImageTk.PhotoImage | None = None
        self._pause_since: float | None = None
        self._img_w = 200
        self._img_h = 40

        self.shell = tk.Frame(self, bg=CHROMA)
        self.shell.pack(fill="both", expand=True)

        self.current = tk.Label(self.shell, image="", bg=CHROMA, bd=0, highlightthickness=0, cursor="fleur")
        self.hit_bar = tk.Frame(self.shell, bg=HIT_BAR, height=22, cursor="fleur")
        self.grip = tk.Label(
            self.hit_bar, text="按住拖动", font=("Microsoft YaHei UI", 8),
            fg="#B0B0B0", bg=HIT_BAR, cursor="fleur",
        )
        self.current.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self.grip.pack(fill="x")
        # 默认隐藏拖动条；悬停再显示；右键快速隐藏整窗
        for w in (self, self.shell, self.current, self.hit_bar, self.grip):
            self._bind_drag(w)
            w.bind("<Enter>", self._on_chrome_enter, add="+")
            w.bind("<Leave>", self._on_chrome_leave, add="+")
            w.bind("<Button-3>", self._on_right_hide, add="+")

        self.apply_style(rebuild=True)
        self.withdraw()
        self.after(80, self._tick)

    def _bind_drag(self, w: tk.Misc) -> None:
        w.bind("<ButtonPress-1>", self._start_drag)
        w.bind("<B1-Motion>", self._on_drag)
        w.bind("<ButtonRelease-1>", self._save_pos)

    def _on_right_hide(self, _event: tk.Event | None = None) -> str:
        """右键：立刻关掉桌面歌词（主窗按钮 / 快捷键可再开）。"""
        if not self.enabled:
            return "break"
        master = self.master
        if hasattr(master, "toggle_overlay"):
            master.after(0, master.toggle_overlay)
        else:
            self.enabled = False
            self.withdraw()
            self._visible = False
        return "break"

    def _pointer_over_self(self) -> bool:
        try:
            x = self.winfo_pointerx()
            y = self.winfo_pointery()
            widget = self.winfo_containing(x, y)
            while widget is not None:
                if widget == self:
                    return True
                widget = widget.master  # type: ignore[assignment]
        except tk.TclError:
            pass
        return False

    def _on_chrome_enter(self, _event: tk.Event | None = None) -> None:
        if self._chrome_hide_job:
            try:
                self.after_cancel(self._chrome_hide_job)
            except Exception:
                pass
            self._chrome_hide_job = None
        self._set_chrome(True)

    def _on_chrome_leave(self, _event: tk.Event | None = None) -> None:
        if self._dragging:
            return
        if self._chrome_hide_job:
            try:
                self.after_cancel(self._chrome_hide_job)
            except Exception:
                pass
        # 子控件之间移动会抖 Leave，稍等再判断指针是否还在窗内
        self._chrome_hide_job = self.after(280, self._hide_chrome_if_left)

    def _hide_chrome_if_left(self) -> None:
        self._chrome_hide_job = None
        if self._dragging:
            return
        if self._pointer_over_self():
            return
        self._set_chrome(False)

    def _set_chrome(self, on: bool) -> None:
        """显示/隐藏拖动条：平时透明只留字，悬停才出操作条。"""
        if on == self._chrome_on and not on:
            return
        want = on and bool(getattr(self.style, "show_grip", True))
        if want:
            if not self.hit_bar.winfo_ismapped():
                self.hit_bar.pack(fill="x", side="bottom")
            self._chrome_on = True
        else:
            if self.hit_bar.winfo_ismapped():
                self.hit_bar.pack_forget()
            self._chrome_on = False
        # 高度随条显隐变化，位置保持
        bar_h = 22 if self._chrome_on else 0
        self._place(self._img_w + 16, self._img_h + 8 + bar_h)

    def _current_xy(self) -> tuple[int, int]:
        if self._live_x is not None and self._live_y is not None and coords_sane(self._live_x, self._live_y):
            return int(self._live_x), int(self._live_y)
        s = self.style
        if s.x is not None and s.y is not None and coords_sane(int(s.x), int(s.y)):
            return int(s.x), int(s.y)
        vx, vy, vw, _vh = virtual_screen()
        return int(vx + max(0, (vw - 420) // 2)), int(vy + 72)

    def _place(self, width: int, height: int) -> None:
        """尺寸+位置一次设上，避免只改尺寸时 Tk 把窗口弹回主屏。"""
        if self._dragging:
            return
        x, y = self._current_xy()
        x, y = clamp_to_virtual(x, y, width, height)
        self._live_x, self._live_y = x, y
        self.geometry(f"{max(80, width)}x{max(36, height)}+{x}+{y}")

    def apply_style(self, rebuild: bool = False) -> None:
        s = self.style
        self.configure(bg=CHROMA)
        self.shell.configure(bg=CHROMA)
        self.current.configure(bg=CHROMA)
        try:
            self.attributes("-transparentcolor", CHROMA)
            self.wm_attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        try:
            self.attributes("-alpha", max(0.25, min(1.0, float(s.alpha))))
        except tk.TclError:
            pass

        self.grip.config(text="按住拖动" if bool(getattr(s, "show_grip", True)) else "")
        if not bool(getattr(s, "show_grip", True)):
            self._set_chrome(False)

        if not self._dragging:
            if s.x is not None and s.y is not None and coords_sane(int(s.x), int(s.y)):
                self._live_x, self._live_y = int(s.x), int(s.y)
            elif self._live_x is None or not coords_sane(self._live_x or 0, self._live_y or 0):
                vx, vy, vw, _vh = virtual_screen()
                self._live_x = int(vx + max(0, (vw - 420) // 2))
                self._live_y = int(vy + 72)

        for w in (self, self.shell, self.current, self.hit_bar, self.grip):
            self._bind_drag(w)

        if self._last_cur:
            self._set_lyric(self._last_cur)
        elif rebuild:
            self._set_lyric("歌词预览 Aa")

    def _set_lyric(self, cur: str) -> None:
        self._last_cur = cur
        s = self.style
        fill = (s.text_color or WHITE).upper()
        if fill in (CHROMA.upper(), "#01FE01", "#00FF00", "#FF00FE", "#FF00FF"):
            fill = WHITE
        outline = (getattr(s, "outline_color", None) or "#000000").upper()
        img = render_lyric_image(
            cur or " ",
            font_size=int(s.font_size),
            bold=bool(getattr(s, "bold", False)),
            fill=fill,
            outline=outline,
            outline_width=int(getattr(s, "outline_width", 0) or 0),
            max_width=max(200, int(s.width) if s.width else 520),
            vertical=bool(s.vertical),
        )
        # 闲置：色键透明只留字；悬停出条时也不改字图
        self._photo = ImageTk.PhotoImage(img)
        self.current.config(image=self._photo, bg=CHROMA)
        self._img_w, self._img_h = img.width, img.height
        bar_h = 22 if self._chrome_on else 0
        self._place(self._img_w + 16, self._img_h + 8 + bar_h)

    def _set_texts(self, meta: str, cur: str, nxt: str) -> None:
        # 兼容旧调用：忽略 meta / nxt
        self._last_meta, self._last_nxt = meta, nxt
        self._set_lyric(cur)

    def _start_drag(self, event: tk.Event) -> None:
        self._dragging = True
        self._drag_root_x = int(event.x_root)
        self._drag_root_y = int(event.y_root)
        x, y = self._current_xy()
        try:
            wx, wy = int(self.winfo_x()), int(self.winfo_y())
            if coords_sane(wx, wy):
                x, y = wx, wy
        except tk.TclError:
            pass
        self._win_x, self._win_y = x, y
        self._live_x, self._live_y = x, y

    def _on_drag(self, event: tk.Event) -> None:
        if not self._dragging:
            return
        x = self._win_x + int(event.x_root) - self._drag_root_x
        y = self._win_y + int(event.y_root) - self._drag_root_y
        try:
            ww, hh = max(80, self.winfo_width()), max(36, self.winfo_height())
        except tk.TclError:
            ww, hh = 80, 36
        x, y = clamp_to_virtual(x, y, ww, hh)
        self._live_x, self._live_y = x, y
        self.geometry(f"+{x}+{y}")

    def _save_pos(self, event: tk.Event | None = None) -> None:
        moved = True
        if event is not None:
            dx = abs(int(event.x_root) - self._drag_root_x)
            dy = abs(int(event.y_root) - self._drag_root_y)
            moved = dx >= 4 or dy >= 4
        self._dragging = False
        if not moved:
            # 单击不改位置，避免脏坐标把窗口「点没」
            return
        x, y = self._current_xy()
        if self._live_x is not None and self._live_y is not None and coords_sane(self._live_x, self._live_y):
            x, y = int(self._live_x), int(self._live_y)
        if not coords_sane(x, y):
            return
        try:
            ww, hh = max(80, self.winfo_width()), max(36, self.winfo_height())
        except tk.TclError:
            ww, hh = 80, 36
        x, y = clamp_to_virtual(x, y, ww, hh)
        self._live_x, self._live_y = x, y
        self.style.x = x
        self.style.y = y
        save_style(self.style)
        # 拖完若鼠标已离开，收回拖动条
        self.after(200, self._hide_chrome_if_left)

    def _estimate_position(self, st: PlaybackState) -> int:
        """按手机进度自动跟词；断线续显时也用本地时钟往前推。"""
        base = int(st.position_ms)
        offset = int(getattr(self.style, "offset_ms", 0) or 0)
        if not st.playing:
            return max(0, base + offset)
        now_ms = int(time.time() * 1000)
        if st.sent_at > 0:
            lag = now_ms - int(st.sent_at)
            # 断线续显允许更大 lag；时钟偏差离谱时退回 received_at
            if lag < 0 or lag > HOLD_AFTER_DISCONNECT_MS:
                lag = int(max(0.0, (time.time() - st.received_at) * 1000)) if st.received_at else 0
        elif st.received_at:
            lag = int(max(0.0, (time.time() - st.received_at) * 1000))
        else:
            lag = 0
        return max(0, base + lag + offset)

    def _should_show(self, st: PlaybackState) -> bool:
        if not self.enabled:
            return False
        if not st.title:
            return False
        age_ms = (time.time() - st.received_at) * 1000 if st.received_at else 99999
        if age_ms > STALE_MS:
            return False
        if st.playing:
            self._pause_since = None
            return True
        # 暂停：不要秒藏（手机 playing 抖动 / 短暂停顿）
        if self._pause_since is None:
            self._pause_since = time.time()
        paused_for = (time.time() - self._pause_since) * 1000
        return paused_for <= HIDE_AFTER_PAUSE_MS

    def _tick(self) -> None:
        try:
            with self.model.lock:
                st = PlaybackState(**self.model.state.__dict__)
                lines = list(self.model.lines)

            show = self._should_show(st)
            # 拖动中绝不隐藏，避免点按被误判成消失
            if show and not self._visible:
                self.deiconify()
                self.lift()
                self._visible = True
                app_log(f"overlay show: {st.artist} - {st.title}")
            elif not show and self._visible and not self._dragging:
                self.withdraw()
                self._visible = False
                app_log("overlay hide")

            if show:
                if lines:
                    pos = self._estimate_position(st)
                    _, cur, _nxt = current_line(lines, pos)
                    text = cur or st.title or "…"
                else:
                    text = st.title or "…"
                if text != self._last_cur:
                    self._set_lyric(text)
        except Exception:
            app_log("overlay tick error:\n" + traceback.format_exc())
        self.after(80, self._tick)


class DesktopClient(tk.Tk):
    def __init__(self, model: AppModel) -> None:
        super().__init__()
        self.model = model
        self.style = load_style()
        self.title("歌词桥")
        self.configure(bg=GRAY_BG)
        self.geometry("720x760")
        self.minsize(520, 480)

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
            canvas.update_idletasks()
            bbox = canvas.bbox("all")
            if bbox:
                canvas.configure(scrollregion=bbox)

        def _sync_width(event: tk.Event) -> None:
            canvas.itemconfigure(pad_win, width=max(1, int(event.width)))
            _sync_scroll()

        pad.bind("<Configure>", _sync_scroll)
        canvas.bind("<Configure>", _sync_width)

        def _wheel(event: tk.Event) -> str | None:
            # 只在指针落在本窗口内时滚动，避免抢其它窗口滚轮
            try:
                x = self.winfo_pointerx() - self.winfo_rootx()
                y = self.winfo_pointery() - self.winfo_rooty()
                if x < 0 or y < 0 or x > self.winfo_width() or y > self.winfo_height():
                    return None
            except tk.TclError:
                return None
            if getattr(event, "delta", 0):
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")
            return "break"

        # Scale/按钮在 Canvas 内时 Leave 会误触发，改用 bind_all + 窗口焦点判断
        def _bind_wheel(_event=None) -> None:
            self.bind_all("<MouseWheel>", _wheel)
            self.bind_all("<Button-4>", _wheel)
            self.bind_all("<Button-5>", _wheel)

        def _unbind_wheel(_event=None) -> None:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")

        self.bind("<FocusIn>", _bind_wheel)
        self.bind("<FocusOut>", _unbind_wheel)
        self.bind("<Map>", _bind_wheel)
        self.after(100, _bind_wheel)
        self.after(300, _sync_scroll)

        inner_pad = tk.Frame(pad, bg=WHITE)
        inner_pad.pack(fill="both", expand=True, padx=22, pady=18)

        tk.Label(inner_pad, text="歌词桥", font=title_f, fg=BLACK, bg=WHITE).pack(anchor="w")
        tk.Label(inner_pad, text="桌面端 · 摸鱼小字 / 息屏续显", font=body_f, fg=MUTE, bg=WHITE).pack(
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
        # 旧配置里的大白粗字 → 迁到摸鱼友好默认（仅当仍是旧默认值时）
        if int(self.style.font_size) >= 28 and (self.style.text_color or "").upper() in (
            WHITE.upper(), "#FFFFFF", "#FFF"
        ):
            self.style.font_size = 12
            self.style.text_color = "#B8B8B8"
            self.style.alpha = min(float(self.style.alpha), 0.55)
            self.style.bold = False
        self.alpha_var = tk.DoubleVar(value=self.style.alpha)
        self.size_var = tk.IntVar(value=max(8, min(48, int(self.style.font_size))))
        self.vertical_var = tk.BooleanVar(value=self.style.vertical)
        self.bold_var = tk.BooleanVar(value=bool(getattr(self.style, "bold", False)))
        self.grip_var = tk.BooleanVar(value=bool(getattr(self.style, "show_grip", True)))
        self.offset_var = tk.IntVar(value=int(getattr(self.style, "offset_ms", 0) or 0))
        self.color_var = tk.StringVar(value=self.style.text_color)
        self.outline_color_var = tk.StringVar(
            value=getattr(self.style, "outline_color", None) or "#000000"
        )
        self.outline_width_var = tk.IntVar(
            value=int(getattr(self.style, "outline_width", 0) or 0)
        )
        self.pos_var = tk.StringVar(value=self._pos_text())

        r0 = tk.Frame(style_box, bg=WHITE)
        r0.pack(fill="x", pady=(0, 6))
        tk.Label(r0, text="预设", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Button(
            r0, text="摸鱼隐蔽", command=lambda: self.apply_preset("stealth"), font=tiny_f,
            fg=WHITE, bg=BLACK, relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            r0, text="清晰大字", command=lambda: self.apply_preset("clear"), font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=10, pady=3, cursor="hand2",
        ).pack(side="left")

        r1 = tk.Frame(style_box, bg=WHITE)
        r1.pack(fill="x", pady=4)
        tk.Label(r1, text="隐蔽度", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Scale(
            r1, from_=0.18, to=1.0, resolution=0.02, orient="horizontal",
            variable=self.alpha_var, bg=WHITE, highlightthickness=0, command=lambda _=None: self._on_style(),
        ).pack(side="left", fill="x", expand=True)
        tk.Label(r1, text="淡←整体→实（不改字色）", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left", padx=4)

        r2 = tk.Frame(style_box, bg=WHITE)
        r2.pack(fill="x", pady=4)
        tk.Label(r2, text="字号", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Scale(
            r2, from_=8, to=48, resolution=1, orient="horizontal",
            variable=self.size_var, bg=WHITE, highlightthickness=0, command=lambda _=None: self._on_style(),
        ).pack(side="left", fill="x", expand=True)
        self.size_label = tk.Label(r2, text=str(self.size_var.get()), font=tiny_f, bg=WHITE, fg=MUTE, width=3)
        self.size_label.pack(side="left")

        r3 = tk.Frame(style_box, bg=WHITE)
        r3.pack(fill="x", pady=4)
        tk.Label(r3, text="样式", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Radiobutton(
            r3, text="横排", variable=self.vertical_var, value=False, bg=WHITE,
            command=self._on_style_rebuild,
        ).pack(side="left")
        tk.Radiobutton(
            r3, text="竖排", variable=self.vertical_var, value=True, bg=WHITE,
            command=self._on_style_rebuild,
        ).pack(side="left", padx=(8, 0))
        tk.Checkbutton(
            r3, text="加粗", variable=self.bold_var, bg=WHITE, command=self._on_style,
        ).pack(side="left", padx=(14, 0))
        tk.Checkbutton(
            r3, text="拖动手柄", variable=self.grip_var, bg=WHITE, command=self._on_style,
        ).pack(side="left", padx=(8, 0))

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

        r5 = tk.Frame(style_box, bg=WHITE)
        r5.pack(fill="x", pady=4)
        tk.Label(r5, text="描边", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        self.outline_preview = tk.Label(
            r5, text="  ", bg=self.outline_color_var.get(), width=3, relief="solid", bd=1
        )
        self.outline_preview.pack(side="left", padx=(0, 6))
        tk.Label(r5, textvariable=self.outline_color_var, font=mono_f, bg=WHITE, fg=MUTE).pack(side="left")
        tk.Button(
            r5, text="描边色", command=self.pick_outline_color, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left", padx=(10, 4))
        tk.Label(r5, text="粗细", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left", padx=(8, 2))
        tk.Scale(
            r5, from_=0, to=3, resolution=1, orient="horizontal", length=90,
            variable=self.outline_width_var, bg=WHITE, highlightthickness=0,
            command=lambda _=None: self._on_style(),
        ).pack(side="left")
        tk.Label(r5, text="0=关", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left", padx=4)

        r6 = tk.Frame(style_box, bg=WHITE)
        r6.pack(fill="x", pady=(4, 0))
        tk.Label(r6, text="位置", font=body_f, bg=WHITE, width=8, anchor="w").pack(side="left")
        tk.Label(r6, textvariable=self.pos_var, font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left")
        tk.Button(
            r6, text="重置", command=self.reset_pos, font=tiny_f,
            fg=BLACK, bg="#F0F0F0", relief="flat", bd=0, padx=8, pady=2, cursor="hand2",
        ).pack(side="left", padx=8)
        tk.Label(r6, text="按住歌词或左侧 ⠿ 拖动", font=tiny_f, bg=WHITE, fg=MUTE).pack(side="left")

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
            text="文字颜色=填充；描边可单独改色/关。隐蔽度只调整体透明，不会把白色乘成黑色。",
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
        self.style.bold = bool(self.bold_var.get())
        self.style.show_grip = bool(self.grip_var.get())
        self.style.text_color = self.color_var.get()
        self.style.outline_color = self.outline_color_var.get()
        self.style.outline_width = int(self.outline_width_var.get())
        self.style.show_meta = False
        self.style.show_next = False
        self.style.show_rail = False
        self.style.offset_ms = int(self.offset_var.get())
        self.style.overlay_enabled = bool(self.overlay_enabled)
        self.style.height = max(28, int(self.style.font_size) * 2 + 16)
        save_style(self.style)
        self.overlay.style = self.style
        self.overlay.apply_style(rebuild=rebuild)
        self.color_preview.config(bg=self.style.text_color)
        if hasattr(self, "outline_preview"):
            self.outline_preview.config(bg=self.style.outline_color)
        if hasattr(self, "size_label"):
            self.size_label.config(text=str(self.style.font_size))
        self.pos_var.set(self._pos_text())

    def _on_style(self) -> None:
        self._commit_style(rebuild=False)

    def _on_style_rebuild(self) -> None:
        self._commit_style(rebuild=True)

    def apply_preset(self, kind: str) -> None:
        if kind == "stealth":
            # 摸鱼：略淡，但必须带描边，否则白字在浅色壁纸上等于隐身
            self.alpha_var.set(0.72)
            self.size_var.set(14)
            self.bold_var.set(False)
            self.grip_var.set(True)
            self.vertical_var.set(False)
            self.color_var.set("#F2F2F2")
            self.outline_width_var.set(1)
            self.outline_color_var.set("#111111")
        else:
            self.alpha_var.set(0.95)
            self.size_var.set(28)
            self.bold_var.set(True)
            self.grip_var.set(True)
            self.vertical_var.set(False)
            self.color_var.set(WHITE)
            self.outline_width_var.set(2)
            self.outline_color_var.set("#000000")
        self._commit_style(rebuild=True)

    def pick_color(self) -> None:
        rgb, hx = colorchooser.askcolor(color=self.color_var.get(), title="选择歌词填充色")
        if hx:
            self.color_var.set(hx.upper())
            self._commit_style()

    def pick_outline_color(self) -> None:
        rgb, hx = colorchooser.askcolor(
            color=self.outline_color_var.get(), title="选择描边颜色"
        )
        if hx:
            self.outline_color_var.set(hx.upper())
            if int(self.outline_width_var.get()) <= 0:
                self.outline_width_var.set(1)
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

        age_ms = (time.time() - st.received_at) * 1000 if st.received_at else 99999
        show = bool(st.title) and age_ms <= STALE_MS and (
            st.playing or age_ms <= HIDE_AFTER_PAUSE_MS
        )
        if not self.overlay_enabled:
            show = False
        holding = (not clients) and bool(st.title) and age_ms <= STALE_MS and st.playing

        self.ip_var.set(f"{local_ip()}:{PORT}")
        if clients:
            self.link_var.set("手机已连接")
        elif holding:
            self.link_var.set("手机暂离·续显")
        else:
            self.link_var.set("等待手机连接")
        self.clients_var.set(str(clients) if clients else ("…" if holding else "0"))
        if clients and not st.title:
            tip = "已连接但未收到歌曲（手机若显示重连中：停同步再开，或装 1.5.2）"
            self.status_var.set(tip if ok else f"服务异常 · {status}")
        else:
            self.status_var.set(status if ok else f"服务异常 · {status}")
        self.lyric_on_var.set("ON" if show else "OFF")
        self._link_badge.config(
            bg=BLACK if clients else ("#666666" if holding else "#DDDDDD"),
            fg=WHITE if (clients or holding) else MUTE,
        )
        self._lyric_badge.config(bg=BLACK if show else "#DDDDDD", fg=WHITE if show else MUTE)

        if st.title:
            self.track_var.set(st.title)
            if lines:
                pos = st.position_ms
                if st.playing and st.sent_at:
                    pos += max(0, int(time.time() * 1000) - int(st.sent_at))
                elif st.playing and st.received_at:
                    pos += int(max(0.0, (time.time() - st.received_at) * 1000))
                _, cur, nxt = current_line(lines, pos)
                self.line_var.set(cur or nxt or "…")
            else:
                self.line_var.set(f"{st.artist} · 未匹配到歌词" if st.artist else "未匹配到歌词")
        else:
            self.track_var.set("—")
            if clients:
                self.line_var.set("等手机推送曲目（重连中时不会推）")
            else:
                self.line_var.set("等待手机播放")

        self.after(200, self.refresh_ui)

    def on_close(self) -> None:
        self._commit_style()
        if self._hotkey:
            self._hotkey.stop()
            self._hotkey = None
        self.destroy()


def main() -> None:
    if not ensure_single_instance():
        app_log("second launch blocked: already running")
        try:
            focus_existing_instance()
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(
                "歌词桥",
                "歌词桥已经在运行。\n\n已尝试把旧窗口置前。\n若桌面上看不到，到任务栏点「歌词桥」，或结束旧进程后再开。",
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    socket.setdefaulttimeout(5)
    model = AppModel()
    app_log("==== client start pid=%s ====" % os.getpid())
    app_log("ip=" + ", ".join(all_ipv4()) + f" port={PORT}")
    app_log(f"log file: {log_path()}")

    def _thread() -> None:
        try:
            asyncio.run(run_server(model))
        except OSError as e:
            app_log(f"server bind failed: {e}")
            with model.lock:
                model.server_ok = False
                model.status = f"端口 {PORT} 被占用：{e}"
        except Exception as e:
            app_log(f"server crashed: {e}\n{traceback.format_exc()}")
            with model.lock:
                model.server_ok = False
                model.status = f"服务失败：{e}"

    threading.Thread(target=_thread, daemon=True).start()
    try:
        DesktopClient(model).mainloop()
    except Exception:
        app_log("ui crashed:\n" + traceback.format_exc())
        raise
    finally:
        app_log("==== client exit ====")


if __name__ == "__main__":
    main()
