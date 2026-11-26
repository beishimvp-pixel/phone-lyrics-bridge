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
  "source": "qqmusic",
  "sent_at": 1730000000123
}
```

| 字段 | 说明 |
|------|------|
| `playing` | 是否正在播放 |
| `position_ms` | 当前进度（毫秒） |
| `sent_at` | 手机发出时的 epoch 毫秒，用于电脑补偿网络延迟 |
| `package` | 可选，播放器包名。常见：`com.tencent.qqmusic`、`com.luna.music`（汽水）、`com.netease.cloudmusic`、`com.kugou.android`、`tv.danmaku.bili`、`com.dragon.read` |
| `source` | 可选，手机所选来源：`qqmusic` / `soda` / `netease` / `kugou` / `bilibili` / `fanqie`（电脑端以 `package` 识别为准） |

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

电脑端行为（以 `pc/client.py` 为准）：

- `playing=false`：暂停后约 **60 秒**再隐藏桌面歌词（避免短暂停顿闪一下就没）
- 收到 `stop` 或清空歌名：按停止处理
- 超过约 **3 分钟**没收到有效状态：视为过期并隐藏
- 用户在主窗口关闭桌面歌词 / 右键隐藏 / 快捷键关闭后：不再自动弹出，直到再次开启
