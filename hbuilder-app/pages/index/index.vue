<template>
	<view class="page">
		<!-- 连接 -->
		<scroll-view v-if="panel==='connect'" class="panel" scroll-y>
			<view class="mark red">
				<text class="mark-char">词</text>
				<view class="mark-grain"></view>
			</view>
			<view class="body">
				<text class="eyebrow">手机播 · 电脑出词</text>
				<text class="brand">歌词桥</text>
				<text class="lede">把 QQ 音乐的歌词同步到电脑桌面。声音仍在手机。</text>

				<view class="field">
					<text class="field-label">电脑局域网 IP</text>
					<input
						class="ip"
						v-model="ip"
						placeholder="例如 192.168.1.8"
						placeholder-style="color:#9A9A9A"
					/>
					<view class="field-line"></view>
				</view>

				<text class="field-label modes-head">同步来源</text>
				<view class="seg">
					<view
						v-for="m in modes"
						:key="m.id"
						class="seg-item"
						:class="{ on: source===m.id, primary: m.id==='qqmusic' }"
						@click="setSource(m.id)"
					>
						<text class="seg-name">{{ m.name }}</text>
						<text v-if="m.id!=='qqmusic'" class="seg-tag">实验</text>
					</view>
				</view>
				<text class="mode-tip">{{ sourceTip }}</text>

				<view class="palette">
					<view class="chip red"></view>
					<view class="chip ink"></view>
					<view class="chip paper"></view>
				</view>

				<view class="status-row">
					<view class="dot" :class="statusDot"></view>
					<text class="hint">{{ status }}</text>
				</view>

				<view class="btn black" hover-class="btn-press" @click="onStart">开始同步</view>
			</view>
		</scroll-view>

		<!-- 授权 -->
		<scroll-view v-else-if="panel==='permit'" class="panel" scroll-y>
			<view class="mark red sm">
				<text class="mark-char sm">词</text>
			</view>
			<view class="body">
				<text class="eyebrow">首次设置</text>
				<text class="brand">需要系统授权</text>
				<text class="lede">没有这些权限，读不到正在播放的歌曲。</text>

				<view class="checks">
					<view class="check" @click="openNotificationAccess">
						<text class="check-num">01</text>
						<view class="check-copy">
							<text class="check-title">设备和应用通知</text>
							<text class="check-desc">允许「歌词桥」读取播放信息</text>
						</view>
						<text class="check-go">去开启</text>
					</view>
					<view class="check" @click="askBatteryExempt">
						<text class="check-num">02</text>
						<view class="check-copy">
							<text class="check-title">电池与后台</text>
							<text class="check-desc">电量无限制，忽略电池优化</text>
						</view>
						<text class="check-go">去设置</text>
					</view>
					<view class="check static">
						<text class="check-num">03</text>
						<view class="check-copy">
							<text class="check-title">同步时保留通知</text>
							<text class="check-desc">不要上滑杀掉「歌词桥」</text>
						</view>
					</view>
				</view>

				<view class="btn red" hover-class="btn-press" @click="openNotificationAccess">去开通知权限</view>
				<view class="btn ghost" hover-class="btn-press-ghost" @click="afterPermit">已开启，继续</view>
			</view>
		</scroll-view>

		<!-- 同步中 -->
		<scroll-view v-else class="panel" scroll-y>
			<view class="mark black sm">
				<text class="mark-char sm">词</text>
				<view class="live-pill" :class="{ on: socketReady && syncing }">
					{{ syncLiveLabel }}
				</view>
			</view>
			<view class="body">
				<text class="eyebrow">{{ sourceName }}</text>
				<text class="brand track">{{ title || waitText }}</text>
				<text class="sub artist">{{ artist || ' ' }}</text>
				<text class="lyric">{{ status }}</text>

				<view class="seg compact">
					<view
						v-for="m in modes"
						:key="m.id"
						class="seg-item"
						:class="{ on: source===m.id }"
						@click="setSource(m.id)"
					>
						<text class="seg-name">{{ m.short || m.name }}</text>
					</view>
				</view>

				<view class="btn black" hover-class="btn-press" @click="onStop">停止同步</view>
			</view>
		</scroll-view>
	</view>
</template>

<script>
	// #ifdef APP-PLUS
	import {
		openNotificationAccess as utsOpenAccess,
		hasNotificationAccess as utsHasAccess,
		readNowPlaying as utsReadNow,
		startKeepAlive as utsStartKeepAlive,
		stopKeepAlive as utsStopKeepAlive,
		requestIgnoreBatteryOptimizations as utsIgnoreBattery
	} from '@/uni_modules/lyrics-notify-listener'
	// #endif

	const PORT = 8765
	const RECONNECT_MS = 3000
	const EMPTY_MISS_LIMIT = 12
	const MODES = [
		{ id: 'qqmusic', name: 'QQ音乐', short: 'QQ音乐', tip: '首选：同步 QQ 音乐歌词到电脑', wait: '等待QQ音乐播放' },
		{ id: 'bilibili', name: 'B站字幕', short: 'B站', tip: '实验：有 CC/AI 字幕时尽量跟字幕', wait: '等待B站播放' },
		{ id: 'fanqie', name: '番茄听书', short: '番茄', tip: '实验：电脑显示章节或书名', wait: '等待番茄听书播放' }
	]

	export default {
		data() {
			const saved = uni.getStorageSync('sync_source') || 'qqmusic'
			return {
				panel: 'connect',
				ip: uni.getStorageSync('pc_ip') || '',
				source: saved,
				modes: MODES,
				status: '未连接',
				syncing: false,
				socketReady: false,
				intentionalStop: false,
				title: '',
				artist: '',
				socketTask: null,
				timer: null,
				reconnectTimer: null,
				emptyMiss: 0,
				stopSent: false,
				pcHost: ''
			}
		},
		computed: {
			sourceName() {
				const m = MODES.find(x => x.id === this.source)
				return m ? m.name : 'QQ音乐'
			},
			sourceTip() {
				const m = MODES.find(x => x.id === this.source)
				return m ? m.tip : ''
			},
			waitText() {
				const m = MODES.find(x => x.id === this.source)
				return m ? m.wait : '等待播放'
			},
			statusDot() {
				if (this.status.indexOf('失败') >= 0 || this.status.indexOf('断开') >= 0) return 'bad'
				if (this.status.indexOf('连接') >= 0 || this.status.indexOf('同步') >= 0) return 'ok'
				return 'idle'
			},
			syncLiveLabel() {
				if (!this.syncing) return '已停止'
				if (this.socketReady) return '已连接'
				return '重连中'
			}
		},
		onUnload() {
			this.onStop()
		},
		onHide() {
			if (this.syncing) {
				this.setWakelock(true)
			}
		},
		onShow() {
			if (this.syncing) {
				this.startKeepAlive()
				this.setWakelock(true)
			}
		},
		methods: {
			setWakelock(on) {
				// #ifdef APP-PLUS
				try {
					plus.device.setWakelock(!!on)
				} catch (e) {}
				// #endif
			},
			askBatteryExempt() {
				// #ifdef APP-PLUS
				try {
					utsIgnoreBattery()
				} catch (e) {}
				// #endif
			},
			setSource(id) {
				this.source = id
				uni.setStorageSync('sync_source', id)
				this.title = ''
				this.artist = ''
				this.emptyMiss = 0
				this.stopSent = false
				if (this.syncing) {
					this.status = '已切换为 ' + this.sourceName
					this.pushNow()
				}
			},
			clearReconnect() {
				if (this.reconnectTimer) {
					clearTimeout(this.reconnectTimer)
					this.reconnectTimer = null
				}
			},
			clearTick() {
				if (this.timer) {
					clearInterval(this.timer)
					this.timer = null
				}
			},
			closeSocketOnly() {
				this.socketReady = false
				if (this.socketTask) {
					try {
						this.socketTask.close()
					} catch (e) {}
					this.socketTask = null
				}
			},
			startKeepAlive() {
				// #ifdef APP-PLUS
				try {
					utsStartKeepAlive()
				} catch (e) {}
				// #endif
			},
			stopKeepAlive() {
				// #ifdef APP-PLUS
				try {
					utsStopKeepAlive()
				} catch (e) {}
				// #endif
			},
			cleanupAll() {
				this.clearReconnect()
				this.clearTick()
				this.closeSocketOnly()
				this.stopKeepAlive()
				this.syncing = false
				this.socketReady = false
				this.emptyMiss = 0
				this.stopSent = false
			},
			onStart() {
				const ip = (this.ip || '').trim()
				if (!ip) {
					this.status = '请填写电脑 IP'
					return
				}
				uni.setStorageSync('pc_ip', ip)
				uni.setStorageSync('sync_source', this.source)
				// #ifdef APP-PLUS
				if (!this.checkAccess()) {
					this.panel = 'permit'
					return
				}
				// #endif
				this.startSync(ip)
			},
			afterPermit() {
				// #ifdef APP-PLUS
				if (!this.checkAccess()) {
					uni.showToast({
						title: '列表里还没开歌词桥',
						icon: 'none'
					})
					return
				}
				// #endif
				this.startSync((this.ip || '').trim())
			},
			checkAccess() {
				// #ifdef APP-PLUS
				try {
					return !!utsHasAccess()
				} catch (e) {
					return false
				}
				// #endif
				return true
			},
			openNotificationAccess() {
				// #ifdef APP-PLUS
				try {
					utsOpenAccess()
				} catch (e) {
					uni.showToast({
						title: '请到设置搜索：设备和应用通知',
						icon: 'none'
					})
				}
				// #endif
			},
			startSync(ip) {
				this.cleanupAll()
				this.intentionalStop = false
				this.pcHost = ip
				this.panel = 'sync'
				this.syncing = true
				this.title = ''
				this.artist = ''
				this.status = '正在连接 ' + ip + '…'
				this.startKeepAlive()
				this.setWakelock(true)
				this.askBatteryExempt()
				this.connectSocket()
				this.clearTick()
				this.timer = setInterval(() => this.pushNow(), 500)
			},
			connectSocket() {
				if (!this.syncing || this.intentionalStop) return
				this.clearReconnect()
				this.closeSocketOnly()
				const host = this.pcHost
				const url = 'ws://' + host + ':' + PORT
				const task = uni.connectSocket({
					url,
					complete: () => {}
				})
				this.socketTask = task
				task.onOpen(() => {
					if (this.socketTask !== task) return
					this.socketReady = true
					this.status = '已连接 · ' + this.sourceName
					this.stopSent = false
					this.pushNow()
				})
				task.onError(() => {
					if (this.socketTask !== task) return
					this.socketReady = false
					if (!this.syncing || this.intentionalStop) return
					this.status = '连接失败，重试中…'
					this.scheduleReconnect()
				})
				task.onClose(() => {
					if (this.socketTask !== task) return
					this.socketReady = false
					this.socketTask = null
					if (!this.syncing || this.intentionalStop) return
					this.status = '连接断开，' + (RECONNECT_MS / 1000) + ' 秒后重连…'
					this.scheduleReconnect()
				})
			},
			scheduleReconnect() {
				if (!this.syncing || this.intentionalStop) return
				if (this.reconnectTimer) return
				this.reconnectTimer = setTimeout(() => {
					this.reconnectTimer = null
					if (!this.syncing || this.intentionalStop) return
					this.status = '正在重连 ' + this.pcHost + '…'
					this.connectSocket()
				}, RECONNECT_MS)
			},
			onStop() {
				this.intentionalStop = true
				this.sendStop()
				this.setWakelock(false)
				this.cleanupAll()
				this.panel = 'connect'
				this.status = '未连接'
				this.title = ''
				this.artist = ''
			},
			sendStop() {
				if (!this.socketReady || !this.socketTask) return
				try {
					this.socketTask.send({
						data: JSON.stringify({
							type: 'stop'
						})
					})
					this.stopSent = true
				} catch (e) {}
			},
			pushNow() {
				if (!this.syncing) return
				const info = this.readNowPlaying()
				if (!info || !info.title) {
					this.emptyMiss++
					if (this.emptyMiss >= EMPTY_MISS_LIMIT) {
						this.title = ''
						this.artist = ''
						this.status = this.waitText
						if (!this.stopSent) this.sendStop()
					}
					return
				}
				this.emptyMiss = 0
				this.stopSent = false
				this.title = info.title
				this.artist = info.artist || ''
				const link = this.socketReady ? '' : ' · 重连中'
				this.status = (info.playing ? '同步中: ' : '已暂停: ') + info.title + link
				if (!this.socketReady || !this.socketTask) return
				try {
					this.socketTask.send({
						data: JSON.stringify({
							type: 'state',
							playing: !!info.playing,
							title: info.title,
							artist: info.artist || '',
							album: info.album || '',
							position_ms: info.positionMs || 0,
							duration_ms: info.durationMs || 0,
							package: info.packageName || '',
							source: this.source,
							sent_at: Date.now()
						})
					})
				} catch (e) {
					this.socketReady = false
				}
			},
			readNowPlaying() {
				// #ifdef APP-PLUS
				try {
					return utsReadNow(this.source || 'qqmusic')
				} catch (e) {
					return null
				}
				// #endif
				return null
			}
		}
	}
</script>

<style>
	.page {
		min-height: 100vh;
		background: #FAFAFA;
	}

	.panel {
		height: 100vh;
		background: #FAFAFA;
	}

	.mark {
		position: relative;
		height: 38vh;
		min-height: 280rpx;
		background: #E60012;
		overflow: hidden;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.mark.sm {
		height: 26vh;
		min-height: 200rpx;
	}

	.mark.black {
		background: #141414;
	}

	.mark.red {
		background: #E60012;
	}

	.mark-char {
		position: relative;
		z-index: 2;
		color: #fff;
		font-size: 200rpx;
		font-weight: 900;
		line-height: 1;
		letter-spacing: -0.06em;
	}

	.mark-char.sm {
		font-size: 148rpx;
	}

	.mark-grain {
		position: absolute;
		left: 0;
		top: 0;
		right: 0;
		bottom: 0;
		opacity: 0.12;
		background-image: repeating-linear-gradient(
			-18deg,
			rgba(255, 255, 255, 0.35) 0,
			rgba(255, 255, 255, 0.35) 1px,
			transparent 1px,
			transparent 10px
		);
		pointer-events: none;
	}

	.live-pill {
		position: absolute;
		right: 36rpx;
		bottom: 28rpx;
		z-index: 3;
		padding: 10rpx 20rpx;
		font-size: 22rpx;
		font-weight: 600;
		letter-spacing: 0.04em;
		color: rgba(255, 255, 255, 0.72);
		border: 2rpx solid rgba(255, 255, 255, 0.28);
		background: rgba(0, 0, 0, 0.25);
	}

	.live-pill.on {
		color: #fff;
		border-color: #E60012;
		background: #E60012;
	}

	.body {
		padding: 44rpx 48rpx 64rpx;
		display: flex;
		flex-direction: column;
		box-sizing: border-box;
	}

	.eyebrow {
		font-size: 24rpx;
		font-weight: 600;
		letter-spacing: 0.12em;
		color: #8A8A8A;
		text-transform: none;
	}

	.brand {
		margin-top: 12rpx;
		font-size: 64rpx;
		font-weight: 900;
		letter-spacing: -0.03em;
		color: #141414;
		line-height: 1.1;
	}

	.brand.track {
		font-size: 48rpx;
		line-height: 1.25;
	}

	.lede {
		margin-top: 16rpx;
		font-size: 28rpx;
		line-height: 1.55;
		color: #6B6B6B;
		max-width: 92%;
	}

	.sub {
		margin-top: 12rpx;
		font-size: 28rpx;
		color: #6B6B6B;
	}

	.sub.artist {
		min-height: 40rpx;
	}

	.field {
		margin-top: 48rpx;
	}

	.field-label,
	.modes-head {
		font-size: 24rpx;
		font-weight: 600;
		letter-spacing: 0.08em;
		color: #8A8A8A;
	}

	.modes-head {
		margin-top: 40rpx;
		display: block;
	}

	.ip {
		margin-top: 12rpx;
		font-size: 40rpx;
		font-weight: 700;
		color: #141414;
		padding: 12rpx 0 18rpx;
	}

	.field-line {
		height: 3rpx;
		background: #141414;
	}

	.seg {
		display: flex;
		margin-top: 18rpx;
	}

	.seg.compact {
		margin-top: 48rpx;
		margin-bottom: 8rpx;
	}

	.seg-item {
		flex: 1;
		min-height: 88rpx;
		margin-right: 12rpx;
		padding: 16rpx 12rpx;
		box-sizing: border-box;
		border: 3rpx solid #141414;
		background: #fff;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
	}

	.seg-item:last-child {
		margin-right: 0;
	}

	.seg-item.on {
		background: #141414;
	}

	.seg-item.on .seg-name,
	.seg-item.on .seg-tag {
		color: #fff;
	}

	.seg-name {
		font-size: 26rpx;
		font-weight: 700;
		color: #141414;
	}

	.seg-tag {
		margin-top: 4rpx;
		font-size: 18rpx;
		font-weight: 600;
		letter-spacing: 0.06em;
		color: #8A8A8A;
	}

	.mode-tip {
		margin-top: 16rpx;
		font-size: 24rpx;
		line-height: 1.5;
		color: #8A8A8A;
	}

	.palette {
		display: flex;
		margin-top: 36rpx;
	}

	.chip {
		width: 28rpx;
		height: 28rpx;
		margin-right: 12rpx;
	}

	.chip.red {
		background: #E60012;
	}

	.chip.ink {
		background: #141414;
	}

	.chip.paper {
		background: #fff;
		border: 2rpx solid #141414;
		box-sizing: border-box;
	}

	.status-row {
		margin-top: 40rpx;
		margin-bottom: 20rpx;
		display: flex;
		align-items: center;
	}

	.dot {
		width: 14rpx;
		height: 14rpx;
		margin-right: 14rpx;
		border-radius: 50%;
		background: #C8C8C8;
	}

	.dot.ok {
		background: #E60012;
	}

	.dot.bad {
		background: #141414;
	}

	.dot.idle {
		background: #C8C8C8;
	}

	.hint {
		font-size: 26rpx;
		color: #6B6B6B;
	}

	.lyric {
		margin-top: 36rpx;
		font-size: 30rpx;
		line-height: 1.55;
		color: #141414;
	}

	.checks {
		margin-top: 40rpx;
		margin-bottom: 24rpx;
	}

	.check {
		display: flex;
		align-items: center;
		padding: 28rpx 0;
		border-bottom: 2rpx solid #E8E8E8;
	}

	.check.static {
		opacity: 0.92;
	}

	.check-num {
		width: 56rpx;
		font-size: 24rpx;
		font-weight: 700;
		letter-spacing: 0.04em;
		color: #E60012;
	}

	.check-copy {
		flex: 1;
		display: flex;
		flex-direction: column;
	}

	.check-title {
		font-size: 30rpx;
		font-weight: 700;
		color: #141414;
	}

	.check-desc {
		margin-top: 6rpx;
		font-size: 24rpx;
		color: #8A8A8A;
		line-height: 1.4;
	}

	.check-go {
		font-size: 24rpx;
		font-weight: 700;
		color: #141414;
		padding: 10rpx 0 10rpx 16rpx;
	}

	.btn {
		height: 100rpx;
		margin-top: 18rpx;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 32rpx;
		font-weight: 700;
		letter-spacing: 0.02em;
	}

	.btn.black {
		background: #141414;
		color: #fff;
	}

	.btn.red {
		background: #E60012;
		color: #fff;
	}

	.btn.ghost {
		background: transparent;
		color: #141414;
		border: 3rpx solid #141414;
		box-sizing: border-box;
	}

	.btn-press {
		opacity: 0.86;
		transform: scale(0.985);
	}

	.btn-press-ghost {
		background: #F0F0F0;
	}
</style>
