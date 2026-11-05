"""Global hotkey via Win32 RegisterHotKey."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.windll.user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

VK_MAP = {
    "BACK": 0x08,
    "TAB": 0x09,
    "RETURN": 0x0D,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "DELETE": 0x2E,
    "DEL": 0x2E,
}
for i in range(1, 13):
    VK_MAP[f"F{i}"] = 0x70 + i - 1
for c in "0123456789":
    VK_MAP[c] = ord(c)
for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_MAP[c] = ord(c)


def parse_hotkey(text: str) -> tuple[int, int] | None:
    """Return (modifiers, vk) or None."""
    if not text or not text.strip():
        return None
    parts = [p.strip() for p in text.replace("-", "+").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        u = p.upper()
        if u in ("CTRL", "CONTROL"):
            mods |= MOD_CONTROL
        elif u in ("ALT", "MENU"):
            mods |= MOD_ALT
        elif u == "SHIFT":
            mods |= MOD_SHIFT
        elif u in ("WIN", "WINDOWS", "META", "CMD"):
            mods |= MOD_WIN
        else:
            key = u
    if not key or key not in VK_MAP:
        return None
    if mods == 0:
        # Require at least one modifier to avoid eating normal typing.
        return None
    return mods | MOD_NOREPEAT, VK_MAP[key]


def format_hotkey(mods: int, vk: int) -> str:
    names: list[str] = []
    if mods & MOD_CONTROL:
        names.append("Ctrl")
    if mods & MOD_ALT:
        names.append("Alt")
    if mods & MOD_SHIFT:
        names.append("Shift")
    if mods & MOD_WIN:
        names.append("Win")
    key_name = None
    for k, v in VK_MAP.items():
        if v == vk and len(k) == 1:
            key_name = k
            break
    if key_name is None:
        for k, v in VK_MAP.items():
            if v == vk:
                key_name = k.title()
                break
    names.append(key_name or hex(vk))
    return "+".join(names)


class GlobalHotkey:
    def __init__(self, hotkey: str, callback: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._win_tid = 0
        self._stop = threading.Event()
        self._ok = False
        self.error = ""

    def start(self) -> bool:
        self.stop()
        parsed = parse_hotkey(self.hotkey)
        if not parsed:
            self.error = "快捷键无效（需含 Ctrl/Alt/Shift + 主键）"
            self._ok = False
            return False
        mods, vk = parsed
        self._stop.clear()
        self.error = ""
        self._ok = False
        self._thread = threading.Thread(
            target=self._loop, args=(mods, vk), daemon=True, name="GlobalHotkey"
        )
        self._thread.start()
        for _ in range(50):
            if self._ok or self.error:
                break
            threading.Event().wait(0.02)
        return self._ok

    def stop(self) -> None:
        self._stop.set()
        if self._win_tid:
            user32.PostThreadMessageW(self._win_tid, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._win_tid = 0

    def _loop(self, mods: int, vk: int) -> None:
        self._win_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        hotkey_id = 1
        if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
            self.error = "注册失败（可能被其它软件占用）"
            self._ok = False
            return
        self.error = ""
        self._ok = True
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                    try:
                        self.callback()
                    except Exception:
                        pass
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
            self._ok = False
