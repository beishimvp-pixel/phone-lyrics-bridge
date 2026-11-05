"""Desktop lyric style preferences + screen color picker."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import ctypes


@dataclass
class LyricStyle:
    alpha: float = 0.92  # 0.3 ~ 1.0
    font_size: int = 28
    text_color: str = "#FFFFFF"  # 无底板时默认白字更清晰
    vertical: bool = False
    # 桌面层只要当前歌词；以下三项永久关闭（兼容旧配置字段）
    show_next: bool = False
    show_meta: bool = False
    show_rail: bool = False
    overlay_enabled: bool = True  # 用户可随时关/开桌面歌词
    offset_ms: int = 0  # 歌词进度微调，正数=歌词更早出现
    x: int | None = None
    y: int | None = None
    width: int = 760
    height: int = 96
    hotkey: str = "Ctrl+Alt+H"  # 全局：显示/隐藏桌面歌词


def prefs_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    folder = Path(base) / "LyricsBridge"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "lyric_style.json"


def load_style() -> LyricStyle:
    path = prefs_path()
    if not path.exists():
        return LyricStyle()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        base = LyricStyle()
        for k, v in data.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base
    except Exception:
        return LyricStyle()


def save_style(style: LyricStyle) -> None:
    prefs_path().write_text(
        json.dumps(asdict(style), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def screen_pixel_hex(x: int, y: int) -> str:
    """Pick color under screen coordinates (Windows)."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    try:
        color = gdi32.GetPixel(hdc, int(x), int(y))
    finally:
        user32.ReleaseDC(0, hdc)
    if color < 0:
        return "#111111"
    r = color & 0xFF
    g = (color >> 8) & 0xFF
    b = (color >> 16) & 0xFF
    return f"#{r:02X}{g:02X}{b:02X}"


def verticalize(text: str) -> str:
    chars = [c for c in text if c.strip() or c == " "]
    return "\n".join(chars) if chars else ""
