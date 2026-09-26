import SwiftUI

/// 字幕预览页（对应 Web `components/subtitle-preview-dialog.tsx`），从字幕列表弹层里推进过来，
/// 不单独起弹层：左上角返回列表，弹层的液态玻璃、关闭都归列表管（见 `TrackListSheet`）。
///
/// 设计要点（与 Web 保持一致）：
/// - 对白用「时间轴 + 纯文本」呈现，不暴露 SRT 序号或 ASS 样式：用户点一条字幕是为了
///   快速核对语言、内容与同步位置，不是编辑原始文件。对白放原生列表，长字幕按需布局。
/// - 内封轨首次预览要 ffmpeg 通读整个容器（大文件分钟级）。后端不把请求挂住等，
///   而是回 `pending` 并转后台抽取，这里按它给的 `retry_after_ms` 重拉；返回列表只中断
///   轮询（`.task` 随视图消失自动取消），后台抽取会继续做完落缓存，下次打开秒开。
/// - 外挂字幕可以「一键校准时间轴」（右上角）：以影片音轨为基准校准，成功后覆盖当前这个字幕文件，
///   不产生副本；成功后重拉预览，并通知详情页重拉条目（字幕台账可能随之变化）。
struct TrackSubtitlePreviewPage: View {
    let file: API.LibraryFileView
    let stream: API.SubtitleStreamView
    /// 预览接口的中性轨引用：`embedded:{序号}` / `external:{文件名}`
    let track: String
    /// 「语言 · 格式」，与列表行的语义一致
    let label: String
    var onChanged: () async -> Void = {}

    @Environment(\.api) private var api

    @State private var data: API.SubtitlePreviewView?
    @State private var error: String?
    /// 非空 = 内封轨正在后台抽取，还没有内容可显示（不是出错）
    @State private var pending: String?
    @State private var retryKey = 0
    @State private var calibrating = false
    @State private var calibrationNotice: String?

    /// 只有外挂字幕能校准（内封轨长在容器里，改不了）
    private var calibratable: Bool { stream.external && stream.fileName != nil }

    var body: some View {
        Form {
            Section {
                header
            }
            if let calibrationNotice {
                Section {
                    SubsNoticeRow(text: calibrationNotice, tone: .info)
                }
            }
            content
        }
        .subsFormStyle()
        .navigationTitle(label)
        .navigationSubtitle(data.map { "共 \($0.eventCount) 条对白" } ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if calibratable, data != nil {
                ToolbarItem(placement: .primaryAction) {
                    if calibrating {
                        ProgressView().accessibilityLabel("正在校准时间轴")
                    } else {
                        // 以影片音轨校准时间轴，成功后覆盖当前这一个字幕文件，不会产生副本
                        Button("校准时间轴") { Task { await calibrate() } }
                    }
                }
            }
        }
        .task(id: retryKey) { await load() }
    }

    // MARK: - 头部：来源（外挂/内封）+ 格式 + 文件名

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TrackPreviewBadge(text: stream.external ? "外挂" : "内封")
                if let format = data?.format, !format.isEmpty {
                    TrackPreviewBadge(text: format.uppercased())
                }
            }
            Text(stream.fileName ?? file.fileName)
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(2)
                .truncationMode(.middle)
        }
    }

    // MARK: - 主体：加载中 / 失败 / 空 / 对白列表

    @ViewBuilder
    private var content: some View {
        if let error {
            Section {
                SubsNoticeRow(text: "无法预览这条字幕：\(error)", tone: .error)
                Button("重新加载") { retryKey += 1 }
            }
        } else if let data {
            Section {
                if data.cues.isEmpty {
                    Text("字幕已成功解析，但没有可显示的对白")
                        .font(.subheadline)
                        .foregroundStyle(Theme.textMuted)
                } else {
                    ForEach(Array(data.cues.enumerated()), id: \.offset) { _, cue in
                        cueRow(cue)
                    }
                }
            }
        } else {
            Section {
                HStack(alignment: .top, spacing: 12) {
                    ProgressView()
                    VStack(alignment: .leading, spacing: 4) {
                        Text(pending ?? (stream.external ? "正在读取字幕…" : "正在抽取内封字幕…"))
                            .font(.subheadline)
                            .foregroundStyle(Theme.text)
                        if pending != nil {
                            Text("首次读取内封字幕需要通读整个视频文件，读好后会自动显示。")
                                .font(.caption)
                                .foregroundStyle(Theme.textMuted)
                        }
                    }
                }
            }
        }
    }

    private func cueRow(_ cue: API.SubtitleCueView) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(Self.timestamp(cue.startMs))
                    .foregroundStyle(Theme.textMuted)
                Text("→ \(Self.timestamp(cue.endMs))")
                    .foregroundStyle(Theme.textFaint)
            }
            .font(.caption.monospacedDigit())
            .frame(width: 104, alignment: .leading)
            .padding(.top, 2)
            Text(cue.text)
                .font(.callout)
                .foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
    }

    // MARK: - 数据

    /// 拉预览；遇到 pending 按后端建议的间隔（至少 1 秒）重拉，直到拿到内容或出错。
    private func load() async {
        data = nil
        error = nil
        pending = nil
        while !Task.isCancelled {
            do {
                let result = try await api.trackRowsSubtitlePreview(fileId: file.id, track: track)
                if Task.isCancelled { return }
                if let message = result.pending, !message.isEmpty {
                    pending = message
                    try? await Task.sleep(for: .milliseconds(max(1000, result.retryAfterMs)))
                    continue
                }
                pending = nil
                data = result
                return
            } catch is CancellationError {
                return
            } catch {
                if Task.isCancelled { return }
                pending = nil
                self.error = error.localizedDescription.isEmpty ? "字幕预览加载失败，请稍后重试" : error.localizedDescription
                return
            }
        }
    }

    private func calibrate() async {
        guard stream.external, let filename = stream.fileName else { return }
        calibrating = true
        calibrationNotice = nil
        defer { calibrating = false }
        do {
            let result = try await api.librarySubtitlesCalibrateTiming(
                fileId: file.id, body: API.CalibratePayload(filename: filename)
            )
            calibrationNotice = result.message
            if result.ok {
                retryKey += 1
                await onChanged()
            }
        } catch is CancellationError {
        } catch {
            calibrationNotice = error.localizedDescription.isEmpty ? "校准失败，请稍后再试" : error.localizedDescription
        }
    }

    /// 毫秒 → `HH:MM:SS.mmm`（与 Web formatTimestamp 同口径）
    private static func timestamp(_ milliseconds: Int) -> String {
        let totalSeconds = milliseconds / 1000
        let h = totalSeconds / 3600, m = (totalSeconds % 3600) / 60, s = totalSeconds % 60
        return String(format: "%02d:%02d:%02d.%03d", h, m, s, milliseconds % 1000)
    }
}

/// 头部小标签（外挂/内封、格式）
private struct TrackPreviewBadge: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption2)
            .foregroundStyle(Color.white.opacity(0.65))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.white.opacity(0.08), in: .rect(cornerRadius: 4))
    }
}

nonisolated extension APIClient {
    /// 字幕预览（`GET /libraries/files/{id}/subtitles/preview?track=`）。
    ///
    /// 生成函数没有超时参数，这里手写一份带 20 秒超时的版本（同 Web
    /// `SUBTITLE_PREVIEW_TIMEOUT_MS`）：后端保证不在请求里等 ffmpeg，超过 20 秒就是真的异常。
    fileprivate func trackRowsSubtitlePreview(fileId: Int, track: String) async throws -> API.SubtitlePreviewView {
        try await send(
            "GET", "/libraries/files/\(fileId)/subtitles/preview",
            query: [URLQueryItem(name: "track", value: track)],
            timeout: 20
        )
    }
}
