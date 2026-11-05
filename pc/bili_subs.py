"""Best-effort Bilibili CC / AI subtitle fetch by video title."""

from __future__ import annotations

import re

import httpx

from lyrics import LyricLine

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}


def _clean_title(title: str) -> str:
    t = title.strip()
    # MediaSession titles often look like: "标题 - UP主" or with bilibili suffix
    t = re.sub(r"\s*[-_|｜]\s*哔哩哔哩.*$", "", t)
    t = re.sub(r"\s*[-_|｜]\s*bilibili.*$", "", t, flags=re.I)
    return t.strip()


async def fetch_bili_subtitles(title: str, artist: str = "") -> list[LyricLine]:
    q = _clean_title(title)
    if not q:
        return []

    async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS, follow_redirects=True) as client:
        try:
            search = await client.get(
                "https://api.bilibili.com/x/web-interface/search/type",
                params={"search_type": "video", "keyword": q, "page": 1},
            )
            search.raise_for_status()
            data = search.json()
            results = (data.get("data") or {}).get("result") or []
            if not results:
                return []

            # Prefer title containment match
            pick = results[0]
            ql = q.lower()
            for item in results[:8]:
                it = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).lower()
                if ql[:12] and ql[:12] in it:
                    pick = item
                    break
                if artist and artist.lower() in str(item.get("author") or "").lower():
                    pick = item
                    break

            bvid = pick.get("bvid")
            if not bvid:
                return []

            view = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
            )
            view.raise_for_status()
            vdata = (view.json().get("data") or {})
            cid = vdata.get("cid")
            aid = vdata.get("aid")
            if not cid:
                return []

            player = await client.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": bvid, "cid": cid, "aid": aid},
            )
            player.raise_for_status()
            pdata = player.json().get("data") or {}
            subs = ((pdata.get("subtitle") or {}).get("subtitles")) or []
            if not subs:
                return []

            # Prefer Chinese tracks
            track = None
            for s in subs:
                lan = str(s.get("lan") or "")
                if "zh" in lan:
                    track = s
                    break
            track = track or subs[0]
            url = track.get("subtitle_url") or track.get("subtitle_url_v2") or ""
            if not url or url in ("/", ""):
                return []
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://api.bilibili.com" + url

            raw = await client.get(url)
            raw.raise_for_status()
            body = (raw.json().get("body")) or []
            lines: list[LyricLine] = []
            for row in body:
                content = str(row.get("content") or "").strip()
                if not content:
                    continue
                from_sec = float(row.get("from") or 0)
                lines.append(LyricLine(int(from_sec * 1000), content))
            lines.sort(key=lambda x: x.time_ms)
            return lines
        except Exception:
            return []
