# Phone Lyrics Bridge 协议

手机 → 电脑，WebSocket，文本帧，JSON。

## 连接

```
ws://<电脑局域网IP>:8765
```

连接成功后，手机应立刻发一条当前状态；之后每 500ms～1s 推送一次（进度变化或状态变化时更勤）。

## 状态消息

```json
{
  "type": "state",
  "playing": true,
  "title": "歌名",
  "artist": "歌手",
  "album": "专辑",
  "position_ms": 12500,
  "duration_ms": 240000,
  "package": "com.tencent.qqmusic",
  "sent_at": 1730000000123
}
```

| 字段 | 说明 |
|------|------|
| `playing` | 是否正在播放 |
| `position_ms` | 当前进度（毫秒） |
| `sent_at` | 手机发出时的 epoch 毫秒，用于电脑补偿网络延迟 |
| `package` | 可选，播放器包名 |

## 停止 / 清空

任选其一：

```json
{ "type": "stop" }
```

或：

```json
{
  "type": "state",
  "playing": false,
  "title": "",
  "artist": "",
  "position_ms": 0,
  "duration_ms": 0,
  "sent_at": 1730000000123
}
```

电脑在 `playing=false` 或收到 `stop` 后隐藏歌词；超过约 4 秒没收消息也会隐藏。
