import SwiftUI

/// 「洗一轮版」弹层（对应 Web `components/upgrade-run-dialog.tsx`，quality-upgrade.md §13.4/§13.5）。
///
/// 两段式：确认段 → 报告段。确认段把既有订阅的三种状态并成一条动线：
/// - 规则组未配洗版目标 → 必须先选一个带洗版目标的组（含快捷新建），「选组 + 触发」由
///   upgrade-runs 的 rule_set_id 参数合成一次调用；
/// - 库里有文件但不在订阅范围的季 → 如实列出供勾选并入（并入会连带补缺下载，是用户决策）；
/// - 订阅已暂停 → 提示并改为「恢复并触发」。
/// 报告段渲染后端体检快照，不落库、不轮询——后续进展看追踪明细的「洗版中」徽标。
struct UpgradeRunSheet: View {
    let detail: API.SubscriptionDetailView
    /// 一轮洗版已触发（报告看完关闭）后回调，父页面刷新。
    /// 报告态下任何关闭方式（「完成」、左上关闭、下滑）都会回调，同 Web 报告态 Modal 的 onClose = onFinished
    let onFinished: () -> Void

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(\.dismiss) private var dismiss

    @State private var ruleSets: [API.RuleSetView]?
    @State private var ruleSetId: Int?
    @State private var creatingRuleSet = false
    @State private var optedSeasons: Set<Int> = []
    @State private var busy = false
    @State private var error: String?
    @State private var report: API.UpgradeRunView?
    /// 报告里点了「标注片源」的季（电影为 0）
    @State private var annotateSeason: Int?

    private var isMovie: Bool { detail.media.kind == "movie" }
    private var paused: Bool { detail.status == "paused" }
    private var upgradeRules: [API.RuleSetView] { (ruleSets ?? []).filter { $0.upgradeTarget != nil } }
    private var currentHasTarget: Bool { (ruleSets ?? []).first { $0.id == detail.ruleSetId }?.upgradeTarget != nil }
    private var selectedRule: API.RuleSetView? { upgradeRules.first { $0.id == ruleSetId } }
    private var outOfScopeOwned: [API.SeasonOverview] {
        isMovie ? [] : detail.seasonCollection.filter { $0.ownedCount > 0 && !detail.selectedSeasons.contains($0.seasonNumber) }
    }

    var body: some View {
        Group {
            if let report {
                SubsSheetScaffold(
                    title: "洗版体检报告",
                    closable: false,
                    confirm: SubsSheetConfirm(title: "完成", identifier: "upgrade-report-done", action: finish),
                    fullHeight: true
                ) {
                    UpgradeRunReportView(
                        title: detail.media.title,
                        isMovie: isMovie,
                        report: report,
                        onAnnotate: { annotateSeason = $0 }
                    )
                }
            } else {
                confirm
            }
        }
        .interactiveDismissDisabled(busy)
        .sheet(isPresented: $creatingRuleSet) {
            RuleSetEditorSheet(ruleSet: nil) { saved in
                ruleSets = (ruleSets ?? []) + [saved]
                // 新建组带洗版目标时自动选中（快捷新建的动机就是没得选）
                if saved.upgradeTarget != nil { ruleSetId = saved.id }
            }
        }
        .sheet(item: Binding(get: { annotateSeason.map(SeasonKey.init) }, set: { annotateSeason = $0?.season })) { key in
            MediaSourceAnnotationSheet(mediaItemId: detail.media.mediaItemId, seasonNumber: key.season, isMovie: isMovie) { _ in
                // 标注已刷新快照：重跑一轮体检，报告当场翻新
                if let fresh = try? await api.subscriptionsUpgradeRun(subscriptionId: detail.id, body: .init(ruleSetId: nil)) {
                    self.report = fresh
                }
            }
        }
        .task { await loadRules() }
        .onDisappear {
            if report != nil { onFinished() }
        }
    }

    private func finish() {
        dismiss()
    }

    private var confirm: some View {
        SubsSheetScaffold(
            title: "洗一轮版",
            subtitle: "逐集检查《\(detail.media.title)》库里已有的版本，低于洗版目标的立即排入搜索；洗到新版本入库并验证通过后，旧文件自动替换。",
            confirm: SubsSheetConfirm(
                title: paused ? "恢复并触发洗版" : "开始体检并洗版",
                enabled: selectedRule != nil,
                busy: busy,
                identifier: "upgrade-run-start"
            ) { Task { await run() } },
            ready: ruleSets != nil || error != nil
        ) {
            if let error {
                Section { SubsNoticeRow(text: error, tone: .error) }
            }
            if paused {
                Section { SubsNoticeRow(text: "该订阅已暂停。触发洗版会先恢复追踪，随后开始搜索。", tone: .warn) }
            }

            Section {
                if ruleSets == nil {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("正在加载规则组…").foregroundStyle(Theme.textMuted)
                    }
                } else {
                    ForEach(upgradeRules, id: \.id) { rule in
                        SubsChoiceRow(
                            title: rule.name + (rule.id == detail.ruleSetId ? "（当前使用）" : ""),
                            subtitle: "洗到 \(rule.upgradeTarget ?? "")",
                            selected: rule.id == ruleSetId,
                            tint: SubsColor.upgrade
                        ) {
                            ruleSetId = rule.id
                        }
                        .disabled(busy)
                        .accessibilityIdentifier("upgrade-rule-option")
                    }
                    if permissions.canManageSubscriptions {
                        Button("新建规则组…", systemImage: "plus") { creatingRuleSet = true }
                    }
                }
            } header: {
                Text("洗版目标")
            } footer: {
                if ruleSets != nil {
                    if upgradeRules.isEmpty {
                        Text("还没有配置洗版目标的规则组。" + (permissions.canManageSubscriptions ? "新建一个，在编辑器里选择「洗到哪一档」即可。" : "请联系管理员在「设置 → 订阅规则 → 规则组」中配置「洗到哪一档」。"))
                    } else if !currentHasTarget {
                        Text("当前规则组未配置洗版目标，选一个带洗版目标的组，确认后一并换用。")
                    }
                }
            }

            if !outOfScopeOwned.isEmpty {
                Section {
                    ForEach(outOfScopeOwned, id: \.seasonNumber) { season in
                        let stock = "库存 \(season.ownedCount)" + ((season.episodeCount.map { season.ownedCount < $0 } ?? false) ? " / \(season.episodeCount ?? 0)" : "") + " 集"
                        SubsChoiceRow(
                            title: SubsFormat.seasonName(season.seasonNumber),
                            subtitle: stock,
                            selected: optedSeasons.contains(season.seasonNumber)
                        ) {
                            if optedSeasons.contains(season.seasonNumber) { optedSeasons.remove(season.seasonNumber) } else { optedSeasons.insert(season.seasonNumber) }
                        }
                        .disabled(busy)
                    }
                } header: {
                    Text("范围外的库存季")
                } footer: {
                    Text("这些季库里有文件但不在订阅范围内，勾选后并入订阅一起洗版；季内缺集会一并搜索补齐。")
                }
            }
        }
        .accessibilityIdentifier("upgrade-run-sheet")
    }

    private func loadRules() async {
        do {
            let rules = try await api.rulesList()
            ruleSets = rules
            // 现用组已配洗版目标 → 预选它；否则只有一个可选组时也预选
            if let current = rules.first(where: { $0.id == detail.ruleSetId }), current.upgradeTarget != nil {
                ruleSetId = current.id
            } else {
                let candidates = rules.filter { $0.upgradeTarget != nil }
                ruleSetId = candidates.count == 1 ? candidates[0].id : nil
            }
        } catch is CancellationError {
        } catch {
            self.error = "未能加载规则组列表，请稍后重试"
        }
    }

    private func run() async {
        guard let selectedRule else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            if !optedSeasons.isEmpty {
                _ = try await api.subscriptionsUpdate(
                    subscriptionId: detail.id,
                    body: .init(selectedSeasons: (detail.selectedSeasons + optedSeasons).sorted())
                )
            }
            if paused {
                _ = try await api.subscriptionsSetTrackingState(subscriptionId: detail.id, body: .init(state: "active"))
            }
            report = try await api.subscriptionsUpgradeRun(
                subscriptionId: detail.id,
                body: .init(ruleSetId: selectedRule.id != detail.ruleSetId ? selectedRule.id : nil)
            )
        } catch {
            self.error = error.localizedDescription.isEmpty ? "触发洗版失败，请稍后重试" : error.localizedDescription
        }
    }
}

/// 体检报告段：摘要句 + 按季分组的单元列表（一次性快照），直接产出表单 Section，放进 `SubsSheetScaffold`。
/// 订阅弹层的洗版变体（建订阅后自动接一轮洗版）复用本段。
///
/// 「标注片源」弹层不挂在这里：原生列表的行按需创建，挂在行上的弹层滚出屏幕后可能失效，
/// 所以只回调季号，由宿主弹层统一呈现。
struct UpgradeRunReportView: View {
    let title: String
    let isMovie: Bool
    let report: API.UpgradeRunView
    /// 有值时（且为管理员）「无法确认」的季显示「标注片源」入口，参数为季号（电影为 0）
    var onAnnotate: ((Int) -> Void)?

    @Environment(\.permissions) private var permissions

    static func stateMeta(_ state: String) -> (label: String, color: Color) {
        switch state {
        case "upgradable": ("已排洗版", SubsColor.upgrade)
        case "in_flight": ("洗版中", SubsColor.upgradeInFlight)
        case "at_cutoff": ("已达目标", SubsColor.ok)
        case "not_comparable": ("无法确认", SubsColor.neutral)
        default: ("缺失", SubsColor.warn)
        }
    }

    static func note(_ unit: API.UpgradeRunUnitView) -> String {
        switch unit.state {
        case "upgradable": "当前 \(unit.currentLabel ?? "未知") → 目标 \(unit.targetLabel)，已排入立即搜索"
        case "in_flight": "已投递新版本，等待入库验证"
        case "at_cutoff": "当前 \(unit.currentLabel ?? "未知")，已达洗版目标"
        case "not_comparable": "无法确认当前版本是否低于目标，不自动洗；可「标注片源」告知系统，或用「手动选种」直接替换"
        default: "库里没有该单元，将照常搜索下载补齐"
        }
    }

    var body: some View {
        let seasons = Dictionary(grouping: report.units, by: \.seasonNumber)
        let annotatable: Set<Int> = (onAnnotate != nil && permissions.isAdmin)
            ? Set(report.units.filter { $0.state == "not_comparable" }.map(\.seasonNumber)) : []
        let keys = seasons.keys.sorted()
        Section {
            SubsNoticeRow(text: report.summary, tone: .upgrade)
                .accessibilityIdentifier("upgrade-report-summary")
        } header: {
            Text("《\(title)》· 目标 \(report.targetLabel)")
        } footer: {
            if keys.isEmpty { footerNote }
        }
        ForEach(keys, id: \.self) { season in
            Section {
                ForEach(seasons[season] ?? [], id: \.episodeNumber) { unit in
                    unitRow(unit)
                }
            } header: {
                if !isMovie || annotatable.contains(season) {
                    HStack {
                        Text(isMovie ? "正片" : SubsFormat.seasonName(season))
                        Spacer()
                        if annotatable.contains(season) {
                            Button("标注片源") { onAnnotate?(season) }
                                .font(.caption.weight(.medium)).buttonStyle(.bordered).controlSize(.mini)
                        }
                    }
                }
            } footer: {
                if season == keys.last { footerNote }
            }
        }
    }

    private var footerNote: some View {
        Text("后续进展在订阅详情的「追踪明细」里跟进：洗版中的单元带青色徽标，换版成功会记入活动记录。")
    }

    private func unitRow(_ unit: API.UpgradeRunUnitView) -> some View {
        let meta = Self.stateMeta(unit.state)
        return HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(isMovie ? "正片" : "E\(SubsFormat.pad(unit.episodeNumber))")
                .font(.subheadline.weight(.medium)).monospacedDigit().foregroundStyle(Theme.text)
                .frame(width: 44, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(meta.label)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(meta.color)
                    .padding(.horizontal, 7).padding(.vertical, 2)
                    .background(meta.color.opacity(0.13), in: .capsule)
                Text(Self.note(unit)).font(.footnote).foregroundStyle(Theme.textMuted).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
    }
}

/// `.sheet(item:)` 用的季号包装
struct SeasonKey: Identifiable, Hashable {
    var season: Int
    var id: Int { season }
}
