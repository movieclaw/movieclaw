import SwiftUI

/// 字幕预览弹层（对应 Web `components/subtitle-preview-dialog.tsx`）。
///
/// 设计要点（与 Web 保持一致）：
/// - 对白用「时间轴 + 纯文本」呈现，不暴露 SRT 序号或 ASS 样式：用户点一条字幕是为了
///   快速核对语言、内容与同步位置，不是编辑原始文件。长字幕交给 `LazyVStack` 按需布局。
/// - 内封轨首次预览要 ffmpeg 通读整个容器（大文件分钟级）。后端不把请求挂住等，
///   而是回 `pending` 并转后台抽取，这里按它给的 `retry_after_ms` 重拉；关掉弹层只中断
///   轮询（`.task` 随视图消失自动取消），后台抽取会继续做完落缓存，下次打开秒开。
/// - 外挂字幕可以「一键校准时间轴」：以影片音轨为基准校准，成功后覆盖当前这个字幕文件，
///   不产生副本；成功后重拉预览，并通知详情页重拉条目（字幕台账可能随之变化）。
struct TrackSubtitlePreviewSheet: View {
    let file: API.LibraryFileView
    let stream: API.SubtitleStreamView
    /// 预览接口的中性轨引用：`embedded:{序号}` / `external:{文件名}`
    let track: String
    /// 「语言 · 格式」，与列表行的语义一致
    let label: String
    var onChanged: () async -> Void = {}

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var data: API.SubtitlePreviewView?
    @State private var error: String?
    /// 非空 = 内封轨正在后台抽取，还没有内容可显示（不是出错）
    @State private var pending: String?
    @State private var retryKey = 0
    @State private var calibrating = false
    @State private var calibrationNotice: String?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                header
                Divider().overlay(Theme.line)
                content
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                if let data {
                    Divider().overlay(Theme.line)
                    footer(data)
                }
            }
            .navigationTitle("字幕预览")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                        .accessibilityLabel("关闭字幕预览")
                }
            }
        }
        .presentationDetents([.large, .medium])
        .presentationBackground(.regularMaterial)
        .task(id: retryKey) { await load() }
    }

    // MARK: - 头部：来源（外挂/内封）+ 格式 + 「标签 · 文件名」

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                TrackPreviewBadge(text: stream.external ? "外挂" : "内封")
                if let format = data?.format, !format.isEmpty {
                    TrackPreviewBadge(text: format.uppercased())
                }
            }
            Text("\(label) · \(stream.fileName ?? file.fileName)")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    // MARK: - 主体：加载中 / 失败 / 空 / 对白列表

    @ViewBuilder
    private var content: some View {
        if let error {
            VStack(spacing: 12) {
                Text("无法预览这条字幕")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(Color(red: 1, green: 0.71, blue: 0.71))
                Text(error)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .multilineTextAlignment(.center)
                Button("重新加载") { retryKey += 1 }
                    .buttonStyle(.glass)
            }
            .padding(24)
        } else if let data {
            if data.cues.isEmpty {
                Text("字幕已成功解析，但没有可显示的对白")
                    .font(.callout)
                    .foregroundStyle(Theme.textMuted)
                    .padding(.vertical, 80)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(data.cues.enumerated()), id: \.offset) { index, cue in
                            cueRow(cue)
                            if index < data.cues.count - 1 {
                                Divider().overlay(Color.white.opacity(0.06))
                            }
                        }
                    }
                    .padding(.vertical, 8)
                }
            }
        } else {
            VStack(spacing: 12) {
                ProgressView()
                Text(pending ?? (stream.external ? "正在读取字幕…" : "正在抽取内封字幕…"))
                    .font(.callout)
                    .foregroundStyle(Theme.textMuted)
                    .multilineTextAlignment(.center)
                if pending != nil {
                    Text("首次读取内封字幕需要通读整个视频文件，读好后会自动显示。")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .multilineTextAlignment(.center)
                }
            }
            .padding(24)
        }
    }

    private func cueRow(_ cue: API.SubtitleCueView) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(Self.timestamp(cue.startMs))
                    .foregroundStyle(Theme.textFaint)
                Text("→ \(Self.timestamp(cue.endMs))")
                    .foregroundStyle(Color.white.opacity(0.3))
            }
            .font(.caption.monospacedDigit())
            .frame(width: 104, alignment: .leading)
            .padding(.top, 2)
            Text(cue.text)
                .font(.callout)
                .foregroundStyle(Color.white.opacity(0.88))
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
    }

    // MARK: - 底部：校准回执 + 对白条数 + 校准 / 完成

    private func footer(_ data: API.SubtitlePreviewView) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if let calibrationNotice {
                Text(calibrationNotice)
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Color.white.opacity(0.035), in: .rect(cornerRadius: 10))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.white.opacity(0.1)))
            }
            HStack(spacing: 10) {
                Text("共 \(data.eventCount) 条对白")
                    .font(.subheadline)
                    .foregroundStyle(Theme.textMuted)
                Spacer(minLength: 8)
                if stream.external, stream.fileName != nil {
                    // 以影片音轨校准时间轴，成功后覆盖当前这一个字幕文件，不会产生副本
                    Button(calibrating ? "正在校准…" : "校准时间轴") {
                        Task { await calibrate() }
                    }
                    .buttonStyle(.glass)
                    .disabled(calibrating)
                }
                Button("完成") { dismiss() }
                    .buttonStyle(.glass)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
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
