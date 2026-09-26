import MPVCore
import SwiftUI

/// 播放诊断面板（对应 Web `components/player/diagnostics-panel.tsx`，信息层次对标 Emby 的播放信息）。
///
/// 用户报「放不出来 / 很卡」时截这一张图：源规格、处理方式、有没有走显卡、掉帧、会话 id、判定理由、
/// 以及 App 独有的「用的哪个引擎、怎么渲染」全在里面。每一节先摆「源是什么」，下一行「→ 做了什么」。
/// 服务端快照由控制器每 2 秒拉一次（只在面板打开时，同 Web），本地引擎读数面板自己每秒拉一次。
struct PlayerDiagnosticsPanel: View {
    let controller: PlaybackController
    /// 面板高度：竖屏限在中线上方（不压住中央的播放键），横屏可以高一些（同 Web 的取舍）
    let height: CGFloat
    let close: () -> Void

    private static let tierLabels = [0: "原文件直出", 1: "换壳直通", 2: "换壳 + 转音轨", 3: "硬件转码", 4: "软件转码"]
    private static let processingLabels = [
        "direct": "原文件直出", "remux": "仅容器重封装", "audio-transcode": "仅音频转码",
        "transcode-pending": "硬件转码（执行端读取中）", "local-hardware": "本地硬件转码",
        "local-software": "本地软件转码", "remote-hardware": "远程硬件转码",
    ]
    private static let locationLabels = ["client": "客户端", "nas": "NAS", "remote_worker": "远程 Worker"]
    private static let jobStateLabels = [
        "job.pending": "排队中", "job.accepted": "已接单", "job.progress": "转码中", "job.failed": "任务失败", "job.finished": "已完成",
    ]

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            content(stats: controller.engine?.stats())
        }
    }

    @ViewBuilder
    private func content(stats: EngineStats?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("播放诊断").font(.caption.weight(.semibold)).foregroundStyle(.white.opacity(0.9))
                Spacer()
                Button(action: close) {
                    // 点击区域放大到 36pt 并显式声明（透明背景下只有 ✕ 笔画能点中）
                    Image(systemName: "xmark").font(.caption2.weight(.bold)).frame(width: 36, height: 36).contentShape(.rect)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.white.opacity(0.5))
                .accessibilityLabel("关闭播放诊断")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    if let session = controller.session {
                        sections(session: session, stats: stats)
                    } else {
                        Text("正在建立播放会话…").foregroundStyle(.white.opacity(0.6))
                    }
                }
            }
        }
        .font(.system(size: 11.5))
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .frame(maxWidth: 340)
        .frame(height: height)
        .background(.black.opacity(0.7), in: .rect(cornerRadius: 14))
        .clipShape(.rect(cornerRadius: 14))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("player-diagnostics")
    }

    @ViewBuilder
    private func sections(session: API.PlaybackSessionView, stats: EngineStats?) -> some View {
        let decision = session.decision
        let diagnostics = controller.serverDiagnostics
        let source = session.source

        Section(title: "引擎") {
            SourceLine(controller.engine?.kind.label ?? "—")
            ForEach(stats?.details ?? [], id: \.self) { ActionLine($0) }
        }

        Section(title: "流媒体") {
            SourceLine([(source?.container ?? decision.container ?? "未知").uppercased(), mbps(source?.bitRate)].compactMap { $0 }.joined(separator: " · "))
            if controller.playsOriginalFile, decision.tier != 0 {
                ActionLine("原文件直出（MPV 本机解码；服务端判定为\(Self.tierLabels[decision.tier ?? -1] ?? "未知档位")）")
            } else {
                ActionLine(decision.tier == 0 ? "原文件直出" : "HLS · fMP4（\(Self.tierLabels[decision.tier ?? -1] ?? "未知档位")）")
            }
            if decision.degradedFrom != nil { ActionLine("上一档播放失败，自动降档而来", alert: true) }
        }

        Section(title: "执行") {
            let direct = controller.activeSessionId == nil
            let fallbackMode = direct ? "direct" : decision.tier == 1 ? "remux" : decision.tier == 2 ? "audio-transcode" : session.hwBackend != nil ? "transcode-pending" : "local-software"
            let mode = diagnostics?.processingMode ?? fallbackMode
            SourceLine(Self.processingLabels[mode] ?? mode)
            ActionLine("位置：" + (diagnostics.map { Self.locationLabels[$0.executionLocation] ?? $0.executionLocation } ?? (direct ? "客户端直出" : "读取中")))
            if let backend = diagnostics?.backend ?? session.hwBackend {
                ActionLine("后端：\(backend)" + (diagnostics?.encoder.map { " · \($0)" } ?? ""))
            }
            if let worker = diagnostics?.workerId {
                ActionLine("Worker：\(worker)" + (diagnostics?.workerOnline == true ? " · 在线" : diagnostics?.workerOnline == false ? " · 离线" : " · 切换中"))
            }
            if diagnostics?.workerVersion != nil || diagnostics?.ffmpegVersion != nil {
                ActionLine([diagnostics?.workerVersion.map { "Worker \($0)" }, diagnostics?.ffmpegVersion.map { "ffmpeg \($0)" }]
                    .compactMap { $0 }.joined(separator: " · "))
            }
            if diagnostics?.workerPlatform != nil || diagnostics?.workerArch != nil {
                ActionLine("平台：" + [diagnostics?.workerPlatform, diagnostics?.workerArch].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
            }
            if let job = diagnostics?.jobId {
                ActionLine("任务：\(job)" + (diagnostics?.jobState.map { " · \(Self.jobStateLabels[$0] ?? $0)" } ?? "") + (diagnostics?.jobSpeed.map { " · \($0)" } ?? ""))
            }
            if let code = diagnostics?.jobExitCode { ActionLine("ffmpeg 退出码：\(code)", alert: true) }
            if let error = diagnostics?.jobError, error != diagnostics?.sessionError { ActionLine("Worker：\(error)", alert: true) }
            if let tail = diagnostics?.jobStderrTail { ActionLine("ffmpeg stderr：\(tail)", alert: true) }
            if let error = diagnostics?.sessionError { ActionLine(error, alert: true) }
        }

        Section(title: "视频") {
            if let source {
                SourceLine([source.resolution, source.videoCodec?.uppercased(), source.hdr, source.frameRate.map { String(format: "%g fps", $0) }].compactMap { $0 }.joined(separator: " · "))
            }
            if let video = decision.video {
                if video.action == "copy" {
                    ActionLine("直通")
                } else {
                    ActionLine("转码（\((video.codec ?? "h264").uppercased())\(video.height.map { " \($0)p" } ?? "")\(session.hwBackend.map { " · \($0)" } ?? " · 软件")\(video.toneMap ? " · HDR 转 SDR" : "")\(video.bitrateCapBps.map { String(format: " · 按线路限 %.2f Mbps", Double($0) / 1_000_000) } ?? "")）")
                }
                if video.burnSubtitle != nil { ActionLine("字幕压制进画面") }
            }
            let dropped = stats?.droppedFrames
            let ratio = (dropped != nil && (stats?.totalFrames ?? 0) > 0) ? Double(dropped!) / Double(stats!.totalFrames!) : 0
            Text("掉帧 " + (dropped.map { "\($0) / \(stats?.totalFrames.map(String.init) ?? "?")" } ?? "—"))
                .foregroundStyle(ratio > 0.02 ? Color.red.opacity(0.8) : .white.opacity(0.75))
        }

        Section(title: "音频") {
            if let track = decision.audioTracks.first(where: { $0.ref == (controller.currentAudio ?? decision.audio?.trackRef) }) {
                SourceLine([LanguageLabel.of(track.language), track.codec?.uppercased(), track.channels.map { "\($0) 声道" }].compactMap { $0 }.joined(separator: " ") + (track.isDefault ? "（默认）" : ""))
            }
            if let audio = decision.audio {
                ActionLine(audio.action == "copy" || controller.playsOriginalFile ? "直通" : "转码（\((audio.codec ?? "aac").uppercased())\(audio.channels.map { " \($0) 声道" } ?? "")\(audio.downmix ? " · 已降混" : "")）")
            }
        }

        Section(title: "传输") {
            Text([
                stats?.engine ?? "—",
                mbps(stats?.bitrateBps.map { Int($0) }).map { "实时 \($0)" },
                PlaybackController.formatBandwidth(stats?.downlinkBps).map { "取流 \($0)" },
                stats.map { String(format: "缓冲 %.1f 秒", $0.bufferedSeconds) },
                stats.map { String(format: "播放头 %.1f 秒", $0.currentTimeSeconds) },
            ].compactMap { $0 }.joined(separator: " · "))
                .foregroundStyle(.white.opacity(0.75))
            Text(controller.phase == .buffering ? "缓冲中" : controller.paused ? "已暂停" : "播放中")
                .foregroundStyle(.white.opacity(0.75))
            // 「不丝滑」的量化行：上次跳转等了多久；卡顿不含 seek 造成的等待（同 Web qoe.ts 口径），两个数分开读
            let qoe = controller.qoeLive
            Text([
                qoe.lastSeekMs.map { String(format: "上次跳转 %.1f 秒", Double($0) / 1000) },
                "卡顿 \(qoe.rebufferCount) 次" + (qoe.rebufferCount > 0 ? String(format: " · 累计 %.1f 秒", Double(qoe.rebufferMs) / 1000) : ""),
            ].compactMap { $0 }.joined(separator: " · "))
                .foregroundStyle(.white.opacity(0.75))
            Text("会话 \(controller.activeSessionId ?? "无（直出）")")
                .foregroundStyle(.white.opacity(0.5))
                .textSelection(.enabled)
        }

        if let diagnostics, let total = diagnostics.totalSegments {
            Section(title: "供片") {
                SourceLine(diagnostics.pendingSegments.first.map { "等待 \(segment($0))" }
                    ?? diagnostics.requestedSegment.map { "最近请求 \(segment($0))" } ?? "当前无等待分片")
                ActionLine("连续产出 \(segment(diagnostics.highestProducedSegment)) · 头部 \(segment(diagnostics.headSegment)) · 共 \(total) 段")
                if let lead = diagnostics.leadSeconds {
                    ActionLine("转码领先 \(Int(lead.rounded())) 秒" + (diagnostics.pauseReasons.contains("lead") ? " · 已领先足够，转码暂停" : diagnostics.pauseReasons.contains("disk") ? " · 磁盘空间告急，转码暂停" : ""))
                }
                ActionLine("NAS 会话缓存 \(Formatters.bytes(diagnostics.cacheBytes))" + (diagnostics.cacheHit ? " · 命中上次转码产物（\(diagnostics.cachedSegments) 段免转）" : ""))
                if let failed = diagnostics.failedSegments.first {
                    ActionLine("当前缺口 \(segment(failed))" + (diagnostics.failedSegments.count > 1 ? " 等 \(diagnostics.failedSegments.count) 段" : " · 上传失败待重试"), alert: true)
                }
                if let historical = diagnostics.historicalFailedSegments.first {
                    ActionLine("历史失败 \(segment(historical))" + (diagnostics.historicalFailedSegments.count > 1 ? " 等 \(diagnostics.historicalFailedSegments.count) 段" : " · 已落后当前播放位置"))
                }
                if let upload = diagnostics.recentUploads.first {
                    ActionLine("最近上传 \(uploadLabel(upload))", alert: upload.status >= 400)
                }
                if let wait = diagnostics.segmentWaitMs {
                    ActionLine(String(format: "最近供片等待 %.1f 秒", Double(wait) / 1000) + (diagnostics.segmentStatus.map { " · HTTP \($0)" } ?? ""))
                }
            }
        }

        Text(decision.reason)
            .foregroundStyle(.white.opacity(0.6))
            .padding(.top, 4)
        if controller.engine?.kind == .mpv {
            let versions = MPVPlayer.versionInfo
            Text("组件许可：\(versions.mpv) · FFmpeg \(versions.ffmpeg)。\(MPVPlayer.licenseNotice)")
                .foregroundStyle(.white.opacity(0.45))
                .padding(.top, 2)
        }
    }

    private func mbps(_ bps: Int?) -> String? {
        guard let bps, bps > 0 else { return nil }
        return String(format: bps >= 10_000_000 ? "%.0f Mbps" : "%.1f Mbps", Double(bps) / 1_000_000)
    }

    /// 「seg00012.m4s · 成功 · 2.1 MB · 2.1 MB 期望」（同 Web uploadLabel）
    private func uploadLabel(_ upload: API.PlaybackArtifactUploadView) -> String {
        let status = upload.status == 201 ? "成功" : "HTTP \(upload.status)"
        let expected = upload.contentLength.map { "\(Formatters.bytes($0)) 期望" } ?? (upload.transferEncoding == "chunked" ? "chunked" : nil)
        return [upload.name, status, Formatters.bytes(upload.receivedBytes), expected].compactMap { $0 }.joined(separator: " · ")
    }

    private func segment(_ index: Int?) -> String {
        guard let index, index >= 0 else { return "—" }
        return String(format: "seg%05d", index)
    }
}

private struct Section<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(.white.opacity(0.6))
            VStack(alignment: .leading, spacing: 2) { content }.padding(.leading, 12)
        }
    }
}

private struct SourceLine: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).fontWeight(.medium).foregroundStyle(.white.opacity(0.95)) }
}

private struct ActionLine: View {
    let text: String
    let alert: Bool
    init(_ text: String, alert: Bool = false) {
        self.text = text
        self.alert = alert
    }

    var body: some View {
        Text("\(Text("→ ").foregroundStyle(.white.opacity(0.4)))\(Text(text).foregroundStyle(alert ? Color.red.opacity(0.8) : .white.opacity(0.75)))")
    }
}
