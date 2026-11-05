"""Local self-test for lyrics bridge PC logic (no phone required)."""

from __future__ import annotations

import asyncio
import sys
import time
import tkinter as tk

from lyrics import LyricLine, current_line, detect_source, fetch_lyrics
from qq_lyrics import fetch_qq_lyrics
from style_prefs import LyricStyle


def ok(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise SystemExit(1)


async def test_qq_match() -> None:
    lines = await fetch_qq_lyrics("柯柯柯啊", "寂寞才说爱")
    ok("QQ词库能拉到《寂寞才说爱》", len(lines) >= 10, f"{len(lines)} 行")
    # 应有真实歌词句，不只是歌名头
    body = [x for x in lines if x.time_ms > 5000]
    ok("含带时间轴的正文句", len(body) >= 5, f"{len(body)} 句")


async def test_package_routing() -> None:
    ok("包名识别 QQ", detect_source("com.tencent.qqmusic") == "qqmusic")
    ok("包名识别 B站", detect_source("tv.danmaku.bili") == "bilibili")
    ok("包名识别番茄", detect_source("com.dragon.read") == "fanqie")
    lines = await fetch_lyrics(
        "柯柯柯啊", "寂寞才说爱", package="com.tencent.qqmusic"
    )
    ok("带 package 拉取仍有词", len(lines) >= 10, f"{len(lines)} 行")


def test_progress_estimate() -> None:
    # 复用 overlay 内逻辑的精简版
    class St:
        playing = True
        position_ms = 10000
        sent_at = int(time.time() * 1000) - 800  # 800ms ago
        received_at = time.time()

    now = int(time.time() * 1000)
    lag = now - St.sent_at
    est = St.position_ms + lag
    ok("sent_at 补偿会往前推", est >= 10700, f"est={est}")

    lines = [
        LyricLine(0, "A"),
        LyricLine(5000, "B"),
        LyricLine(12000, "C"),
    ]
    _, cur, _ = current_line(lines, 10800)
    ok("进度到 10.8s 应对上接近的句", cur in ("B", "C"), f"cur={cur}")


def test_overlay_lyrics_only() -> None:
    from client import AppModel, LyricsOverlay

    root = tk.Tk()
    root.withdraw()
    model = AppModel()
    style = LyricStyle(show_meta=False, show_next=False, show_rail=False, font_size=28)
    ov = LyricsOverlay(root, model, style)
    kids = ov.shell.winfo_children()
    labels = [w for w in kids if isinstance(w, tk.Label)]
    ok("桌面层只有歌词 Label", len(labels) == 1, f"labels={len(labels)}")
    ok("没有「词」侧栏控件", not any(getattr(w, "cget", lambda *_: "")("text") == "词" for w in labels))
    ov._set_lyric("测试一句歌词")
    ok("能设置当前歌词", ov.current.cget("text") == "测试一句歌词")

    # 无词不应显示
    show = ov._should_show(
        type("S", (), {
            "title": "寂寞才说爱",
            "playing": True,
            "received_at": time.time(),
            "position_ms": 0,
            "sent_at": 0,
        })(),
        has_lyrics=False,
    )
    ok("无匹配歌词时桌面层不显示", show is False)

    show2 = ov._should_show(
        type("S", (), {
            "title": "寂寞才说爱",
            "playing": True,
            "received_at": time.time(),
            "position_ms": 0,
            "sent_at": 0,
        })(),
        has_lyrics=True,
    )
    ok("有词且在播时应显示", show2 is True)
    ov.destroy()
    root.destroy()


async def main() -> None:
    print("=== 歌词桥本地自测 ===")
    await test_qq_match()
    await test_package_routing()
    test_progress_estimate()
    test_overlay_lyrics_only()
    print("=== 全部 PASS（本地可验证项）===")
    print("未覆盖（需真机）：切应用保活、息屏、通知权限、实机 WebSocket")


if __name__ == "__main__":
    asyncio.run(main())
