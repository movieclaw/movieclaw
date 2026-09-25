import SwiftUI

/// 设置 → 订阅规则（对应 Web `subscription-settings-section.tsx`）。
///
/// 两个住户：
/// - **模拟一单**：搜一部片，把路由选库 / 选规则组 / 投递落点 / 入库方式完整预演一遍，
///   只调预览接口（`GET /subscriptions/download-routing-preview`），不会真的订阅；
/// - **规则组**：完整管理入口（新建 / 编辑 / 复制 / 设默认 / 删除），编辑器复用订阅模块的
///   `RuleSetEditorSheet`，摘要芯片复用 `RuleSetText.summary`，与订阅弹层看到的是同一套人话。
struct SubscriptionRulesSettingsView: View {
    var body: some View {
        Form {
            SettingsBSimulateSection()
            SettingsBRuleSetsSection()
        }
        .settingsBFormStyle()
    }
}

// MARK: - 模拟一单

/// 模拟一单：输入去抖 400ms 搜 TMDB（只取带类型的条目，路由需要 movie/tv 先验），
/// 选中后调预览接口，按 Web 同样的步骤编号展示结论。
private struct SettingsBSimulateSection: View {
    @Environment(\.api) private var api
    @State private var query = ""
    @State private var candidates: [API.DiscoveredTitleView]?
    @State private var searching = false
    @State private var picked: API.DiscoveredTitleView?
    @State private var preview: API.DispatchPreviewView?
    @State private var previewFailed = false

    var body: some View {
        Section {
            SettingsBIntro(text: "搜一部片，看它订阅后会进哪个库、用哪个规则组、投递到哪、怎么入库——只做预演，不会真的订阅。配完收藏范围或规则组的适用范围，在这里试一下就知道。")
            TextField("输入片名，如：葬送的芙莉莲", text: $query)
                .autocorrectionDisabled()
                .accessibilityIdentifier("simulate-query")
            if searching {
                Text("正在搜索…").font(.footnote).foregroundStyle(Theme.textFaint)
            } else if let candidates, picked == nil {
                if candidates.isEmpty {
                    Text("没有找到条目，换个关键词试试").font(.footnote).foregroundStyle(Theme.textFaint)
                } else {
                    SettingsBFlow {
                        ForEach(candidates, id: \.titleRef) { item in
                            Button {
                                pick(item)
                            } label: {
                                HStack(spacing: 6) {
                                    Text(item.title).font(.footnote.weight(.medium)).foregroundStyle(Theme.text)
                                    Text("\(item.releaseYear.map(String.init) ?? "") \(item.mediaType == "movie" ? "电影" : "剧集")")
                                        .font(.footnote).foregroundStyle(Theme.textFaint)
                                }
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(Color.white.opacity(0.07), in: .capsule)
                            }
                            .buttonStyle(.plain)
                            .accessibilityIdentifier("simulate-candidate")
                        }
                    }
                    .padding(.vertical, 4)
                }
            }
            if let picked {
                resultView(picked)
            }
        } header: {
            Text("模拟一单")
        }
        .task(id: query) { await search() }
    }

    @ViewBuilder
    private func resultView(_ item: API.DiscoveredTitleView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("《\(item.title)》\(item.releaseYear.map { " (\($0))" } ?? "")")
                    .font(.body.weight(.medium))
                Spacer()
                Button("重选") {
                    picked = nil
                    preview = nil
                }
                .font(.footnote)
                .buttonStyle(.borderless)
            }
            if let preview {
                let hasRuleSet = preview.ruleSetReason != nil
                SettingsBSimStep(n: 1, text: preview.routeReason ?? (preview.libraryName.map { "入库到「\($0)」" } ?? "没有可用的媒体库"))
                if let reason = preview.ruleSetReason {
                    SettingsBSimStep(n: 2, text: reason)
                }
                SettingsBSimStep(n: hasRuleSet ? 3 : 2, text: dispatchText(preview))
                if preview.stagingPath != nil {
                    SettingsBSimStep(n: hasRuleSet ? 4 : 3, text: "等待文件进入媒体库根目录后自动扫描入账（上传/转存等外部流转由你的工具完成）")
                }
                if preview.ok {
                    Label("全链路可行：订阅后即可自动下载并整理入库", systemImage: "checkmark")
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Theme.success)
                        .accessibilityIdentifier("simulate-ok")
                } else {
                    SettingsBNotice(text: preview.warning ?? "", tone: .warn)
                        .accessibilityIdentifier("simulate-warning")
                }
            } else if previewFailed {
                Text("预演失败，请重试").font(.footnote).foregroundStyle(Theme.danger)
            } else {
                Text("正在预演…").font(.footnote).foregroundStyle(Theme.textFaint)
            }
        }
        .padding(.vertical, 4)
    }

    private func dispatchText(_ p: API.DispatchPreviewView) -> String {
        switch p.mode {
        case "watch":
            if let staging = p.stagingPath {
                return "投递到自动入库的监听目录 \(p.path ?? "")，下载完成后识别改名并整理到 \(staging)"
            }
            return "投递到自动入库的监听目录 \(p.path ?? "")，下载完成后自动整理入库"
        case "inplace":
            // 条目目录由后端按命名模板渲染，前端不自己拼「标题 (年份)」
            var dir = p.entryDir ?? p.path ?? ""
            while dir.count > 1, dir.hasSuffix("/") { dir.removeLast() }
            return "直接下载到库内目录 \(dir)，完成后自动入账"
        default:
            return "落到下载器默认目录（不会自动入库）"
        }
    }

    private func search() async {
        picked = nil
        preview = nil
        let q = query.trimmingCharacters(in: .whitespaces)
        guard q.count >= 2 else {
            candidates = nil
            return
        }
        try? await Task.sleep(for: .milliseconds(400))
        if Task.isCancelled { return }
        searching = true
        defer { searching = false }
        do {
            let view = try await api.searchTitles(body: .init(query: q, provider: "tmdb", saveHistory: false))
            candidates = Array(view.titles.filter { $0.mediaType != nil }.prefix(6))
        } catch is CancellationError {
        } catch {
            candidates = []
        }
    }

    private func pick(_ item: API.DiscoveredTitleView) {
        picked = item
        preview = nil
        previewFailed = false
        guard let kind = item.mediaType else { return }
        Task {
            do {
                preview = try await api.subscriptionsPreviewDownloadRouting(
                    kind: kind, tmdbId: Int(item.externalId), title: item.title, year: item.releaseYear
                )
            } catch {
                previewFailed = true
            }
        }
    }
}

private struct SettingsBSimStep: View {
    let n: Int
    let text: String
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text("\(n)")
                .font(.caption2.weight(.semibold))
                .frame(width: 18, height: 18)
                .background(Color.white.opacity(0.1), in: .circle)
            Text(text)
                .font(.footnote)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("simulate-step")
    }
}

// MARK: - 规则组

/// 规则组清单（对应 Web `RuleSetsPanel` + `RuleSetRow`）：
/// 第一行组名（点它即编辑）与「默认」标，右侧 ··· 菜单收纳全部操作；第二行小字写适用范围与使用中的订阅数；
/// 第三行摘要芯片。删除保护前置：默认组与被订阅引用的组直接禁用删除项，并把原因写在菜单里。
private struct SettingsBRuleSetsSection: View {
    /// 编辑器打开参数：ruleSet 有值 = 编辑；nil = 新建，template 提供预填（复制场景）
    private struct EditorTarget: Identifiable {
        let id = UUID()
        var ruleSet: API.RuleSetView?
        var template: (name: String, spec: RuleSetSpec)?
    }

    @Environment(\.api) private var api
    @Environment(Feedback.self) private var feedback
    @State private var ruleSets: [API.RuleSetView]?
    @State private var routingOptions: LibraryRoutingOptions?
    @State private var editing: EditorTarget?
    @State private var error: String?

    var body: some View {
        Section {
            SettingsBIntro(text: "规则组定义「什么样的资源可接受」——硬性条件（分辨率、编码、体积、免费等）与偏好顺序。给规则组设置「适用范围」（电影/剧集、区域、类型）后，订阅时会自动选中匹配的组：多个组都匹配时条件更多的优先，都不匹配时用标「默认」的组。修改只影响之后的资源评估，已下载的内容不受影响。")
            if let error {
                SettingsBNotice(text: error, tone: .danger)
            }
            if let ruleSets {
                ForEach(ruleSets, id: \.id) { rule in
                    row(rule)
                }
            } else {
                Text("正在加载…").foregroundStyle(Theme.textMuted)
            }
        } header: {
            HStack {
                Text("规则组")
                Spacer()
                Button("+ 新建规则组") { editing = EditorTarget() }
                    .font(.footnote.weight(.medium))
                    .buttonStyle(.glass)
                    .textCase(nil)
                    .accessibilityIdentifier("ruleset-create")
            }
        }
        .task { await reload() }
        .sheet(item: $editing) { target in
            RuleSetEditorSheet(ruleSet: target.ruleSet, template: target.template) { _ in
                Task { await reload() }
            }
            .sheetFeedback()
        }
    }

    private func row(_ rule: API.RuleSetView) -> some View {
        let chips = RuleSetText.summary(rule.typedSpec)
        let scope = RuleSetScope(rule.matchRules).summary(routingOptions)
        let deleteBlock: String? = rule.isDefault ? "默认组不可删"
            : rule.referenceCount > 0 ? "\(rule.referenceCount) 个订阅在用" : nil
        let usage = rule.referenceCount > 0 ? " · \(rule.referenceCount) 个订阅使用中" : ""
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Button {
                    editing = EditorTarget(ruleSet: rule)
                } label: {
                    Text(rule.name).font(.body.weight(.semibold)).foregroundStyle(Theme.text).lineLimit(1)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("ruleset-name-\(rule.name)")
                if rule.isDefault { SettingsBBadge(text: "默认") }
                Spacer(minLength: 8)
                Menu {
                    Button("编辑", systemImage: "pencil") { editing = EditorTarget(ruleSet: rule) }
                    Button("复制为新组", systemImage: "plus.square.on.square") {
                        editing = EditorTarget(template: (name: "\(rule.name) 副本", spec: rule.typedSpec))
                    }
                    Button(rule.isDefault ? "已是默认组" : "设为默认组", systemImage: "star") {
                        Task { await makeDefault(rule) }
                    }
                    .disabled(rule.isDefault)
                    Divider()
                    Button(role: .destructive) {
                        Task { await remove(rule) }
                    } label: {
                        Label("删除", systemImage: "trash")
                        if let deleteBlock { Text(deleteBlock) }
                    }
                    .disabled(deleteBlock != nil)
                } label: {
                    Image(systemName: "ellipsis")
                        .frame(width: 30, height: 30)
                        .contentShape(.rect)
                }
                .buttonStyle(.glass)
                .buttonBorderShape(.circle)
                .accessibilityLabel("「\(rule.name)」的操作")
                .accessibilityIdentifier("ruleset-menu-\(rule.name)")
            }
            Group {
                if let scope {
                    Text("适用 \(Text(scope).foregroundStyle(Theme.textMuted))\(usage)").foregroundStyle(Theme.textFaint)
                } else {
                    Text((rule.isDefault ? "其他组都不适用时兜底" : "未设适用范围，仅手动选用") + usage)
                        .foregroundStyle(Theme.textFaint)
                }
            }
            .font(.caption)
            if chips.isEmpty {
                Text("全不限：任何识别为本条目的资源都可接受").font(.caption).foregroundStyle(Theme.textFaint)
            } else {
                SettingsBFlow {
                    ForEach(chips, id: \.self) { SettingsBChip(text: $0) }
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func reload() async {
        if routingOptions == nil { routingOptions = try? await api.routingOptions() }
        do {
            ruleSets = try await api.rulesList()
            error = nil
        } catch is CancellationError {
        } catch {
            if ruleSets == nil { ruleSets = [] }
        }
    }

    private func makeDefault(_ rule: API.RuleSetView) async {
        do {
            _ = try await api.rulesDefault(ruleSetId: rule.id)
            feedback.success("「\(rule.name)」已设为默认规则组")
            await reload()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "设置失败，请稍后重试" : error.localizedDescription
        }
    }

    private func remove(_ rule: API.RuleSetView) async {
        guard await feedback.confirm("删除规则组「\(rule.name)」？", confirmTitle: "删除", destructive: true) else { return }
        do {
            _ = try await api.rulesDelete(ruleSetId: rule.id)
            await reload()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "删除失败，请稍后重试" : error.localizedDescription
        }
    }
}
