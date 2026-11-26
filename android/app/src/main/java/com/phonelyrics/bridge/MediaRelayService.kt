package com.phonelyrics.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import org.json.JSONObject
import java.net.URI
import java.util.concurrent.atomic.AtomicBoolean

class MediaRelayService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var client: WebSocketClient? = null
    private var pcHost: String = ""
    private val running = AtomicBoolean(false)
    private var lastPayload: String = ""
    private var lastStopSent = false

    private val tick = object : Runnable {
        override fun run() {
            if (!running.get()) return
            pushState()
            handler.postDelayed(this, 500)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelfSafe()
                return START_NOT_STICKY
            }
            else -> {
                pcHost = intent?.getStringExtra(EXTRA_HOST)?.trim().orEmpty()
                if (pcHost.isEmpty()) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                startForeground(NOTIF_ID, buildNotification("正在连接 $pcHost…"))
                running.set(true)
                connect()
                handler.removeCallbacks(tick)
                handler.post(tick)
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running.set(false)
        handler.removeCallbacks(tick)
        try {
            client?.close()
        } catch (_: Exception) {
        }
        client = null
        super.onDestroy()
    }

    private fun stopSelfSafe() {
        running.set(false)
        handler.removeCallbacks(tick)
        sendStop()
        try {
            client?.close()
        } catch (_: Exception) {
        }
        client = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun connect() {
        try {
            client?.close()
        } catch (_: Exception) {
        }

        val uri = URI("ws://$pcHost:$PORT")
        val ws = object : WebSocketClient(uri) {
            override fun onOpen(handshakedata: ServerHandshake?) {
                updateNotification("已连接 $pcHost")
                broadcastStatus("已连接 $pcHost")
                lastPayload = ""
                pushState(force = true)
            }

            override fun onMessage(message: String?) {}

            override fun onClose(code: Int, reason: String?, remote: Boolean) {
                updateNotification("连接断开，重试中…")
                broadcastStatus("连接断开，3 秒后重试")
                if (running.get()) {
                    handler.postDelayed({ if (running.get()) connect() }, 3000)
                }
            }

            override fun onError(ex: Exception?) {
                broadcastStatus("连接错误: ${ex?.message ?: "unknown"}")
            }
        }
        client = ws
        Thread {
            try {
                ws.connectBlocking()
            } catch (e: Exception) {
                broadcastStatus("无法连接: ${e.message}")
                if (running.get()) {
                    handler.postDelayed({ if (running.get()) connect() }, 3000)
                }
            }
        }.start()
    }

    private fun preferredController(): MediaController? {
        val mgr = getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
        val cn = ComponentName(this, SessionNotificationListener::class.java)
        val sessions = try {
            mgr.getActiveSessions(cn)
        } catch (e: SecurityException) {
            broadcastStatus("请先开启通知使用权")
            emptyList()
        }

        if (sessions.isEmpty()) return null

        fun isMusicApp(pkg: String): Boolean {
            val p = pkg.lowercase()
            return p.contains("qqmusic") ||
                p.contains("luna.music") ||
                p.contains("sodamusic") ||
                p.contains("cloudmusic") ||
                p.contains("kugou")
        }

        val musicPlaying = sessions.firstOrNull { ctrl ->
            isMusicApp(ctrl.packageName) &&
                (ctrl.playbackState?.state == PlaybackState.STATE_PLAYING ||
                    ctrl.playbackState?.state == PlaybackState.STATE_BUFFERING)
        }
        if (musicPlaying != null) return musicPlaying

        val musicAny = sessions.firstOrNull { isMusicApp(it.packageName) }
        if (musicAny != null) return musicAny

        return sessions.firstOrNull { ctrl ->
            val st = ctrl.playbackState?.state
            st == PlaybackState.STATE_PLAYING || st == PlaybackState.STATE_BUFFERING
        } ?: sessions.first()
    }

    private fun pushState(force: Boolean = false) {
        val ctrl = preferredController()
        val meta = ctrl?.metadata
        val pb = ctrl?.playbackState

        val title = meta?.getString(MediaMetadata.METADATA_KEY_TITLE)?.trim().orEmpty()
        val artist = (
            meta?.getString(MediaMetadata.METADATA_KEY_ARTIST)
                ?: meta?.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST)
            )?.trim().orEmpty()
        val album = meta?.getString(MediaMetadata.METADATA_KEY_ALBUM)?.trim().orEmpty()
        val duration = meta?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0L
        val position = pb?.position?.coerceAtLeast(0L) ?: 0L
        val state = pb?.state ?: PlaybackState.STATE_NONE
        val playing = state == PlaybackState.STATE_PLAYING || state == PlaybackState.STATE_BUFFERING

        if (title.isEmpty() || state == PlaybackState.STATE_NONE || state == PlaybackState.STATE_STOPPED) {
            if (!lastStopSent || force) {
                sendStop()
                lastStopSent = true
                lastPayload = ""
            }
            return
        }

        lastStopSent = false
        val payload = JSONObject()
            .put("type", "state")
            .put("playing", playing)
            .put("title", title)
            .put("artist", artist)
            .put("album", album)
            .put("position_ms", position)
            .put("duration_ms", duration)
            .put("package", ctrl?.packageName ?: "")
            .put("sent_at", System.currentTimeMillis())
            .toString()

        if (!force && payload == lastPayload) return
        // Always refresh position while playing even if other fields same:
        val sameTrackPaused = !playing && lastPayload.contains("\"title\":\"$title\"")
        if (!force && !playing && sameTrackPaused && lastPayload.contains("\"playing\":false")) {
            // still send occasionally so PC knows we're alive
        }

        if (client?.isOpen == true) {
            try {
                client?.send(payload)
                lastPayload = payload
                val tip = if (playing) "同步中: $title" else "已暂停: $title"
                updateNotification(tip)
                broadcastStatus(tip, title, artist)
            } catch (_: Exception) {
            }
        }
    }

    private fun sendStop() {
        if (client?.isOpen != true) return
        try {
            client?.send(JSONObject().put("type", "stop").toString())
            updateNotification("等待播放…")
            broadcastStatus("等待 QQ 音乐播放")
        } catch (_: Exception) {
        }
    }

    private fun buildNotification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(CHANNEL_ID, "歌词同步", NotificationManager.IMPORTANCE_LOW)
        nm.createNotificationChannel(channel)

        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this,
            1,
            Intent(this, MediaRelayService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("歌词桥")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(open)
            .addAction(0, "停止", stop)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }

    private fun broadcastStatus(text: String, title: String = "", artist: String = "") {
        sendBroadcast(
            Intent(ACTION_STATUS)
                .setPackage(packageName)
                .putExtra(EXTRA_STATUS, text)
                .putExtra(EXTRA_TITLE, title)
                .putExtra(EXTRA_ARTIST, artist),
        )
    }

    companion object {
        const val PORT = 8765
        const val EXTRA_HOST = "host"
        const val EXTRA_STATUS = "status"
        const val EXTRA_TITLE = "title"
        const val EXTRA_ARTIST = "artist"
        const val ACTION_STOP = "com.phonelyrics.bridge.STOP"
        const val ACTION_STATUS = "com.phonelyrics.bridge.STATUS"
        private const val CHANNEL_ID = "lyrics_bridge"
        private const val NOTIF_ID = 42

        fun start(context: Context, host: String) {
            val i = Intent(context, MediaRelayService::class.java).putExtra(EXTRA_HOST, host)
            context.startForegroundService(i)
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, MediaRelayService::class.java).setAction(ACTION_STOP),
            )
        }
    }
}
