"""Stability test for lyrics-bridge WebSocket (phone-side behavior).

Simulates 1.5.2-style sync: one long-lived socket, state every 500ms.
Fails if the connection drops during the run.
Also asserts source no longer contains 1.5.1 reconnect-storm patterns.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import websockets

HOST = "127.0.0.1"
PORT = 8765
DURATION_S = 45
INTERVAL_S = 0.5
ROOT = Path(__file__).resolve().parents[1]
INDEX_VUE = ROOT / "hbuilder-app" / "pages" / "index" / "index.vue"
INDEX_UTS = (
    ROOT
    / "hbuilder-app"
    / "uni_modules"
    / "lyrics-notify-listener"
    / "utssdk"
    / "app-android"
    / "index.uts"
)


def check_source() -> list[str]:
    errs: list[str] = []
    vue = INDEX_VUE.read_text(encoding="utf-8")
    uts = INDEX_UTS.read_text(encoding="utf-8")

    if "multiple: true" in vue:
        errs.append("index.vue still has multiple: true (1.5.1 storm)")
    if "markSocketDown" in vue:
        errs.append("index.vue still has markSocketDown (1.5.1 storm)")
    if "fail:" in vue and "发送失败" in vue:
        errs.append("index.vue still reconnects on send fail callback")
    if "KEEPALIVE_REFRESH_MS" not in vue:
        errs.append("index.vue missing KEEPALIVE_REFRESH_MS throttle")
    if "OPEN_WATCHDOG_MS" not in vue:
        errs.append("index.vue missing OPEN_WATCHDOG_MS (1.5.3)")
    if "nativeWs" not in vue or "new WebSocket" not in vue:
        errs.append("index.vue missing native WebSocket path (1.5.3)")
    if "type: 'hello'" not in vue and 'type: "hello"' not in vue and "type: 'hello'" not in vue.replace('"', "'"):
        if "hello" not in vue:
            errs.append("index.vue missing hello handshake")
    if "export function startKeepAlive" not in uts:
        errs.append("UTS missing direct export startKeepAlive (Vue3 pack requirement)")
    if "export { startKeepAlive, stopKeepAlive }" in uts:
        errs.append("UTS still uses re-export (breaks Vue3 pack)")
    return errs


async def sustained_sync() -> tuple[int, int, float]:
    """Return (sent, disconnects, elapsed_s)."""
    uri = f"ws://{HOST}:{PORT}"
    sent = 0
    disconnects = 0
    t0 = time.monotonic()
    deadline = t0 + DURATION_S
    pos = 0

    while time.monotonic() < deadline:
        try:
            async with websockets.connect(uri, open_timeout=5, ping_interval=20, ping_timeout=60) as ws:
                opened = time.monotonic()
                while time.monotonic() < deadline:
                    payload = {
                        "type": "state",
                        "playing": True,
                        "title": "La La La",
                        "artist": "Naughty Boy",
                        "album": "test",
                        "position_ms": pos,
                        "duration_ms": 220000,
                        "package": "com.tencent.qqmusic",
                        "source": "qqmusic",
                        "sent_at": int(time.time() * 1000),
                    }
                    await ws.send(json.dumps(payload, ensure_ascii=False))
                    sent += 1
                    pos += int(INTERVAL_S * 1000)
                    await asyncio.sleep(INTERVAL_S)
                # clean stop
                await ws.send(json.dumps({"type": "stop"}))
                held = time.monotonic() - opened
                return sent, disconnects, held
        except Exception as e:
            disconnects += 1
            print(f"[reconnect] disconnect #{disconnects}: {type(e).__name__}: {e}")
            await asyncio.sleep(1.2)
    return sent, disconnects, time.monotonic() - t0


async def main() -> int:
    print("== source checks ==")
    errs = check_source()
    if errs:
        for e in errs:
            print("FAIL:", e)
        return 2
    print("PASS: source patterns OK (1.5.2 stable)")

    print(f"\n== sustained WS sync {DURATION_S}s @ {INTERVAL_S}s ==")
    try:
        sent, disconnects, held = await sustained_sync()
    except Exception as e:
        print("FAIL: could not run against server:", e)
        return 3

    print(f"sent={sent} disconnects={disconnects} held={held:.1f}s")
    if disconnects > 0:
        print("FAIL: connection dropped during sustained sync")
        return 4
    if sent < int(DURATION_S / INTERVAL_S * 0.8):
        print("FAIL: too few messages sent")
        return 5
    print("PASS: sustained sync held without reconnect")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
