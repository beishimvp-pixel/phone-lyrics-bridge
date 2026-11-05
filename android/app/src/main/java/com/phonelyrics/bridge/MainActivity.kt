package com.phonelyrics.bridge

import android.Manifest
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.phonelyrics.bridge.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private var syncing = false

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val text = intent?.getStringExtra(MediaRelayService.EXTRA_STATUS) ?: return
            binding.statusText.text = text
            binding.syncStatus.text = text
            // Prefer explicit track extras when present
            val title = intent.getStringExtra(MediaRelayService.EXTRA_TITLE)
            val artist = intent.getStringExtra(MediaRelayService.EXTRA_ARTIST)
            if (!title.isNullOrBlank()) {
                binding.trackTitle.text = title
                binding.trackArtist.text = artist.orEmpty()
                binding.syncLabel.text = getString(R.string.syncing_label)
                binding.syncMark.setBackgroundColor(ContextCompat.getColor(this@MainActivity, R.color.brand_black))
            } else if (text.contains("等待")) {
                binding.trackTitle.text = getString(R.string.waiting_track)
                binding.trackArtist.text = ""
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.ipInput.setText(Prefs.getIp(this))
        showPanel(Panel.CONNECT)

        binding.btnStart.setOnClickListener {
            val ip = binding.ipInput.text?.toString()?.trim().orEmpty()
            if (ip.isEmpty()) {
                binding.statusText.text = "请填写电脑 IP"
                return@setOnClickListener
            }
            Prefs.setIp(this, ip)
            if (!isNotificationServiceEnabled()) {
                showPanel(Panel.PERMIT)
                return@setOnClickListener
            }
            startSync(ip)
        }

        binding.btnGoPermit.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        binding.btnPermitDone.setOnClickListener {
            if (!isNotificationServiceEnabled()) {
                binding.statusText.text = "还没检测到通知使用权，请先在系统里打开"
                return@setOnClickListener
            }
            val ip = Prefs.getIp(this).ifBlank {
                binding.ipInput.text?.toString()?.trim().orEmpty()
            }
            if (ip.isEmpty()) {
                showPanel(Panel.CONNECT)
                binding.statusText.text = "请填写电脑 IP"
                return@setOnClickListener
            }
            startSync(ip)
        }

        binding.btnStop.setOnClickListener {
            MediaRelayService.stop(this)
            syncing = false
            showPanel(Panel.CONNECT)
            binding.statusText.text = getString(R.string.status_idle)
            binding.trackTitle.text = getString(R.string.waiting_track)
            binding.trackArtist.text = ""
        }
    }

    private fun startSync(ip: String) {
        maybeAskPostNotifications()
        MediaRelayService.start(this, ip)
        syncing = true
        showPanel(Panel.SYNC)
        binding.syncStatus.text = "正在连接 $ip…"
        binding.trackTitle.text = getString(R.string.waiting_track)
    }

    private enum class Panel { CONNECT, PERMIT, SYNC }

    private fun showPanel(panel: Panel) {
        binding.panelConnect.visibility = if (panel == Panel.CONNECT) View.VISIBLE else View.GONE
        binding.panelPermit.visibility = if (panel == Panel.PERMIT) View.VISIBLE else View.GONE
        binding.panelSync.visibility = if (panel == Panel.SYNC) View.VISIBLE else View.GONE
        window.statusBarColor = ContextCompat.getColor(
            this,
            if (panel == Panel.SYNC) R.color.brand_black else R.color.brand_red,
        )
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(MediaRelayService.ACTION_STATUS)
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(statusReceiver, filter)
        }
    }

    override fun onStop() {
        unregisterReceiver(statusReceiver)
        super.onStop()
    }

    private fun isNotificationServiceEnabled(): Boolean {
        val cn = ComponentName(this, SessionNotificationListener::class.java)
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners")
        if (flat.isNullOrEmpty()) return false
        val names = flat.split(":").mapNotNull {
            ComponentName.unflattenFromString(it)
        }
        return names.any { it == cn } || flat.contains(packageName)
    }

    private fun maybeAskPostNotifications() {
        if (Build.VERSION.SDK_INT < 33) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            100,
        )
    }
}
