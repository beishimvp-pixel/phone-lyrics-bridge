"""Desktop lyric style preferences + screen color picker."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import ctypes


@dataclass
class LyricStyle:
    alpha: float = 0.85  # 整窗透明度（不改字色）；1=不透
    font_size: int = 12  # 摸鱼默认小字；可调 8~48
    text_color: str = "#FFFFFF"  # 选什么就画什么，不再被隐蔽度改色
    outline_color: str = "#000000"  # 描边颜色，可单独选
    outline_width: int = 0  # 0=无描边；1~3
    bold: bool = False  # 细体更不显眼
    vertical: bool = False
    # 桌面层只要当前歌词；以下三项永久关闭（兼容旧配置字段）
    show_next: bool = False
    show_meta: bool = False
    show_rail: bool = False
    overlay_enabled: bool = True  # 用户可随时关/开桌面歌词
    offset_ms: int = 0  # 歌词进度微调，正数=歌词更早出现
    show_grip: bool = True  # 小拖动手柄，小字时也好拖
    x: int | None = None
    y: int | None = None
    width: int = 520
    height: int = 48
    hotkey: str = "Ctrl+Alt+H"  # 全局：显示/隐藏桌面歌词


def prefs_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    folder = Path(base) / "LyricsBridge"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def prefs_path() -> Path:
    return prefs_dir() / "lyric_style.json"


def log_path() -> Path:
    return prefs_dir() / "client.log"


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
