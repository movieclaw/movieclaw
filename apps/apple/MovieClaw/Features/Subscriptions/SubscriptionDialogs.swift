import SwiftUI

// 订阅详情与订阅弹层共用的对话框：选季行、投递路由预检说明、取消订阅（管理员）、
// 减季后的清理追问、调整订阅、更换规则组、管理菜单。
// 文案与判定逐条对应 Web 同名组件（subscription-cancel-dialog / season-cleanup-dialog /
// subscription-adjust-dialog / RuleSetSwitchDialog / SubscriptionManageSheet）。

// MARK: - 选季行

/// 季选择行：季名 + 播出进度 + 库存提示；未播季可勾（勾了 = 要整季）。
/// 订阅弹层与调整订阅共用（Web SeasonRow）。
struct SeasonPickRow: View {
    let season: API.SeasonOverview
    let checked: Bool
    let onToggle: () -> Void

    private var progress: String {
        let total = season.episodeCount ?? 0
        if total > 0, season.airedCount >= total { return "全 \(total) 集已播完" }
        if total > 0 { return "已播 \(season.airedCount)/\(total) 集" }
        return season.airedCount > 0 ? "已播 \(season.airedCount) 集" : "未播出"
    }

    private var owned: String? {
        let total = season.episodeCount ?? 0
        guard season.ownedCount > 0 else { return nil }
        return total > 0 && season.ownedCount >= total ? "整季已在库" : "库里已有 \(season.ownedCount) 集"
    }

    var body: some View {
        Button(action: onToggle) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(SubsFormat.seasonName(season.seasonNumber)).foregroundStyle(Theme.text)
                    HStack(spacing: 6) {
                        Text(progress).foregroundStyle(Theme.textMuted)
                        if let owned {
                            Text(owned).foregroundStyle(SubsColor.ok.opacity(0.9))
                        }
                    }
                    .font(.footnote).monospacedDigit()
                }
                Spacer(minLength: 4)
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(Theme.accentStrong)
                    .opacity(checked ? 1 : 0)
            }
            .contentShape(.rect)
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(checked ? .isSelected : [])
        .accessibilityIdentifier("season-\(season.seasonNumber)")
    }
}

// MARK: - 投递路由预检

/// 选库即预演「下载会落到哪、能否自动入库」；配置问题当场亮出（后端与真实投递同源判定）
///
/// 两处口径不同（照搬 Web）：订阅弹层的监听模式会多说一句暂存整理去向、库内目录去掉尾斜杠；
/// 调整订阅弹层（`adjusting`）只说监听目录、目录原样显示。
struct DispatchPreviewNote: View {
    let preview: API.DispatchPreviewView
    var adjusting = false

    // 放在玻璃弹层的表单脚注里：字号与对比度比系统脚注提一档，否则虚化背景上看不清；警示用图标 + 文字，不在脚注里套色块
    var body: some View {
        if preview.ok {
            Text(text)
                .font(.footnote)
                .foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("dispatch-preview")
        } else {
            Label(preview.warning ?? "按当前配置投递无法自动入库", systemImage: "exclamationmark.triangle.fill")
                .font(.footnote)
                .foregroundStyle(SubsColor.warn)
                .accessibilityIdentifier("dispatch-preview")
        }
    }

    private var text: String {
        if preview.mode == "watch" {
            let path = preview.path ?? ""
            if !adjusting, let staging = preview.stagingPath {
                return "将投递到自动入库的监听目录 \(path)，下载完成后整理到 \(staging)，文件进入媒体库根目录后自动入账"
            }
            return "将投递到自动入库的监听目录 \(path)，下载完成后自动整理入库"
        }
        // 条目目录由后端按命名模板渲染（entry_dir），不自己拼「标题 (年份)」
        var dir = preview.entryDir ?? preview.path ?? ""
        while !adjusting, dir.hasSuffix("/") { dir.removeLast() }
        return "将直接下载到库内目录 \(dir)，完成后自动入账"
    }
}

// MARK: - 取消订阅（管理员）

/// 取消订阅确认（管理员）：默认只取消订阅、什么都不删；「删下载任务」「删媒体库文件」两个显式勾选，
/// 勾之前就把数量与后果讲清。数量来自 removal-preview，预览没回来时两个开关先禁用（不让用户盲签）。
struct SubscriptionCancelSheet: View {
    let subscriptionId: Int
    let title: String
    /// 用户确认：带两个清理开关的最终取值
    let onConfirm: (_ deleteTorrents: Bool, _ deleteLibraryFiles: Bool) async -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss
    @State private var preview: API.SubscriptionRemovalPreviewView?
    @State private var deleteTorrents = false
    @State private var deleteFiles = false
    @State private var busy = false

    var body: some View {
        let torrentCount = preview?.torrentCount ?? 0
        let fileCount = preview?.libraryFileCount ?? 0
        SubsSheetScaffold(
            title: "取消订阅",
            subtitle: "取消订阅《\(title)》将停止追踪剩余内容。默认只取消订阅，已经下载或入库的内容都会保留。",
            closeTitle: "先不",
            ready: preview != nil
        ) {
            Section {
                SubsCleanupToggle(
                    label: preview == nil ? "同时删除相关的下载任务"
                        : torrentCount == 0 ? "同时删除相关的下载任务（没有可删除的任务）" : "同时删除相关的下载任务（\(torrentCount) 个）",
                    description: torrentCount == 0 ? nil : "从下载器移除任务，并删除下载目录里的文件，不可恢复。",
                    warning: deleteTorrents && (preview?.hitAndRunCount ?? 0) > 0
                        ? "其中 \(preview?.hitAndRunCount ?? 0) 个种子仍在 H&R 考核或考核状态未知，删除后可能影响站点考核。" : nil,
                    isOn: $deleteTorrents,
                    disabled: preview == nil || torrentCount == 0
                )
                .accessibilityIdentifier("cancel-delete-torrents")
                SubsCleanupToggle(
                    label: preview == nil ? "同时删除媒体库里的资源"
                        : fileCount == 0 ? "同时删除媒体库里的资源（媒体库中没有该作品）"
                        : "同时删除媒体库里的资源（\(fileCount) 个文件 · \(SubsFormat.bytes(preview?.libraryBytes))）",
                    description: fileCount == 0 ? nil : "文件会移入媒体库回收站，\(preview?.recycleRetentionDays ?? 7) 天内可以恢复。",
                    isOn: $deleteFiles,
                    disabled: preview == nil || fileCount == 0
                )
                .accessibilityIdentifier("cancel-delete-files")
            } footer: {
                if deleteTorrents || deleteFiles {
                    Text("订阅会立刻取消，清理在后台进行——可以在「任务中心」查看进度和结果。")
                }
            }
            // 破坏性确认不放右上 ✓：列表末尾一组红色行按钮，与系统「删除」类操作同一形态
            Section {
                Button(role: .destructive) {
                    Task {
                        busy = true
                        await onConfirm(deleteTorrents, deleteFiles)
                        busy = false
                    }
                } label: {
                    HStack {
                        Text(busy ? "处理中…" : "取消订阅")
                        Spacer()
                        if busy { ProgressView() }
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("confirm-cancel-subscription")
            }
        }
        .interactiveDismissDisabled(busy)
        .accessibilityIdentifier("cancel-sheet")
        .task {
            // 每次打开都重新拉预览、开关复位：上次勾过的「连媒体库一起删」绝不能被默认带上
            preview = try? await api.subscriptionsPreviewRemoval(subscriptionId: subscriptionId)
        }
    }
}

// MARK: - 减季后的清理追问

/// 减季保存成功后追问：要不要把移出范围那几季的内容也清理掉。走开的按钮叫「保留内容」——
/// 此刻季已经减完，叫「取消」会被读成「撤销减季」。跨季种子被保护时主动说出来。
struct SeasonCleanupContent: View {
    let title: String
    let seasons: [Int]
    let preview: API.SubscriptionRemovalPreviewView
    let onKeep: () -> Void
    let onConfirm: (_ deleteTorrents: Bool, _ deleteLibraryFiles: Bool) async -> Void

    @State private var deleteTorrents = false
    @State private var deleteFiles = false
    @State private var busy = false

    private func seasonText(_ values: [Int]) -> String { "第 \(values.sorted().map(String.init).joined(separator: "、")) 季" }

    var body: some View {
        let label = seasonText(seasons)
        let them = seasons.count > 1 ? "这几季" : "这一季"
        SubsSheetScaffold(
            title: "\(label)已移出订阅",
            subtitle: "《\(title)》的\(them)不再追了。要不要把已经下载的内容也清理掉？不清理也没关系，种子和文件都原样留着。",
            closeTitle: "保留内容",
            onClose: onKeep
        ) {
            Section {
                SubsCleanupToggle(
                    label: preview.torrentCount == 0 ? "同时删除\(label)的下载任务（没有可单独删除的任务）" : "同时删除\(label)的下载任务（\(preview.torrentCount) 个）",
                    description: preview.torrentCount == 0 ? nil : "从下载器移除任务，并删除下载目录里的文件，不可恢复。以后重新勾选\(them)需要重新下载。",
                    warning: deleteTorrents && preview.hitAndRunCount > 0 ? "其中 \(preview.hitAndRunCount) 个种子仍在 H&R 考核或考核状态未知，删除后可能影响站点考核。" : nil,
                    isOn: $deleteTorrents,
                    disabled: preview.torrentCount == 0
                )
                SubsCleanupToggle(
                    label: preview.libraryFileCount == 0 ? "同时删除\(label)在媒体库里的资源（媒体库中没有）"
                        : "同时删除\(label)在媒体库里的资源（\(preview.libraryFileCount) 个文件 · \(SubsFormat.bytes(preview.libraryBytes))）",
                    description: preview.libraryFileCount == 0 ? nil : "文件会移入媒体库回收站，\(preview.recycleRetentionDays) 天内可以恢复。",
                    isOn: $deleteFiles,
                    disabled: preview.libraryFileCount == 0
                )
            } footer: {
                if deleteTorrents || deleteFiles {
                    Text("清理在后台进行——可以在「任务中心」查看进度和结果。")
                }
            }
            if let first = preview.retainedCrossSeason.first {
                Section {
                    SubsNoticeRow(
                        text: "另有 \(preview.retainedCrossSeason.count) 个跨季种子\(first.seasons.isEmpty ? "" : "（如 \(seasonText(first.seasons))合集）")仍被保留的季使用，不会删除。",
                        tone: .info
                    )
                }
            }
            Section {
                Button(role: .destructive) {
                    Task {
                        busy = true
                        await onConfirm(deleteTorrents, deleteFiles)
                        busy = false
                    }
                } label: {
                    HStack {
                        Text(busy ? "处理中…" : "清理\(label)")
                        Spacer()
                        if busy { ProgressView() }
                    }
                }
                .disabled(busy || !(deleteTorrents || deleteFiles))
            }
        }
        .interactiveDismissDisabled(busy)
    }
}

// MARK: - 调整订阅

/// 调整订阅：创建后修改季选择 / 入库目标库（自动续订是详情页的独立动作）。
/// 季结构走 title-preview（幂等），选库即时走投递预检；只提交发生变化的字段。
/// 减季的清理是保存**之后**的第二步：数量取自后端保存后的真实出域结果，不在前端推算。
struct SubscriptionAdjustSheet: View {
    let detail: API.SubscriptionDetailView
    let onSaved: () -> Void

    @Environment(\.api) private var api
    @Environment(\.permissions) private var permissions
    @Environment(\.dismiss) private var dismiss
    @Environment(Feedback.self) private var feedback

    @State private var seasons: [API.SeasonOverview]?
    @State private var libraries: [API.LibraryView] = []
    @State private var selected: Set<Int> = []
    /// nil = 按默认库路由
    @State private var libraryId: Int?
    @State private var preview: API.DispatchPreviewView?
    @State private var error: String?
    @State private var busy = false
    @State private var cleanup: (seasons: [Int], preview: API.SubscriptionRemovalPreviewView)?

    private var isMovie: Bool { detail.media.kind == "movie" }

    private var droppedWithProgress: [Int] {
        guard !isMovie else { return [] }
        let progressed = Set(detail.wanted.filter { $0.status != "wanted" }.map(\.seasonNumber))
        return detail.selectedSeasons.filter { !selected.contains($0) && progressed.contains($0) }
    }

    private var seasonsChanged: Bool { !isMovie && selected.sorted() != detail.selectedSeasons.sorted() }
    private var libraryChanged: Bool { permissions.canManageSubscriptions && libraryId != detail.libraryId }

    var body: some View {
        if let cleanup {
            SeasonCleanupContent(
                title: detail.media.title,
                seasons: cleanup.seasons,
                preview: cleanup.preview,
                onKeep: { onSaved(); dismiss() },
                onConfirm: { torrents, files in
                    do {
                        _ = try await api.subscriptionsCleanupSeasons(
                            subscriptionId: detail.id,
                            body: .init(seasons: cleanup.seasons, deleteTorrents: torrents, deleteLibraryFiles: files)
                        )
                        feedback.success("正在后台清理，可在「任务中心」查看进度")
                    } catch {
                        feedback.error(error.localizedDescription.isEmpty ? "清理失败，请稍后重试" : error.localizedDescription)
                    }
                    onSaved()
                    dismiss()
                }
            )
        } else {
            form
        }
    }

    private var form: some View {
        SubsSheetScaffold(
            title: "调整订阅",
            subtitle: "《\(detail.media.title)》——加季会恢复或补建追踪；减季会让整季退出追踪范围，但不会删除下载器任务、已下载文件或入库内容。",
            confirm: SubsSheetConfirm(
                title: "保存调整",
                enabled: (seasonsChanged || libraryChanged) && (isMovie || !selected.isEmpty),
                busy: busy,
                identifier: "adjust-save"
            ) { Task { await save() } },
            ready: isMovie || seasons != nil || error != nil
        ) {
            if let error {
                Section { SubsNoticeRow(text: error, tone: .error) }
            }
            if !isMovie {
                Section {
                    if let seasons {
                        ForEach(seasons, id: \.seasonNumber) { season in
                            SeasonPickRow(season: season, checked: selected.contains(season.seasonNumber)) {
                                if selected.contains(season.seasonNumber) { selected.remove(season.seasonNumber) } else { selected.insert(season.seasonNumber) }
                            }
                        }
                    } else {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("正在加载季集信息…").foregroundStyle(Theme.textMuted)
                        }
                    }
                } header: {
                    Text("选择要收录的季")
                } footer: {
                    Text("勾选即要整季（含未播集）")
                }
                if !droppedWithProgress.isEmpty {
                    Section {
                        SubsNoticeRow(
                            text: "第 \(droppedWithProgress.map(String.init).joined(separator: "、")) 季已有下载进度：保存只让它退出追踪（停止进度关联、缺失搜索与自动换源），不会动任何文件"
                                + (permissions.canManageSubscriptions ? "；保存后会问你要不要顺手清理这一季的内容" : ""),
                            tone: .warn
                        )
                    }
                }
            }
            if permissions.canManageSubscriptions, !libraries.isEmpty {
                Section {
                    Picker("入库到", selection: $libraryId) {
                        Text("按默认库路由").tag(Int?.none)
                        ForEach(libraries, id: \.id) { library in
                            Text(library.name + (library.isDefault ? "（默认）" : "")).tag(Int?.some(library.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .accessibilityIdentifier("adjust-library")
                } footer: {
                    if let preview { DispatchPreviewNote(preview: preview, adjusting: true) }
                }
            }
        }
        .accessibilityIdentifier("adjust-sheet")
        .task {
            selected = Set(detail.selectedSeasons)
            libraryId = detail.libraryId
            do {
                if !isMovie {
                    let prepared = try await api.uiSubscriptionsPreviewTitle(body: .init(titleRef: "tmdb:\(detail.media.kind):\(detail.media.tmdbId)"))
                    seasons = prepared.status == "ready" ? prepared.seasons : []
                }
                if permissions.canManageSubscriptions {
                    libraries = try await api.libraryList(kind: detail.media.kind, scope: "all")
                }
            } catch is CancellationError {
            } catch {
                self.error = "加载季集与媒体库信息失败，请稍后重试"
            }
        }
        .task(id: libraryId) {
            // 选库即预演投递落点（null = 该类型默认库也预演）
            guard permissions.canManageSubscriptions else { return }
            preview = nil
            preview = try? await api.subscriptionsPreviewDownloadRouting(kind: detail.media.kind, libraryId: libraryId, tmdbId: detail.media.tmdbId)
        }
    }

    private func save() async {
        busy = true
        error = nil
        let dropped = seasonsChanged ? detail.selectedSeasons.filter { !selected.contains($0) } : []
        do {
            _ = try await api.subscriptionsAdjust(
                subscriptionId: detail.id,
                body: SubscriptionAdjustPayload(
                    selectedSeasons: seasonsChanged ? selected.sorted() : nil,
                    libraryId: libraryChanged ? .some(libraryId) : nil
                )
            )
            feedback.success("订阅已调整")
            if !dropped.isEmpty, permissions.canManageSubscriptions {
                if let plan = try? await api.subscriptionsPreviewRemoval(subscriptionId: detail.id, seasons: dropped) {
                    if plan.torrentCount > 0 || plan.libraryFileCount > 0 {
                        cleanup = (dropped, plan)
                        busy = false
                        return
                    }
                } else {
                    feedback.info("订阅已调整；这一季的内容未作清理")
                }
            }
            onSaved()
            dismiss()
        } catch {
            self.error = error.localizedDescription
            busy = false
        }
    }
}

// MARK: - 更换规则组

/// 换规则组：列出全部规则组（含条件摘要），点选即应用；只影响之后的资源评估
struct RuleSetSwitchSheet: View {
    let ruleSets: [API.RuleSetView]
    let currentId: Int
    let onPick: (Int) async throws -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        SubsSheetScaffold(
            title: "更换规则组",
            subtitle: "点选即应用，只影响之后的资源评估；已下载/已入库的内容不受影响。需要新的组合条件可去「设置 → 订阅规则 → 规则组」新建。"
        ) {
            if let error {
                Section { SubsNoticeRow(text: error, tone: .error) }
            }
            Section {
                ForEach(ruleSets, id: \.id) { rule in
                    let chips = RuleSetText.summary(rule.typedSpec)
                    SubsChoiceRow(
                        title: rule.name + (rule.isDefault ? "（默认）" : ""),
                        subtitle: chips.isEmpty ? "全不限" : chips.joined(separator: " · "),
                        selected: rule.id == currentId
                    ) {
                        Task { await pick(rule.id) }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("ruleset-option")
                }
            }
        }
        .accessibilityIdentifier("ruleset-switch-sheet")
    }

    private func pick(_ id: Int) async {
        if id == currentId {
            dismiss()
            return
        }
        busy = true
        error = nil
        do {
            try await onPick(id)
            dismiss()
        } catch {
            self.error = error.localizedDescription
            busy = false
        }
    }
}

// MARK: - 管理菜单（移动端「更多」抽屉）

/// 「更多」管理面板：每一行完整触控目标，危险操作与普通配置之间用分隔断开；
/// 具体动作的二次确认由调用方负责。
struct SubscriptionManageSheet: View {
    enum Action { case adjust, upgradeRun, toggleFollow, switchRule, togglePause, remove }

    let paused: Bool
    let completed: Bool
    let canSubscribe: Bool
    let canManage: Bool
    /// nil = 电影订阅（不展示自动续订）
    let followFuture: Bool?
    let busy: Bool
    let onAction: (Action) -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if canSubscribe { row("调整订阅", systemImage: "slider.horizontal.3", action: .adjust, chevron: true) }
                    if canSubscribe { row("洗一轮版", systemImage: "sparkles", action: .upgradeRun, chevron: true) }
                    if canSubscribe, let followFuture {
                        row(followFuture ? "关闭自动续订" : "开启自动续订", systemImage: followFuture ? "bell.slash" : "bell", action: .toggleFollow)
                    }
                    if canManage { row("更换规则组", systemImage: "list.bullet.rectangle", action: .switchRule, chevron: true) }
                    if canSubscribe {
                        row(paused ? "恢复追踪" : "暂停追踪", systemImage: paused ? "play.circle" : "pause.circle", action: .togglePause, chevron: true)
                            .disabled(busy || completed)
                    }
                }
                if canSubscribe {
                    Section {
                        Button(role: .destructive) {
                            dismiss()
                            onAction(.remove)
                        } label: {
                            Label("取消订阅", systemImage: "trash")
                        }
                        .disabled(busy)
                        .accessibilityIdentifier("manage-remove")
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .navigationTitle("管理订阅")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭", systemImage: "xmark") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .accessibilityIdentifier("manage-sheet")
    }

    private func row(_ title: String, systemImage: String, action: Action, chevron: Bool = false) -> some View {
        Button {
            dismiss()
            onAction(action)
        } label: {
            HStack {
                Label(title, systemImage: systemImage).foregroundStyle(Theme.text)
                Spacer()
                if chevron { Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint) }
            }
        }
        .disabled(busy)
        .accessibilityIdentifier("manage-\(title)")
    }
}

// MARK: - 自定义取消键的确认框

/// 订阅详情页的确认框：与全局 `Feedback.confirm` 同一形态（系统 alert），只多一个可定制的取消键文案。
///
/// 为什么不用全局那个：它的取消键固定「取消」。Web 订阅详情的「立即搜索」「暂停/恢复追踪」取消键是「返回」，
/// 成员「取消订阅」的取消键是「先不」——若用「取消」，会和确认键「取消订阅」并列、两个都以「取消」开头，容易看混。
@Observable
final class SubsConfirmCenter {
    struct Request {
        var title: String
        var message: String?
        var confirmTitle: String
        var cancelTitle: String
        var destructive: Bool
        fileprivate let resolver: Resolver
    }

    /// 只回答一次（按钮动作与弹窗关闭回调都可能触发）
    fileprivate final class Resolver {
        private var continuation: CheckedContinuation<Bool, Never>?
        init(_ continuation: CheckedContinuation<Bool, Never>) { self.continuation = continuation }
        func resolve(_ value: Bool) {
            continuation?.resume(returning: value)
            continuation = nil
        }
    }

    var request: Request?

    func confirm(_ title: String, message: String?, confirmTitle: String, cancelTitle: String, destructive: Bool = false) async -> Bool {
        await withCheckedContinuation { continuation in
            request = Request(
                title: title, message: message, confirmTitle: confirmTitle, cancelTitle: cancelTitle,
                destructive: destructive, resolver: Resolver(continuation)
            )
        }
    }

    fileprivate func finish(_ value: Bool) {
        let pending = request
        request = nil
        pending?.resolver.resolve(value)
    }
}

/// 承载 `SubsConfirmCenter` 的弹窗（挂在使用它的页面上）
struct SubsConfirmHost: ViewModifier {
    @Bindable var center: SubsConfirmCenter

    func body(content: Content) -> some View {
        content.alert(
            center.request?.title ?? "",
            isPresented: Binding(get: { center.request != nil }, set: { if !$0 { center.finish(false) } }),
            presenting: center.request
        ) { request in
            Button(request.cancelTitle, role: .cancel) { center.finish(false) }
            Button(request.confirmTitle, role: request.destructive ? .destructive : nil) { center.finish(true) }
        } message: { request in
            if let message = request.message { Text(message) }
        }
    }
}
