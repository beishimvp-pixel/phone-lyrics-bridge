"""Simulate Android client — run while app.py is up, to test desktop lyrics without a phone."""

from __future__ import annotations

import asyncio
import json
import time

import websockets

HOST = "127.0.0.1"
PORT = 8765


async def main() -> None:
    uri = f"ws://{HOST}:{PORT}"
    async with websockets.connect(uri) as ws:
        title = "晴天"
        artist = "周杰伦"
        print(f"connected, simulating: {artist} - {title}")
        for i in range(40):
            payload = {
                "type": "state",
                "playing": True,
                "title": title,
                "artist": artist,
                "album": "叶惠美",
                "position_ms": i * 1000,
                "duration_ms": 269000,
                "sent_at": int(time.time() * 1000),
            }
            await ws.send(json.dumps(payload, ensure_ascii=False))
            await asyncio.sleep(1)
        await ws.send(json.dumps({"type": "stop"}))
        print("sent stop, lyrics should hide")
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
