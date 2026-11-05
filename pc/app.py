"""
Phone Lyrics Bridge — PC side
WebSocket server + always-on-top desktop lyrics.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from lyrics import LyricLine, current_line, fetch_lyrics
from netutil import all_ipv4, local_ip

PORT = 8765
STALE_MS = 4000
HIDE_AFTER_PAUSE_MS = 1200


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


@dataclass
class AppModel:
    state: PlaybackState = field(default_factory=PlaybackState)
    lines: list[LyricLine] = field(default_factory=list)
    status: str = "等待手机连接…"
    clients: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class LyricsOverlay:
    def __init__(self, model: AppModel) -> None:
        self.model = model
        self.root = tk.Tk()
        self.root.title("Phone Lyrics Bridge")
        self.root.attributes("-topmost", True)
        self.root.overrideredirect(True)
        self.root.configure(bg="#101010")
        try:
            self.root.attributes("-alpha", 0.92)
        except tk.TclError:
            pass

        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"720x120+{(sw - 720) // 2}+80")

        self._drag_x = 0
        self._drag_y = 0
        self.root.bind("<ButtonPress-1>", self._start_drag)
        self.root.bind("<B1-Motion>", self._on_drag)

        self.meta = tk.Label(
            self.root,
            text="",
            font=("Segoe UI", 10),
            fg="#9aa0a6",
            bg="#101010",
            anchor="w",
        )
        self.meta.pack(fill="x", padx=18, pady=(10, 0))

        self.current = tk.Label(
            self.root,
            text="等待手机播放…",
            font=("Microsoft YaHei UI", 18, "bold"),
            fg="#f1f3f4",
            bg="#101010",
            wraplength=680,
            justify="center",
        )
        self.current.pack(fill="both", expand=True, padx=18)

        self.nxt = tk.Label(
            self.root,
            text="",
            font=("Microsoft YaHei UI", 11),
            fg="#5f6368",
            bg="#101010",
            wraplength=680,
            justify="center",
        )
        self.nxt.pack(fill="x", padx=18, pady=(0, 12))

        self.status = tk.Label(
            self.root,
            text="",
            font=("Consolas", 9),
            fg="#80868b",
            bg="#101010",
            anchor="w",
        )
        self.status.pack(fill="x", padx=18, pady=(0, 8))

        self._visible = True
        self._tick()

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _estimate_position(self, st: PlaybackState) -> int:
        if not st.playing or not st.received_at:
            return st.position_ms
        # Advance from last sample using local receive time (avoids phone/PC clock skew).
        lag = max(0.0, (time.time() - st.received_at) * 1000)
        return st.position_ms + int(lag)

    def _should_show(self, st: PlaybackState) -> bool:
        if not st.title:
            return False
        age_ms = (time.time() - st.received_at) * 1000 if st.received_at else 99999
        if age_ms > STALE_MS:
            return False
        if not st.playing:
            if age_ms > HIDE_AFTER_PAUSE_MS:
                return False
        return True

    def _tick(self) -> None:
        with self.model.lock:
            st = PlaybackState(**self.model.state.__dict__)
            lines = list(self.model.lines)
            status = self.model.status
            clients = self.model.clients

        show = self._should_show(st)
        if show and not self._visible:
            self.root.deiconify()
            self._visible = True
        elif not show and self._visible:
            self.root.withdraw()
            self._visible = False

        ip = local_ip()
        self.status.config(
            text=f"ws://{ip}:{PORT}  |  已连接 {clients} 台  |  {status}"
        )

        if show:
            pos = self._estimate_position(st)
            _, cur, nxt = current_line(lines, pos)
            meta = f"{st.artist}  ·  {st.title}" if st.artist else st.title
            self.meta.config(text=meta)
            if lines:
                self.current.config(text=cur or "…")
                self.nxt.config(text=nxt)
            else:
                self.current.config(text="未找到同步歌词")
                self.nxt.config(text="正在听 · 歌词源暂无匹配")
        else:
            self.meta.config(text="")
            self.current.config(text="等待手机播放…")
            self.nxt.config(text="")

        self.root.after(80, self._tick)

    def run(self) -> None:
        self.root.mainloop()


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

        msg_type = data.get("type", "state")
        if msg_type == "stop":
            with self.model.lock:
                self.model.state = PlaybackState(received_at=time.time())
                self.model.lines = []
                self.model.status = "已停止"
            return

        title = str(data.get("title") or "").strip()
        artist = str(data.get("artist") or "").strip()
        album = str(data.get("album") or "").strip()
        playing = bool(data.get("playing"))
        position_ms = int(data.get("position_ms") or 0)
        duration_ms = int(data.get("duration_ms") or 0)
        sent_at = int(data.get("sent_at") or int(time.time() * 1000))
        track_key = f"{artist}|{title}".lower()

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
            )
            self.model.status = "播放中" if playing else "已暂停"

        if not title:
            with self.model.lock:
                self.model.lines = []
            return

        if track_key != old_key:
            self._fetch_token += 1
            token = self._fetch_token
            asyncio.create_task(self._load_lyrics(token, artist, title, album, duration_ms))

    async def _load_lyrics(
        self, token: int, artist: str, title: str, album: str, duration_ms: int
    ) -> None:
        with self.model.lock:
            self.model.status = "拉取歌词中…"
            self.model.lines = []
        lines = await fetch_lyrics(artist, title, album, duration_ms)
        if token != self._fetch_token:
            return
        with self.model.lock:
            self.model.lines = lines
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
        await asyncio.Future()


def main() -> None:
    # Allow rebinding quickly during restarts
    socket.setdefaulttimeout(5)
    model = AppModel()
    ips = ", ".join(all_ipv4())
    print(f"Phone Lyrics Bridge")
    print(f"在手机 App 填入电脑地址，例如: {local_ip()}")
    print(f"本机可用 IP: {ips}")
    print(f"端口: {PORT}")

    def _thread() -> None:
        asyncio.run(run_server(model))

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    LyricsOverlay(model).run()


if __name__ == "__main__":
    main()
