"""Fetch synced lyrics from LRCLIB (free, no key)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

_LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


@dataclass
class LyricLine:
    time_ms: int
    text: str


def parse_lrc(raw: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for row in raw.splitlines():
        m = _LRC_LINE.match(row.strip())
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = float(m.group(2))
        text = m.group(3).strip()
        if not text:
            continue
        lines.append(LyricLine(int((minutes * 60 + seconds) * 1000), text))
    lines.sort(key=lambda x: x.time_ms)
    return lines


def detect_source(package: str) -> str:
    p = (package or "").lower()
    if "qqmusic" in p or "tencent.qqmusic" in p:
        return "qqmusic"
    if "bili" in p or "danmaku" in p:
        return "bilibili"
    if "dragon.read" in p or "fanqie" in p or "tomato" in p:
        return "fanqie"
    return "generic"


async def fetch_lyrics(
    artist: str,
    title: str,
    album: str = "",
    duration_ms: int = 0,
    package: str = "",
) -> list[LyricLine]:
    if not title.strip():
        return []

    source = detect_source(package)

    # 番茄听书：通常没有公开逐句时间轴 → 桌面显示章节/书名
    if source == "fanqie":
        text = title.strip()
        if artist.strip():
            return [
                LyricLine(0, text),
                LyricLine(max(1000, duration_ms - 1000) if duration_ms > 2000 else 10_000, artist.strip()),
            ]
        return [LyricLine(0, text)]

    # B 站：尽量拉 CC/AI 字幕；失败则回退为标题
    if source == "bilibili":
        try:
            from bili_subs import fetch_bili_subtitles

            bili_lines = await fetch_bili_subtitles(title, artist)
            if bili_lines:
                return bili_lines
        except Exception:
            pass
        return [LyricLine(0, title.strip())]

    # QQ 音乐 / 其它：QQ 词库 → LRCLIB
    if source in ("qqmusic", "generic"):
        try:
            from qq_lyrics import fetch_qq_lyrics

            qq_lines = await fetch_qq_lyrics(artist, title)
            if qq_lines:
                return qq_lines
        except Exception:
            pass

    params: dict[str, str | int] = {
        "track_name": title.strip(),
        "artist_name": artist.strip() or "Unknown",
    }
    if album.strip():
        params["album_name"] = album.strip()
    if duration_ms > 0:
        params["duration"] = max(1, duration_ms // 1000)

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get("https://lrclib.net/api/get", params=params)
            if r.status_code == 404:
                r = await client.get(
                    "https://lrclib.net/api/search",
                    params={"q": f"{artist} {title}".strip()},
                )
                r.raise_for_status()
                items = r.json()
                if not items:
                    return [LyricLine(0, title.strip())] if source != "qqmusic" else []
                best = items[0]
                synced = best.get("syncedLyrics") or ""
                if not synced and best.get("id"):
                    g = await client.get(f"https://lrclib.net/api/get/{best['id']}")
                    if g.status_code == 200:
                        synced = g.json().get("syncedLyrics") or ""
                parsed = parse_lrc(synced)
                return parsed or ([LyricLine(0, title.strip())] if source != "qqmusic" else [])
            r.raise_for_status()
            data = r.json()
            synced = data.get("syncedLyrics") or ""
            if not synced:
                parsed = parse_lrc(data.get("plainLyrics") or "")
            else:
                parsed = parse_lrc(synced)
            return parsed or ([LyricLine(0, title.strip())] if source != "qqmusic" else [])
        except Exception:
            return [LyricLine(0, title.strip())] if source != "qqmusic" else []

def current_line(lines: list[LyricLine], position_ms: int) -> tuple[int, str, str]:
    """Return (index, current, next)."""
    if not lines:
        return -1, "", ""
    idx = -1
    for i, line in enumerate(lines):
        if line.time_ms <= position_ms:
            idx = i
        else:
            break
    if idx < 0:
        return -1, "", lines[0].text
    nxt = lines[idx + 1].text if idx + 1 < len(lines) else ""
    return idx, lines[idx].text, nxt
