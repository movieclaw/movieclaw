import SwiftUI

// 下载器清单的一台（对应 Web `DownloaderRow` + `PathMappingTable` + `DownloaderActionsMenu`）。

/// 行菜单里的操作（顺序同 Web ⋯ 菜单）
enum SettingsBDlAction: Equatable {
    case toggleEnabled, edit, limits, setDefault, verify, delete
}

/// 编辑器打开参数：downloader 为 nil = 新建
struct SettingsBDlEditorTarget: Identifiable {
    let id = UUID()
    var downloader: API.DownloaderView?
}

/// 限速与队列弹层打开参数（生成的模型没有 Identifiable，包一层）
struct SettingsBDlLimitsTarget: Identifiable {
    let downloader: API.DownloaderView
    var id: Int { downloader.id }
}

/// 连接状态 → 文案与色调（与站点配置同语言，同 Web STATUS_META）
enum SettingsBDlStatus {
    static func label(_ status: String) -> String {
        switch status {
        case "active": "已连接"
        case "verifying": "测试中"
        case "pending": "待测试"
        case "failed": "连接失败"
        default: status
        }
    }

    static func tone(_ status: String) -> SettingsBTone {
        switch status {
        case "active": .ok
        case "verifying": .info
        case "failed": .danger
        default: .neutral
        }
    }

    /// 需要轮询测试进度的中间态（此时也不允许「重新测试连接」）
    static func inProgress(_ status: String) -> Bool {
        status == "pending" || status == "verifying"
    }

    static func typeLabel(_ clientType: String) -> String {
        clientType == "transmission" ? "Transmission" : "qBittorrent"
    }

    /// 路径体检异常 → 短词（ok 不额外标注，正常不该抢注意力）
    static func pathStateLabel(_ state: String) -> String {
        switch state {
        case "empty": "目录为空"
        case "not_dir": "不是目录"
        case "missing": "不存在"
        case "unmapped": "未覆盖"
        default: state
        }
    }
}

/// 一台下载器 = 一个 Section：首行（名称 + 徽标 + 副标题 + 失败原因）点击展开详情，右侧 ··· 菜单。
///
/// 「API 通、路径瞎」是独立于连接状态的故障面：连接测试通过但映射的本地侧不可达时绿灯是假的——
/// 下载全部无法入库。所以此时状态胶囊直接改口「路径异常」，并把最坏的那条体检结论亮在首行（同 Web）。
struct SettingsBDlRowSection: View {
    let downloader: API.DownloaderView
    let expanded: Bool
    let busy: Bool
    let onToggle: () -> Void
    let onAction: (SettingsBDlAction) -> Void

    private var pathsBroken: Bool { downloader.status == "active" && !downloader.pathsHealthy }
    private var failedReason: String? {
        downloader.status == "failed" ? downloader.lastError.flatMap { $0.isEmpty ? nil : $0 } : nil
    }
    private var worstPath: API.PathProbeView? {
        pathsBroken ? downloader.pathHealth?.first { $0.state != "ok" } : nil
    }

    var body: some View {
        Section {
            header
            if expanded {
                details
            }
        }
    }

    // MARK: - 首行

    private var header: some View {
        HStack(spacing: 10) {
            Button(action: onToggle) {
                HStack(spacing: 12) {
                    Image(systemName: "arrow.down.to.line")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(Theme.text.opacity(0.85))
                        .frame(width: 36, height: 36)
                        .background(Color.white.opacity(0.07), in: .rect(cornerRadius: 10))
                    VStack(alignment: .leading, spacing: 4) {
                        // 窄屏徽章允许折到名称下一行，别把名称挤成两个字
                        SettingsBFlow(spacing: 6, lineSpacing: 4) {
                            Text(downloader.name)
                                .font(.body.weight(.semibold))
                                .foregroundStyle(Theme.text)
                                .lineLimit(1)
                            if downloader.isDefault { SettingsBBadge(text: "默认", tone: .accent) }
                            SettingsBDlStatusPill(
                                label: pathsBroken ? "路径异常" : SettingsBDlStatus.label(downloader.status),
                                tone: pathsBroken ? .danger : SettingsBDlStatus.tone(downloader.status)
                            )
                            if !downloader.enabled { SettingsBBadge(text: "已停用") }
                        }
                        Text(subtitle)
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .lineLimit(1)
                        // 连接失败或路径异常时把原因亮出来——那一刻没有比它更重要的信息
                        if let reason = failedReason ?? worstPath?.detail {
                            Text(reason)
                                .font(.caption)
                                .foregroundStyle(Theme.danger)
                                .lineLimit(2)
                                .accessibilityIdentifier("downloader-error-\(downloader.name)")
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Theme.textFaint)
                        .rotationEffect(.degrees(expanded ? 180 : 0))
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("downloader-row-\(downloader.name)")
            .accessibilityValue(expanded ? "已展开" : "已收起")

            actionsMenu
        }
        .opacity(downloader.enabled ? 1 : 0.6)
        .padding(.vertical, 2)
    }

    /// 副标题：类型 + 版本 + 上次检查时间（失败原因另起一行红字）
    private var subtitle: String {
        [
            SettingsBDlStatus.typeLabel(downloader.clientType),
            downloader.version,
            downloader.lastCheckedAt.map { "上次检查 \(SettingsBFormat.relative($0))" },
        ]
        .compactMap { $0 }
        .filter { !$0.isEmpty }
        .joined(separator: " · ")
    }

    private var actionsMenu: some View {
        Menu {
            Button(downloader.enabled ? "停用下载器" : "启用下载器",
                   systemImage: downloader.enabled ? "pause.circle" : "play.circle") { onAction(.toggleEnabled) }
                .disabled(busy)
                .accessibilityIdentifier("downloader-menu-toggle")
            Button("编辑配置", systemImage: "pencil") { onAction(.edit) }
                .accessibilityIdentifier("downloader-menu-edit")
            Button("限速与队列…", systemImage: "speedometer") { onAction(.limits) }
                .disabled(busy)
                .accessibilityIdentifier("downloader-menu-limits")
            Button("设为默认", systemImage: "star") { onAction(.setDefault) }
                .disabled(busy || downloader.isDefault)
                .accessibilityIdentifier("downloader-menu-default")
            Button("重新测试连接", systemImage: "bolt.horizontal") { onAction(.verify) }
                .disabled(busy || SettingsBDlStatus.inProgress(downloader.status))
                .accessibilityIdentifier("downloader-menu-verify")
            Divider()
            Button("删除配置", systemImage: "trash", role: .destructive) { onAction(.delete) }
                .disabled(busy)
                .accessibilityIdentifier("downloader-menu-delete")
        } label: {
            Group {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "ellipsis")
                }
            }
            .frame(width: 30, height: 30)
            .contentShape(.rect)
        }
        .buttonStyle(.glass)
        .buttonBorderShape(.circle)
        .accessibilityLabel("下载器操作")
        .accessibilityIdentifier("downloader-menu-\(downloader.name)")
    }

    // MARK: - 展开详情：连接 / 落盘 / 映射

    @ViewBuilder
    private var details: some View {
        SettingsBDlGroupLabel(text: "连接")
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text("地址").foregroundStyle(Theme.textMuted)
            Spacer(minLength: 8)
            if let url = URL(string: downloader.url) {
                // 同 Web：地址可点，在浏览器打开下载器 WebUI
                Link(destination: url) {
                    Label(downloader.url, systemImage: "arrow.up.right.square")
                        .labelStyle(SettingsBDlTrailingIconLabel())
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .accessibilityIdentifier("downloader-webui-\(downloader.name)")
            } else {
                Text(downloader.url).lineLimit(1).truncationMode(.middle)
            }
        }
        SettingsBValueRow(label: "用户名", value: downloader.username ?? "未设置")
        SettingsBValueRow(label: "版本", value: downloader.version ?? "—")

        SettingsBDlGroupLabel(text: "落盘")
        SettingsBValueRow(label: "默认保存目录", value: downloader.savePath ?? "下载器默认", mono: downloader.savePath != nil)

        SettingsBDlGroupLabel(text: "映射")
        SettingsBDlMappingTable(mappings: downloader.pathMappings ?? [], health: downloader.pathHealth ?? [])

        // Web 的「重新测试连接」只在 ⋯ 菜单里；手机上展开详情后顺手给一个直达按钮（同一操作）
        Button {
            onAction(.verify)
        } label: {
            HStack {
                Label(SettingsBDlStatus.inProgress(downloader.status) ? "测试中…" : "重新测试连接", systemImage: "bolt.horizontal")
                Spacer()
                if busy { ProgressView().controlSize(.small) }
            }
        }
        .disabled(busy || SettingsBDlStatus.inProgress(downloader.status))
        .accessibilityIdentifier("downloader-verify-\(downloader.name)")
    }
}

/// 状态胶囊：色点 + 文案（Web 的状态 pill）
struct SettingsBDlStatusPill: View {
    let label: String
    let tone: SettingsBTone

    var body: some View {
        HStack(spacing: 4) {
            SettingsBDot(tone: tone, size: 6)
            Text(label).font(.caption2.weight(.medium))
        }
        .foregroundStyle(tone == .neutral ? Theme.textMuted : tone.color)
        .padding(.horizontal, 7)
        .padding(.vertical, 2)
        .background((tone == .neutral ? Color.white : tone.color).opacity(0.12), in: .capsule)
        .fixedSize()
    }
}

/// 详情里的小段标签（Web 详情左侧的「连接 / 落盘 / 映射」）
private struct SettingsBDlGroupLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption.weight(.medium))
            .foregroundStyle(Theme.textFaint)
            .listRowSeparator(.hidden, edges: .bottom)
            .padding(.top, 4)
    }
}

/// 标题在前、图标在后的 Label 样式（外链箭头放右侧）
private struct SettingsBDlTrailingIconLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.title
            configuration.icon.font(.caption)
        }
    }
}

/// 路径映射对照表：一条映射一块，上面是 MovieClaw 看到的路径，下面是下载器看到的同一个位置。
///
/// 跨容器部署时两边对同一块盘叫不同名字，这里是核对「投递时路径会被翻成什么」的地方；
/// 每条挂本地侧的体检结论——配置「看起来对」而运行时挂载失效时，这是唯一不靠命令行
/// 就能看出「目录是空的」的位置（同 Web `PathMappingTable`）。
private struct SettingsBDlMappingTable: View {
    let mappings: [API.PathMapping]
    let health: [API.PathProbeView]

    var body: some View {
        if mappings.isEmpty {
            Text("未配置——MovieClaw 与下载器看到的是同一套路径。")
                .font(.subheadline)
                .foregroundStyle(Theme.textMuted)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("MovieClaw 视角")
                    Image(systemName: "arrow.right")
                    Text("下载器视角")
                }
                .font(.caption2.weight(.medium))
                .foregroundStyle(Theme.textFaint)
                ForEach(Array(mappings.enumerated()), id: \.offset) { _, mapping in
                    row(mapping)
                }
            }
            .padding(.vertical, 2)
        }
    }

    private func row(_ mapping: API.PathMapping) -> some View {
        let probe = health.first { $0.local == mapping.local }
        let broken = probe.map { $0.state != "ok" } ?? false
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(mapping.local)
                    .font(.footnote.monospaced())
                    .foregroundStyle(broken ? Theme.danger : Theme.text)
                    .lineLimit(2)
                    .truncationMode(.middle)
                if broken, let probe {
                    SettingsBBadge(text: SettingsBDlStatus.pathStateLabel(probe.state), tone: .danger)
                }
            }
            HStack(spacing: 6) {
                Image(systemName: "arrow.turn.down.right").font(.caption2).foregroundStyle(Theme.textFaint)
                Text(mapping.remote)
                    .font(.footnote.monospaced())
                    .foregroundStyle(Theme.text)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
            if broken, let probe {
                Text(probe.detail)
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .textSelection(.enabled)
    }
}
