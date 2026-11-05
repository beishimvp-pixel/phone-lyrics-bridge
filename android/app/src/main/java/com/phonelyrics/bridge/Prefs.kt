package com.phonelyrics.bridge

import android.content.Context

object Prefs {
    private const val NAME = "bridge"
    private const val KEY_IP = "pc_ip"

    fun getIp(context: Context): String =
        context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
            .getString(KEY_IP, "") ?: ""

    fun setIp(context: Context, ip: String) {
        context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_IP, ip.trim())
            .apply()
    }
}
