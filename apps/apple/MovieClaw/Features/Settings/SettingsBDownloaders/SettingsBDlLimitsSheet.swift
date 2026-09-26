import SwiftUI

/// 限速与队列（对应 Web `DownloaderLimitsModal`）：实时读写下载器的全局限速与任务队列上限。
///
/// 队列上限（qB 最大活动种子数 / Tr 下载与做种队列）决定同时活动的任务数，
/// 刷流做种多时最容易撞上：任务进 queued 排队、免费窗口内可能下不完。
///
/// 口径与 Web 完全一致：
/// - 限速以 KiB/s 输入，留空 = 不限速（提交 null）；
/// - 队列数值留空 = 保持下载器现状（提交 null）；
/// - Transmission 没有「最大活动种子数」，读回 null 时隐藏该输入、提交 null；
/// - 保存后回读生效值（下载器可能钳制），然后关闭。
struct SettingsBDlLimitsSheet: View {
    let downloader: API.DownloaderView

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @Environment(\.dismiss) private var dismiss

    @State private var loading = true
    @State private var busy = false
    @State private var error: String?
    @State private var dlKib = ""
    @State private var upKib = ""
    @State private var altSpeed = false
    @State private var queueEnabled = false
    @State private var maxDown = ""
    @State private var maxUp = ""
    @State private var maxTotal = ""
    @State private var supportsMaxTotal = true

    private static let kib = 1024

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    SettingsBIntro(text: "实时读写下载器的全局设置。限速留空 = 不限；队列上限决定同时活动的任务数，开着刷流大量做种时建议调大做种/活动上限，避免新任务排队。")
                    if let error {
                        SettingsBNotice(text: error, tone: .danger)
                            .accessibilityIdentifier("downloader-limits-error")
                    }
                }
                if loading {
                    Section {
                        ProgressView().frame(maxWidth: .infinity).padding(.vertical, 12)
                    }
                } else {
                    Section("限速") {
                        SettingsBNumberField(label: "全局下载限速", text: $dlKib, placeholder: "不限", unit: "KiB/s",
                                             identifier: "downloader-limits-download")
                        SettingsBNumberField(label: "全局上传限速", text: $upKib, placeholder: "不限", unit: "KiB/s",
                                             identifier: "downloader-limits-upload")
                        Toggle(isOn: $altSpeed) {
                            Text("备用限速档（计划任务/手动一键慢速时生效的那组限速）")
                                .font(.subheadline)
                                .foregroundStyle(Theme.textMuted)
                        }
                        .accessibilityIdentifier("downloader-limits-alt-speed")
                    }
                    Section("队列") {
                        Toggle(isOn: $queueEnabled.animation(.snappy)) {
                            Text("任务队列（超出上限的任务排队等待）")
                                .font(.subheadline)
                                .foregroundStyle(Theme.textMuted)
                        }
                        .accessibilityIdentifier("downloader-limits-queue")
                        if queueEnabled {
                            SettingsBNumberField(label: "最大同时下载数", text: $maxDown, identifier: "downloader-limits-max-down")
                            SettingsBNumberField(label: "最大做种数", text: $maxUp, identifier: "downloader-limits-max-up")
                            if supportsMaxTotal {
                                SettingsBNumberField(label: "最大活动种子数", text: $maxTotal, identifier: "downloader-limits-max-total")
                            }
                        }
                    }
                }
            }
            .disabled(busy)
            .scrollContentBackground(.hidden)
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("限速与队列 · \(downloader.name)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消", systemImage: "xmark") { dismiss() }
                        .disabled(busy)
                        .accessibilityIdentifier("sheet-close")
                }
            }
            .safeAreaBar(edge: .bottom) {
                SubsPrimaryButton(title: busy ? "保存中…" : "保存", busy: busy, enabled: !loading,
                                  identifier: "downloader-limits-save") {
                    Task { await save() }
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
        }
        .presentationBackground(.regularMaterial)
        .task { await load() }
    }

    private func apply(_ limits: API.DownloaderLimitsView) {
        dlKib = limits.downloadLimitBytes.map { String(Int((Double($0) / Double(Self.kib)).rounded())) } ?? ""
        upKib = limits.uploadLimitBytes.map { String(Int((Double($0) / Double(Self.kib)).rounded())) } ?? ""
        altSpeed = limits.altSpeedEnabled ?? false
        queueEnabled = limits.queueEnabled ?? false
        maxDown = limits.maxActiveDownloads.map(String.init) ?? ""
        maxUp = limits.maxActiveUploads.map(String.init) ?? ""
        maxTotal = limits.maxActiveTorrents.map(String.init) ?? ""
        supportsMaxTotal = limits.maxActiveTorrents != nil
    }

    private func load() async {
        loading = true
        error = nil
        do {
            apply(try await api.dlLimits(downloaderId: downloader.id))
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    /// 空 = 不限速（null）；非正数同样视为不限
    private func parseSpeed(_ raw: String) -> Int? {
        let text = raw.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, let value = Double(text), value.isFinite else { return nil }
        let kib = Int(value.rounded())
        return kib > 0 ? kib * Self.kib : nil
    }

    /// 空 = 保持现状（null）
    private func parseCount(_ raw: String) -> Int? {
        let text = raw.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, let value = Double(text), value.isFinite else { return nil }
        let count = Int(value.rounded())
        return count >= 0 ? count : nil
    }

    /// 粘贴超长数字（numberPad 也能粘贴）时 Int 转换或 ×1024 会溢出直接闪退：先挡在提交前
    private static func tooLarge(_ raw: String) -> Bool {
        guard let value = Double(raw.trimmingCharacters(in: .whitespaces)), value.isFinite else { return false }
        return abs(value.rounded()) > Double(Int.max / kib)
    }

    private func save() async {
        guard !busy else { return }
        if [dlKib, upKib, maxDown, maxUp, maxTotal].contains(where: Self.tooLarge) {
            error = "数值过大，请填写合理的数字"
            return
        }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let applied = try await api.dlLimitsSet(downloaderId: downloader.id, body: .init(
                downloadLimitBytes: parseSpeed(dlKib),
                uploadLimitBytes: parseSpeed(upKib),
                altSpeedEnabled: altSpeed,
                queueEnabled: queueEnabled,
                maxActiveDownloads: parseCount(maxDown),
                maxActiveUploads: parseCount(maxUp),
                maxActiveTorrents: supportsMaxTotal ? parseCount(maxTotal) : nil
            ))
            apply(applied) // 回读生效值（下载器可能钳制），表单即时对齐
            dismiss()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            feedback.error(error)
        }
    }
}
