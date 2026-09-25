import SwiftUI

// 自动入库规则卡片（对应 Web `RuleCard` + `RuleEntriesPanel`）与页面共用的小类型。

/// 编辑器打开参数：rule 为 nil = 新建
struct SettingsBIwEditorTarget: Identifiable {
    let id = UUID()
    var rule: API.ImportWatchView?
}

/// 规则下方展开中的条目清单（数据放在根视图，按规则 id 存）
struct SettingsBIwPanel {
    var status: String
    var data: API.IngestEntriesView?
    var failed = false
}

/// 下载器目录候选：源目录大概率就是下载器的某个目录，供表单一键填入
struct SettingsBIwDirOption: Hashable {
    /// movieclaw 视角的目录（默认保存目录或路径映射左列）
    let path: String
    /// 来源下载器名称
    let downloaderName: String

    /// 从下载器配置里收集 movieclaw 视角的目录候选（去重，保持配置顺序）
    static func collect(_ downloaders: [API.DownloaderView]) -> [SettingsBIwDirOption] {
        var seen = Set<String>()
        var options: [SettingsBIwDirOption] = []
        for d in downloaders {
            let paths = [d.savePath] + (d.pathMappings ?? []).map(\.local)
            for case let path? in paths where !path.isEmpty && !seen.contains(path) {
                seen.insert(path)
                options.append(SettingsBIwDirOption(path: path, downloaderName: d.name))
            }
        }
        return options
    }
}

/// 台账四个状态页签（顺序、文案、色调同 Web ENTRY_TABS）
enum SettingsBIwTab: String, CaseIterable {
    case pending, failed, imported, ignored

    var label: String {
        switch self {
        case .pending: "待处理"
        case .failed: "失败"
        case .imported: "已入库"
        case .ignored: "已忽略"
        }
    }

    var color: Color {
        switch self {
        case .pending: Theme.warning
        case .failed: Theme.danger.mix(with: .white, by: 0.3)
        case .imported, .ignored: Theme.textMuted
        }
    }
}

/// 一条规则 = 一个 Section：头行（路径 / 策略 → 目标 / 编辑 / 删除）+ 台账摘要行 + 可展开的条目清单。
///
/// 摘要行是分档结论的承载面：待处理不是错误、不进全局告警，用户在这里看到数字、
/// 点开清单认领或忽略。「已入库」报作品数（同剧多季 / 多版本算一部），条目数与作品数不同时
/// 补注条目与文件数，说清实际规模（同 Web）。
struct SettingsBIwRuleSection: View {
    let rule: API.ImportWatchView
    let movie: Bool
    let panel: SettingsBIwPanel?
    let onEdit: () -> Void
    let onRemove: () -> Void
    let onToggleTab: (String) -> Void
    let onEntriesChanged: () -> Void

    private var total: Int {
        SettingsBIwTab.allCases.reduce(0) { $0 + (rule.stats[$1.rawValue] ?? 0) }
    }

    var body: some View {
        Section {
            header
            if total > 0 {
                statsRow
            }
            if let panel {
                entries(panel)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "folder")
                .foregroundStyle(Theme.accent.opacity(0.8))
            VStack(alignment: .leading, spacing: 3) {
                Text(rule.sourcePath)
                    .font(.subheadline.monospaced())
                    .foregroundStyle(Theme.text)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Text("\(rule.strategy == "hardlink" ? "硬链接" : "复制") → \(rule.targetLabel)\(rule.processExisting ? "" : "（跳过存量）")")
                    .font(.caption)
                    .foregroundStyle(Theme.textMuted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("编辑", action: onEdit)
                .font(.footnote.weight(.medium))
                .buttonStyle(.glass)
                .accessibilityIdentifier("import-watch-edit-\(rule.id)")
            Button("删除对 \(rule.sourcePath) 的监听", systemImage: "xmark", action: onRemove)
                .labelStyle(.iconOnly)
                .foregroundStyle(Theme.textFaint)
                .buttonStyle(.borderless)
                .accessibilityIdentifier("import-watch-delete-\(rule.id)")
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("import-watch-rule-\(rule.id)")
    }

    /// 台账摘要：非零状态成为可点开的过滤标签
    private var statsRow: some View {
        SettingsBFlow(spacing: 6, lineSpacing: 6) {
            ForEach(SettingsBIwTab.allCases.filter { (rule.stats[$0.rawValue] ?? 0) > 0 }, id: \.self) { tab in
                let active = panel?.status == tab.rawValue
                Button {
                    onToggleTab(tab.rawValue)
                } label: {
                    Text(label(tab))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(tab.color)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(Color.white.opacity(active ? 0.16 : 0.06), in: .capsule)
                        .overlay(Capsule().strokeBorder(Color.white.opacity(active ? 0.25 : 0.08)))
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("import-watch-tab-\(rule.id)-\(tab.rawValue)")
                .accessibilityAddTraits(active ? .isSelected : [])
            }
        }
        .padding(.vertical, 2)
    }

    private func label(_ tab: SettingsBIwTab) -> String {
        let count = rule.stats[tab.rawValue] ?? 0
        guard tab == .imported, rule.importedWorks > 0 else { return "\(tab.label) \(count)" }
        var text = "\(tab.label) \(rule.importedWorks) 部"
        if rule.importedFiles > 0 && count > rule.importedWorks {
            text += "（\(count) 个条目 · \(rule.importedFiles) 个文件）"
        }
        return text
    }

    @ViewBuilder
    private func entries(_ panel: SettingsBIwPanel) -> some View {
        if panel.failed {
            Text("清单加载失败，请重试。").font(.caption).foregroundStyle(Theme.textMuted)
        } else if let data = panel.data {
            if data.entries.isEmpty {
                Text("该状态下暂无条目。").font(.caption).foregroundStyle(Theme.textFaint)
            } else {
                if panel.status == SettingsBIwTab.pending.rawValue {
                    Text("这些条目无法自动识别，不会重复尝试也不会报错：搜索认领后立即整理入库；确认不需要整理的（花絮、自录、其他工具管理的内容）可忽略。")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(data.entries, id: \.id) { entry in
                    SettingsBIwEntryRow(entry: entry, movie: movie, onChanged: onEntriesChanged)
                }
            }
        } else {
            Text("加载中…").font(.caption).foregroundStyle(Theme.textFaint)
        }
    }
}
