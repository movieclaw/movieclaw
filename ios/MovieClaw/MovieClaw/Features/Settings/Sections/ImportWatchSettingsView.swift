import SwiftUI

/// 设置 → 自动入库（对应 Web `import-watch-section.tsx`，原名「监听导入」）：媒体库之上的独立功能。
///
/// 每条规则 = 监听一个源目录，目录里下载完成的内容（下载器确认或指纹静默 + 探测通过）自动识别，
/// 按规范命名搬进目标库主根或自定义目录。媒体库本身只有一套目录体系、不承载下载语义——
/// 需要「下载区 → 库」搬运的用户在这里独立配置。
///
/// 页面结构：
/// - 每条规则一个 Section：头行（源目录 / 策略 → 目标 / 编辑 / 删除）+ 台账摘要标签；
///   点摘要标签（待处理 / 失败 / 已入库 / 已忽略）在规则下方展开该状态的条目清单，再点收起（同 Web）；
/// - 条目清单的数据放在根视图按规则 id 存（`panels`），不在行上挂 `.task`——
///   Form 会把 Section / ForEach 上的修饰符分发给每一行；
/// - 新建 / 编辑走弹层 `SettingsBIwEditorSheet`，挂在 Form 根上。
///
/// Web 无轮询（条目动作后主动刷新），这里同样不轮询。
struct ImportWatchSettingsView: View {
    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    /// 深链参数：体检修复卡「去建规则」带 `suggest=auto&kinds=movie,tv`（Web 同名查询串）
    @Environment(\.routeQuery) private var routeQuery
    /// 预填队列：还要按「自动路由」新建的类型（电影建完接着建剧集）；用户中途关闭即放弃
    @State private var suggestKinds: [String] = []
    /// 刚保存了一条、队列里还有下一类型：弹层关闭后接着打开下一条的预填
    @State private var suggestNext = false
    @State private var routeQueryConsumed = false

    @State private var rules: [API.ImportWatchView]?
    @State private var libraries: [API.LibraryView] = []
    @State private var downloaderDirs: [SettingsBIwDirOption] = []
    @State private var failed = false
    @State private var error: String?
    @State private var editor: SettingsBIwEditorTarget?
    /// 规则 id → 展开中的条目清单（同一规则同时只开一个状态）
    @State private var panels: [Int: SettingsBIwPanel] = [:]

    var body: some View {
        Form {
            Section {
                Text("监听下载目录，其中\(Text("下载完成").fontWeight(.medium).foregroundStyle(Theme.text))的内容（下载器确认完成，或文件持续静默且探测通过）自动识别、按「标题 (年份)」规范命名搬进目标媒体库或你指定的自定义目录。源文件原地保留：硬链接零占用、可继续做种；复制适合跨盘。把下载器的保存目录设为这里的源目录，即可实现「下载完成自动整理入库」。")
                    .font(.footnote)
                    .foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                if let error {
                    SettingsBNotice(text: error, tone: .danger)
                        .accessibilityIdentifier("import-watch-error")
                }
                if failed {
                    HStack {
                        Text("自动入库配置加载失败").foregroundStyle(Theme.textMuted)
                        Spacer()
                        Button("重试") { Task { await reload() } }
                            .buttonStyle(.glass)
                            .accessibilityIdentifier("import-watch-retry")
                    }
                }
            }

            if let rules, !failed {
                if rules.isEmpty {
                    Section {
                        Text("还没有自动入库规则。不需要「下载区 → 库」自动搬运的话，这里保持为空即可。")
                            .font(.subheadline)
                            .foregroundStyle(Theme.textMuted)
                            .frame(maxWidth: .infinity)
                            .multilineTextAlignment(.center)
                            .padding(.vertical, 12)
                            .accessibilityIdentifier("import-watch-empty")
                    }
                }
                ForEach(rules, id: \.id) { rule in
                    SettingsBIwRuleSection(
                        rule: rule,
                        movie: isMovie(rule),
                        panel: panels[rule.id],
                        onEdit: { editor = SettingsBIwEditorTarget(rule: rule) },
                        onRemove: { Task { await remove(rule) } },
                        onToggleTab: { status in Task { await toggle(rule, status) } },
                        onEntriesChanged: { Task { await entriesChanged(rule) } }
                    )
                }
                Section {
                    Button {
                        editor = SettingsBIwEditorTarget(rule: nil)
                    } label: {
                        Label("添加自动入库规则", systemImage: "plus")
                            .frame(maxWidth: .infinity)
                    }
                    .accessibilityIdentifier("import-watch-add")
                }
            } else if rules == nil && !failed {
                Section {
                    ProgressView().frame(maxWidth: .infinity).padding(.vertical, 12)
                }
            }
        }
        .settingsBFormStyle()
        .task {
            await reload()
            consumeRouteQuery()
        }
        .sheet(item: $editor, onDismiss: {
            if suggestNext, let kind = suggestKinds.first {
                suggestNext = false
                editor = SettingsBIwEditorTarget(rule: nil, initialTarget: .auto(kind: kind))
            } else {
                suggestNext = false
                suggestKinds = [] // 用户中途关闭即放弃剩余预填队列（同 Web）
            }
        }) { target in
            SettingsBIwEditorSheet(rule: target.rule, libraries: libraries, downloaderDirs: downloaderDirs,
                                   initialTarget: target.initialTarget) {
                Task { await reload() }
                // 还有下一个类型要建：关窗后接着预填下一类型（Web 弹窗不关、表单重置为下一类型）
                if target.initialTarget != nil, suggestKinds.count > 1 {
                    suggestKinds.removeFirst()
                    suggestNext = true
                }
            }
            .sheetFeedback()
        }
    }

    /// 体检修复卡跳转落地（Web `?suggest=auto&kinds=movie,tv`）：自动打开新建规则并预选「自动路由」，
    /// 多个类型排成队列；kinds 缺省或全非法时按电影
    private func consumeRouteQuery() {
        guard !routeQueryConsumed else { return }
        routeQueryConsumed = true
        guard routeQuery["suggest"] == "auto" else { return }
        let kinds = (routeQuery["kinds"] ?? "").split(separator: ",").map(String.init).filter { $0 == "movie" || $0 == "tv" }
        suggestKinds = kinds.isEmpty ? ["movie"] : kinds
        editor = SettingsBIwEditorTarget(rule: nil, initialTarget: .auto(kind: suggestKinds[0]))
    }

    /// 条目是电影还是剧集：指定库看库类型，自动路由 / 自定义目录看规则声明（同 Web）
    private func isMovie(_ rule: API.ImportWatchView) -> Bool {
        if let libraryId = rule.libraryId {
            return (libraries.first { $0.id == libraryId }?.kind ?? "movie") == "movie"
        }
        return (rule.kind ?? "movie") == "movie"
    }

    // MARK: - 数据

    private func reload() async {
        failed = false
        do {
            async let ruleRows = api.watchList()
            async let libs = api.libraryList(scope: "all")
            let (r, l) = try await (ruleRows, libs)
            rules = r
            libraries = l
        } catch is CancellationError {
        } catch {
            failed = true
        }
        // 下载器目录只是表单的快捷候选，拉取失败不影响主功能，静默降级为无候选
        if let rows = try? await api.dlList() {
            downloaderDirs = SettingsBIwDirOption.collect(rows)
        } else {
            downloaderDirs = []
        }
    }

    private func remove(_ rule: API.ImportWatchView) async {
        guard await feedback.confirm(
            "删除对 \(rule.sourcePath) 的监听？",
            message: "源目录与已导入的文件都不受影响，只是停止监听。",
            confirmTitle: "删除",
            destructive: true
        ) else { return }
        error = nil
        do {
            _ = try await api.watchDelete(ruleId: rule.id)
            panels[rule.id] = nil
            await reload()
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
            feedback.error(error)
        }
    }

    /// 点摘要标签：同一个再点收起，否则切到该状态并拉清单
    private func toggle(_ rule: API.ImportWatchView, _ status: String) async {
        if panels[rule.id]?.status == status {
            panels[rule.id] = nil
            return
        }
        panels[rule.id] = SettingsBIwPanel(status: status)
        await loadEntries(ruleId: rule.id, status: status)
    }

    private func loadEntries(ruleId: Int, status: String) async {
        do {
            let data = try await api.watchEntries(ruleId: ruleId, status: status)
            // 期间用户可能已切走或收起，只回填仍对应的面板
            guard panels[ruleId]?.status == status else { return }
            panels[ruleId]?.data = data
            panels[ruleId]?.failed = false
        } catch is CancellationError {
        } catch {
            guard panels[ruleId]?.status == status else { return }
            panels[ruleId]?.failed = true
        }
    }

    /// 条目动作（认领 / 忽略 / 恢复）会改变计数：刷新清单与规则的 stats
    private func entriesChanged(_ rule: API.ImportWatchView) async {
        if let status = panels[rule.id]?.status {
            await loadEntries(ruleId: rule.id, status: status)
        }
        if let rows = try? await api.watchList() { rules = rows }
    }
}
