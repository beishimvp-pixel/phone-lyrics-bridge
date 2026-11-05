"""Best-effort QQ Music lyric fallback for Chinese tracks."""

from __future__ import annotations

import json
import re
from base64 import b64decode

import httpx

from lyrics import LyricLine, parse_lrc

_HEADERS = {
    "Referer": "https://y.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _strip_jsonp(text: str) -> dict:
    text = text.strip()
    if text.startswith("callback(") or text.startswith("MusicJsonCallback("):
        text = text[text.find("(") + 1 : text.rfind(")")]
    elif "(" in text and text.endswith(")"):
        text = text[text.find("(") + 1 : -1]
    return json.loads(text)


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\s\-_/｜|·•.，,、()（）\[\]【】]+", "", s)
    s = re.sub(r"(官方|伴奏|live|dj|版|remix)$", "", s)
    return s


def _score_song(item: dict, title: str, artist: str) -> int:
    t = _norm(str(item.get("songname") or item.get("title") or ""))
    a = _norm(str(item.get("singer", [{}])[0].get("name") if item.get("singer") else "") or "")
    if not a and item.get("singer"):
        try:
            a = _norm(",".join(x.get("name") or "" for x in item.get("singer") or []))
        except Exception:
            a = ""
    nt, na = _norm(title), _norm(artist)
    score = 0
    if nt and t == nt:
        score += 100
    elif nt and (nt in t or t in nt):
        score += 60
    if na and a and (na in a or a in na or na == a):
        score += 40
    if nt and t and t.startswith(nt[: max(2, len(nt) // 2)]):
        score += 10
    return score


async def _lyric_by_mid(client: httpx.AsyncClient, songmid: str) -> list[LyricLine]:
    lyric_resp = await client.get(
        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
        params={
            "songmid": songmid,
            "format": "json",
            "nobase64": 0,
            "g_tk": 5381,
        },
    )
    lyric_resp.raise_for_status()
    raw = lyric_resp.text
    payload = json.loads(raw) if raw.lstrip().startswith("{") else _strip_jsonp(raw)
    lyric_b64 = payload.get("lyric") or ""
    if not lyric_b64:
        return []
    try:
        lrc = b64decode(lyric_b64).decode("utf-8", errors="ignore")
    except Exception:
        lrc = lyric_b64
    # Apply [offset:] if present, then strip tags
    offset_ms = 0
    m = re.search(r"\[offset:\s*(-?\d+)\]", lrc, flags=re.I)
    if m:
        offset_ms = int(m.group(1))
    lrc = re.sub(r"\[(ti|ar|al|by|offset):[^\]]*]", "", lrc)
    lines = parse_lrc(lrc)
    if offset_ms and lines:
        lines = [LyricLine(max(0, x.time_ms + offset_ms), x.text) for x in lines]
    return lines


async def fetch_qq_lyrics(artist: str, title: str) -> list[LyricLine]:
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return []

    queries = []
    for q in (f"{title} {artist}".strip(), title, f"{title} {artist.split('/')[0]}".strip()):
        if q and q not in queries:
            queries.append(q)

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS, follow_redirects=True) as client:
        try:
            best_mid = ""
            best_score = -1
            for q in queries:
                search = await client.get(
                    "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                    params={
                        "w": q,
                        "format": "json",
                        "p": 1,
                        "n": 10,
                        "cr": 1,
                    },
                )
                if search.status_code != 200:
                    continue
                data = search.json()
                songs = (data.get("data") or {}).get("song", {}).get("list") or []
                for item in songs:
                    sc = _score_song(item, title, artist)
                    mid = item.get("songmid") or item.get("mid") or ""
                    if sc > best_score and mid:
                        best_score = sc
                        best_mid = mid
                if best_score >= 100:
                    break

            if not best_mid or best_score < 40:
                return []

            return await _lyric_by_mid(client, best_mid)
        except Exception:
            return []
