package com.phonelyrics.bridge

import android.service.notification.NotificationListenerService

/**
 * Exists so MediaSessionManager.getActiveSessions() is allowed.
 * User must enable notification access for this app.
 */
class SessionNotificationListener : NotificationListenerService()
